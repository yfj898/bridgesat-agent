"""Citation labels and metadata validation for retrieval results.

Every retrieval result must carry content ID/version, source lineage,
license, review status, and a citation label; any missing field excludes
the result (plan section 8).
"""

from __future__ import annotations

REQUIRED_FIELDS = (
    "content_id",
    "version",
    "content_type",
    "target_skill",
    "audience",
    "license_id",
    "license_name",
    "source_id",
    "review_status",
    "body",
)

# Subskills are only meaningful for questions; lessons carry an empty
# subskill by design (see the lesson schema).
SUBSUBSKILL_REQUIRED_TYPES = ("question",)

# Sources that must never enter the retrieval path (plan section 8).
RESTRICTED_SOURCES = ("gsm8k", "gsm8k_synthetic", "gsm8k_math")

PUBLISHED_STATUS = "published"


def citation_label(
    *,
    content_id: str,
    version: int,
    content_type: str,
    source_id: str,
    license_id: str,
) -> str:
    """Build a deterministic, auditable citation label for a result."""
    return (
        f"BridgeSAT math item {content_id} v{version} ({content_type}); "
        f"concept source {source_id}; license {license_id}"
    )


def validate_metadata(record: dict) -> list[str]:
    """Return a list of missing-required-field errors; empty means valid.

    A record that fails validation must be excluded from results and is
    counted against metadata coverage in evals.
    """
    missing = [field for field in REQUIRED_FIELDS if not record.get(field)]
    if record.get("content_type") in SUBSUBSKILL_REQUIRED_TYPES and not record.get(
        "target_subskill"
    ):
        missing.append("target_subskill")
    return missing
