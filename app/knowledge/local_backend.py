"""Governed PostgreSQL tsvector retrieval backend.

Implements the fixed retrieval order from plan section 8:

    review_status=published + audience filter
    -> license/source filter
    -> skill/subskill/misconception filter
    -> PostgreSQL tsvector (websearch_to_tsquery)
    -> at most two-hop prerequisite expansion
    -> deterministic reranking
    -> citation/version/license validation
    -> approved content or an explicit no-result

The tsvector index is derived from published content packs only (see
``index_pack``); the content registry stays authoritative.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from app.knowledge import citations
from app.knowledge.citations import PUBLISHED_STATUS, RESTRICTED_SOURCES
from app.knowledge.hierarchy import prerequisites_of

DEFAULT_AUDIENCE = "student"
DEFAULT_ALLOWED_LICENSES = ("bridgesat_original",)

# Versioned reranker weights. The version string is returned with results
# so weight changes are auditable; weights are tuned only on the dev set.
WEIGHTS_V1 = {
    "weight_version": "v1",
    "fts_rank": 1.0,
    "skill_exact": 3.0,
    "subskill_exact": 2.0,
    "prerequisite_hop": 1.2,
    "difficulty_proximity": 0.8,
    "content_type": 1.0,
    "offline_availability": 0.5,
    "recently_shown": -1.0,
    "how_to_lesson": 1.5,
}

# Deterministic cues that mark a query as instructional ("how do I solve
# ...") so lessons surface before hint-heavy question bodies. Operational
# words like "evaluate" are not cues: "evaluate 7x - 6 at negative four" is
# an exact-item query, not a request for instruction.
HOW_TO_CUES = frozenset(
    {
        "how",
        "solve",
        "method",
        "steps",
        "work",
        "out",
        "isolate",
        "eliminate",
        "model",
        "rate",
    }
)

# Function words do not count as evidence that a bare query overlaps the
# corpus; a query whose only matches are stopwords is an explicit no-result.
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "from",
        "in",
        "into",
        "is",
        "of",
        "on",
        "or",
        "the",
        "to",
        "when",
        "with",
        "like",
        "than",
    }
)

# A bare query (no metadata filter) must match this many distinct
# content-bearing terms, otherwise the result is an explicit no-result.
MIN_BARE_QUERY_TERM_HITS = 2

# Candidates beyond this window have no fts_rank contribution in the fixed
# reranker; their lexical position should not break a semantic tie.
FTS_TIE_BREAK_WINDOW = 5


@dataclass(frozen=True)
class RetrievalResult:
    content_id: str
    version: int
    content_type: str
    target_skill: str
    target_subskill: str
    audience: str
    license_id: str
    license_name: str
    source_id: str
    review_status: str
    body: str
    citation: str
    score: float
    rank: int


@dataclass
class RetrievalResponse:
    results: list[RetrievalResult] = field(default_factory=list)
    explicit_no_result: bool = False
    weights_version: str = WEIGHTS_V1["weight_version"]
    elapsed_ms: int = 0
    expanded_skills: list[str] = field(default_factory=list)


class RestrictedSourceError(RuntimeError):
    """A pack item draws from a source that must never enter retrieval."""


class UnpublishedPackError(RuntimeError):
    """Only published packs may be indexed."""


LESSON_TYPES = ("lesson", "micro_lesson", "worked_example")


def _indexed_body(item: dict) -> str:
    if item.get("content_type") in LESSON_TYPES:
        return " ".join(
            str(part)
            for part in (item.get("title", ""), item.get("body", ""))
            if part
        )
    parts = [
        item.get("prompt", ""),
        " ".join(
            hint.get("text", "") for hint in item.get("hints", []) if hint
        ),
        item.get("worked_explanation", ""),
    ]
    misconceptions = set((item.get("misconception_map") or {}).values())
    if misconceptions:
        parts.append("misconceptions: " + " ".join(sorted(misconceptions)))
    return " ".join(part for part in parts if part)


def _audience_of(item: dict) -> str:
    return item.get("audience", DEFAULT_AUDIENCE)


def _row_to_record(row) -> dict:
    return {
        "content_id": row["content_id"],
        "version": row["version"],
        "content_type": row["content_type"],
        "target_skill": row["target_skill"],
        "target_subskill": row["target_subskill"],
        "audience": row["audience"],
        "license_id": row["license_id"],
        "license_name": row["license_name"],
        "source_id": row["source_id"],
        "review_status": row["review_status"],
        "body": row["body"],
        "_raw_score": 0.0,
        "_fts_hit": False,
        "_hop": 9,
    }


def _to_result(record: dict, score: float, rank: int) -> RetrievalResult:
    return RetrievalResult(
        content_id=record["content_id"],
        version=record["version"],
        content_type=record["content_type"],
        target_skill=record["target_skill"],
        target_subskill=record["target_subskill"],
        audience=record["audience"],
        license_id=record["license_id"],
        license_name=record["license_name"],
        source_id=record["source_id"],
        review_status=record["review_status"],
        body=record["body"],
        citation=citations.citation_label(
            content_id=record["content_id"],
            version=record["version"],
            content_type=record["content_type"],
            source_id=record["source_id"],
            license_id=record["license_id"],
        ),
        score=score,
        rank=rank,
    )


def index_pack(connection: psycopg.Connection, pack_dir: Path) -> dict:
    """Index a published pack's items and lessons into the tsvector table.

    Restricted sources (for example GSM8K) are rejected outright; they must
    never enter the index or any retrieval path. Indexing is a rebuild: the
    table is cleared and repopulated from the pack.
    """
    manifest = json.loads((pack_dir / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status") != PUBLISHED_STATUS:
        raise UnpublishedPackError(f"Pack {pack_dir.name} is not published")

    rows: list[dict] = []
    item_count = 0
    lesson_count = 0
    for file_name, content_type in (
        ("items.jsonl", "question"),
        ("lessons.jsonl", "lesson"),
    ):
        path = pack_dir / file_name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            item = json.loads(line)
            lineage = item.get("source_lineage") or {}
            source_id = lineage.get("source_id", "")
            if source_id in RESTRICTED_SOURCES:
                raise RestrictedSourceError(
                    f"{item.get('id')} draws from restricted source {source_id}"
                )
            actual_type = item.get("content_type", content_type)
            rows.append(
                {
                    "content_id": item["id"],
                    "version": item.get("version", 1),
                    "content_type": actual_type,
                    "target_skill": item.get("target_skill", ""),
                    "target_subskill": item.get("target_subskill", ""),
                    "audience": _audience_of(item),
                    "license_id": (item.get("license") or {}).get("id", ""),
                    "license_name": (item.get("license") or {}).get("name", ""),
                    "source_id": source_id,
                    # The pack is the published artifact: items inside a
                    # published pack are published for retrieval.
                    "review_status": PUBLISHED_STATUS,
                    "body": _indexed_body(item),
                }
            )
            if actual_type in LESSON_TYPES:
                lesson_count += 1
            else:
                item_count += 1

    with connection.transaction():
        connection.execute("DELETE FROM knowledge_fts")
        with connection.cursor() as cursor:
            cursor.executemany(
                """
                INSERT INTO knowledge_fts (
                    content_id, version, content_type, target_skill,
                    target_subskill, audience, license_id, license_name,
                    source_id, review_status, body
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (content_id) DO UPDATE SET
                    version = EXCLUDED.version,
                    content_type = EXCLUDED.content_type,
                    target_skill = EXCLUDED.target_skill,
                    target_subskill = EXCLUDED.target_subskill,
                    audience = EXCLUDED.audience,
                    license_id = EXCLUDED.license_id,
                    license_name = EXCLUDED.license_name,
                    source_id = EXCLUDED.source_id,
                    review_status = EXCLUDED.review_status,
                    body = EXCLUDED.body
                """,
                [
                    (
                        row["content_id"],
                        row["version"],
                        row["content_type"],
                        row["target_skill"],
                        row["target_subskill"],
                        row["audience"],
                        row["license_id"],
                        row["license_name"],
                        row["source_id"],
                        row["review_status"],
                        row["body"],
                    )
                    for row in rows
                ],
            )
        connection.execute(
            """
            INSERT INTO knowledge_index_log (
                indexed_at, pack_id, pack_version, item_count, lesson_count, status
            ) VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                datetime.now(UTC).isoformat(),
                manifest["pack_id"],
                manifest["pack_version"],
                item_count,
                lesson_count,
                "indexed",
            ),
        )
    return {"items": item_count, "lessons": lesson_count}


