"""Fuzzy task name matching.

Provides a three-tier strategy for matching a user query against task names:

1. **Exact ID** — query equals task id exactly → score 1.0
2. **Substring** — query is contained in name (case-insensitive) → score 0.8
3. **Token overlap** — shared word tokens between query and name → score 0.3–0.5
4. **SequenceMatcher** — difflib similarity ratio → score ≤ 0.4

Callers receive a list of ``(score, task)`` tuples sorted by descending score
and can decide how to handle zero, one, or multiple matches.

Usage::

    from omnifocus.fuzzy import find_tasks, MatchResult

    results = find_tasks("buy bread", model.active_tasks)
    if not results:
        raise OFTaskNotFound("buy bread")
    if len(results) == 1 or results[0].score >= 0.8:
        best = results[0].task
    else:
        # Present choices to the user
        ...
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from omnifocus.models import Task

# Score thresholds
EXACT_SCORE = 1.0
SUBSTRING_SCORE = 0.8
TOKEN_OVERLAP_MAX = 0.5
SEQMATCH_SCALE = 0.4
SEQMATCH_MIN_RATIO = 0.4

# Minimum score for a result to be included
MIN_SCORE = 0.1


@dataclass(frozen=True)
class MatchResult:
    """A single fuzzy match result.

    Attributes:
        score: Confidence score in [0, 1].
        task: The matched :class:`~omnifocus.models.Task`.
    """

    score: float
    task: Task


def find_tasks(
    query: str,
    tasks: Sequence[Task],
    limit: int = 10,
) -> list[MatchResult]:
    """Match *query* against a sequence of tasks.

    Args:
        query: User-supplied search string (may be a task id or name fragment).
        tasks: Candidate tasks to search.
        limit: Maximum number of results to return.

    Returns:
        List of :class:`MatchResult` sorted by descending score, capped at
        *limit*.  Returns an empty list if no task scores above
        :data:`MIN_SCORE`.
    """
    query_stripped = query.strip()
    query_lower = query_stripped.lower()

    results: list[MatchResult] = []

    for task in tasks:
        # Tier 1: exact ID match
        if task.id == query_stripped:
            return [MatchResult(score=EXACT_SCORE, task=task)]

        name_lower = task.name.lower()
        score = _score(query_lower, name_lower)
        if score >= MIN_SCORE:
            results.append(MatchResult(score=score, task=task))

    results.sort(key=lambda r: -r.score)
    return results[:limit]


def _score(query_lower: str, name_lower: str) -> float:
    """Compute a similarity score between the lowercased query and name."""
    # Tier 2: substring
    if query_lower in name_lower:
        return SUBSTRING_SCORE

    # Tier 3: token overlap
    q_tokens = set(query_lower.split())
    n_tokens = set(name_lower.split())
    if q_tokens:
        overlap_ratio = len(q_tokens & n_tokens) / len(q_tokens)
        if overlap_ratio > 0:
            return TOKEN_OVERLAP_MAX * overlap_ratio

    # Tier 4: difflib
    ratio = SequenceMatcher(None, query_lower, name_lower).ratio()
    if ratio >= SEQMATCH_MIN_RATIO:
        return ratio * SEQMATCH_SCALE

    return 0.0
