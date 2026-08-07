"""Skill hierarchy and prerequisite expansion for governed retrieval."""

from __future__ import annotations

from app.content_pipeline.contracts import PREREQUISITES, SKILLS, SUBSKILLS


def skills() -> list[str]:
    return list(SKILLS)


def subskills_of(skill: str) -> list[str]:
    return list(SUBSKILLS.get(skill, []))


def prerequisites_of(skill: str, *, max_hops: int = 2) -> list[str]:
    """Return prerequisite skills within ``max_hops`` of ``skill``.

    The expansion is breadth-first over the reviewed prerequisite graph,
    including the skill itself at hop 0. Results are ordered by hop, then
    alphabetically for determinism.
    """
    if max_hops < 1:
        return []
    seen: set[str] = set()
    ordered: list[str] = []
    frontier = [skill]
    for _ in range(max_hops + 1):
        if not frontier:
            break
        next_frontier: list[str] = []
        for current in sorted(frontier):
            if current in seen:
                continue
            seen.add(current)
            ordered.append(current)
            next_frontier.extend(PREREQUISITES.get(current, []))
        frontier = next_frontier
    return ordered