class KnowledgeBackend:
    """PostgreSQL tsvector retrieval with the fixed governed pipeline."""

    def __init__(self, connection: psycopg.Connection, *, weights: dict | None = None) -> None:
        self.connection = connection
        self.weights = weights or WEIGHTS_V1

    # --- pipeline steps ----------------------------------------------------

    def retrieve(
        self,
        query: str,
        *,
        audience: str = DEFAULT_AUDIENCE,
        allowed_licenses: tuple[str, ...] = DEFAULT_ALLOWED_LICENSES,
        skill: str | None = None,
        subskill: str | None = None,
        misconception: str | None = None,
        difficulty: int | None = None,
        content_type: str | None = None,
        recently_shown: set[str] | None = None,
        max_results: int = 5,
    ) -> RetrievalResponse:
        started = time.perf_counter()
        recently_shown = recently_shown or set()

        expanded_skills = prerequisites_of(skill, max_hops=2) if skill else []

        candidates = self._search(
            self.connection,
            query,
            audience=audience,
            allowed_licenses=allowed_licenses,
            skill=skill,
            subskill=subskill,
            misconception=misconception,
            expanded_skills=expanded_skills,
        )

        scored = self._rerank(
            candidates,
            query=query,
            skill=skill,
            subskill=subskill,
            misconception=misconception,
            difficulty=difficulty,
            content_type=content_type,
            recently_shown=recently_shown,
            expanded_skills=expanded_skills,
        )

        results: list[RetrievalResult] = []
        explicit_no_result = False
        for rank, (record, score) in enumerate(scored, start=1):
            if rank > max_results:
                break
            missing = citations.validate_metadata(record)
            if missing:
                # Any missing citation/version/license field excludes the
                # result; coverage is measured in evals.
                continue
            results.append(_to_result(record, score, rank))

        if not results:
            explicit_no_result = True

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        return RetrievalResponse(
            results=results,
            explicit_no_result=explicit_no_result,
            weights_version=self.weights["weight_version"],
            elapsed_ms=elapsed_ms,
            expanded_skills=expanded_skills,
        )

    def _search(
        self,
        connection,
        query: str,
        *,
        audience: str,
        allowed_licenses: tuple[str, ...],
        skill: str | None,
        subskill: str | None,
        misconception: str | None,
        expanded_skills: list[str],
    ) -> list[dict]:
        if not allowed_licenses:
            return []

        base_sql = """
            SELECT content_id, version, content_type, target_skill,
                   target_subskill, audience, license_id, license_name,
                   source_id, review_status, body
            FROM knowledge_fts
            WHERE audience = %s
              AND review_status = %s
              AND license_id IN ({license_ph})
              AND source_id NOT IN ({source_ph})
        """
        params: list[object] = [audience, PUBLISHED_STATUS]
        placeholders_license = ",".join(["%s"] * len(allowed_licenses))
        params.extend(allowed_licenses)
        placeholders_sources = ",".join(["%s"] * len(RESTRICTED_SOURCES))
        params.extend(RESTRICTED_SOURCES)

        sql = base_sql.format(
            license_ph=placeholders_license, source_ph=placeholders_sources
        )

        has_metadata_filter = False
        if skill:
            sql += " AND target_skill IN (%s)" % ",".join(["%s"] * len(expanded_skills))
            params.extend(expanded_skills)
            has_metadata_filter = True
        if subskill:
            sql += " AND target_subskill = %s"
            params.append(subskill)
            has_metadata_filter = True
        if misconception:
            sql += " AND body LIKE %s"
            params.append(f"%{misconception}%")
            has_metadata_filter = True

        match = self._match_phrase(query)
        fts_hits: list[tuple[str, float]] = []
        if query.strip():
            try:
                fts_sql = (
                    # Cover-density rank is the PG relevance signal; the
                    # lexical rank keeps OR-heavy queries close to SQLite
                    # BM25 ordering by accounting for frequency and length.
                    "SELECT content_id, "
                    "ts_rank_cd(body_tsv, q.tsv) AS _rank, "
                    "ts_rank(body_tsv, q.tsv) AS _lexical_rank "
                    "FROM knowledge_fts, websearch_to_tsquery('english', %s) AS q(tsv) "
                    + sql.split("FROM knowledge_fts", 1)[1].lstrip("\n ")
                    + " AND body_tsv @@ q.tsv "
                    + "ORDER BY _lexical_rank DESC, _rank DESC, content_id"
                )
                fts_rows = connection.execute(fts_sql, [match, *params]).fetchall()
                fts_hits = [
                    (row["content_id"], float(row["_rank"])) for row in fts_rows
                ]
            except Exception:
                fts_hits = []

        if not has_metadata_filter:
            if not fts_hits:
                return []
            content_terms = [
                token.strip('"')
                for token in query.replace('"', " ").split()
                if token.strip('"')
            ]
            content_terms = [
                token
                for token in content_terms
                if token not in STOPWORDS
                and any(char.isalnum() for char in token)
            ]
            term_hits = 0
            for token in content_terms[:8]:
                row = connection.execute(
                    "SELECT 1 AS hit FROM knowledge_fts, "
                    "websearch_to_tsquery('english', %s) AS q(tsv) "
                    "WHERE body_tsv @@ q.tsv LIMIT 1",
                    [f'"{token}"'],
                ).fetchone()
                if row:
                    term_hits += 1
            if term_hits < MIN_BARE_QUERY_TERM_HITS:
                return []

        if not has_metadata_filter:
            hit_ids = [content_id for content_id, _ in fts_hits]
            sql += " AND content_id IN (%s)" % ",".join(["%s"] * len(hit_ids))
            params.extend(hit_ids)

        rows = connection.execute(sql, params).fetchall()
        fts_rank = {content_id: index for index, (content_id, _) in enumerate(fts_hits)}
        candidates: list[dict] = []
        for row in rows:
            record = _row_to_record(row)
            record["_fts_hit"] = row["content_id"] in fts_rank
            record["_fts_position"] = fts_rank.get(row["content_id"], 99)
            record["_hop"] = (
                expanded_skills.index(record["target_skill"])
                if record["target_skill"] in expanded_skills
                else 9
            )
            candidates.append(record)
        return candidates

    @staticmethod
    def _match_phrase(query: str) -> str:
        """Turn a free-text query into a safe websearch_to_tsquery input.

        Terms are OR-joined so any single lexical overlap recalls the row;
        the ranker uses ts_rank_cd position to separate relevant results.
        """
        tokens = [token.strip('"') for token in query.replace('"', " ").split()]
        terms = [
            token
            for token in tokens
            if token
            and token not in ("AND", "OR")
            and any(char.isalnum() for char in token)
        ]
        if not terms:
            return '""'
        return " OR ".join(f'"{term}"' for term in terms[:8])

    def _rerank(
        self,
        candidates: list[dict],
        *,
        query: str,
        skill: str | None,
        subskill: str | None,
        misconception: str | None,
        difficulty: int | None,
        content_type: str | None,
        recently_shown: set[str],
        expanded_skills: list[str],
    ) -> list[tuple[dict, float]]:
        query_terms = set(query.lower().split()) if query else set()
        is_how_to = bool(query_terms & HOW_TO_CUES)
        scored: list[tuple[dict, float]] = []
        for record in candidates:
            score = 0.0
            if record["_fts_hit"]:
                # Stronger ts_rank position contributes more; the top
                # hit gets the full weight, later hits fade out.
                score += self.weights["fts_rank"] * max(
                    0.0, 1.0 - 0.25 * record["_fts_position"]
                )
            if skill and record["target_skill"] == skill:
                score += self.weights["skill_exact"]
            if subskill and record["target_subskill"] == subskill:
                score += self.weights["subskill_exact"]
            if difficulty and record.get("target_difficulty"):
                score += self.weights["difficulty_proximity"] * max(
                    0.0, 1.0 - abs(record["target_difficulty"] - difficulty) / 4.0
                )
            if content_type and record["content_type"] == content_type:
                score += self.weights["content_type"]
            if is_how_to and record["content_type"] in ("micro_lesson", "worked_example"):
                score += self.weights["how_to_lesson"]
            if record["content_id"] in recently_shown:
                score += self.weights["recently_shown"]
            if record["_hop"] <= 2:
                score += self.weights["prerequisite_hop"] * (1.0 - record["_hop"] * 0.4)
            if misconception and misconception in record["body"]:
                score += self.weights["skill_exact"] * 0.5
            if query_terms:
                body_terms = set(record["body"].lower().split())
                overlap = len(query_terms & body_terms)
                score += 0.25 * min(overlap, 4)
            scored.append((record, score))

        scored.sort(
            key=lambda pair: (
                -pair[1],
                pair[0]["_fts_position"]
                if pair[0]["_fts_hit"]
                and pair[0]["_fts_position"] <= FTS_TIE_BREAK_WINDOW
                else 99,
                pair[0]["content_id"],
                pair[0]["version"],
            )
        )
        return scored
