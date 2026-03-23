"""Tests for :mod:`omnifocus.fuzzy`."""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from datetime import UTC, datetime

from omnifocus.fuzzy import (
    EXACT_SCORE,
    MIN_SCORE,
    SUBSTRING_SCORE,
    _score,
    find_tasks,
)
from omnifocus.models import Task

UTC = UTC
NOW = datetime(2026, 3, 22, tzinfo=UTC)


def _task(tid: str, name: str) -> Task:
    return Task(
        id=tid,
        name=name,
        parent_task_id=None,
        project_id=None,
        inbox=True,
        completed=None,
        flagged=False,
        due=None,
        start=None,
        hidden=None,
        note="",
        rank=1,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )


TASKS = [
    _task("t01", "Buy milk"),
    _task("t02", "Buy bread"),
    _task("t03", "Deploy to production"),
    _task("t04", "Write unit tests"),
    _task("t05", "Fix production bug"),
    _task("t06", "Zrób zakupy spożywcze"),  # Polish
    _task("t07", "Buy 🛒 groceries"),  # emoji
]


class TestFindTasks:
    def test_exact_id_match(self) -> None:
        results = find_tasks("t01", TASKS)
        assert len(results) == 1
        assert results[0].score == EXACT_SCORE
        assert results[0].task.id == "t01"

    def test_exact_id_returns_immediately(self) -> None:
        """Exact ID match must short-circuit and return one result."""
        results = find_tasks("t03", TASKS)
        assert len(results) == 1
        assert results[0].task.name == "Deploy to production"

    def test_substring_match(self) -> None:
        results = find_tasks("milk", TASKS)
        assert len(results) >= 1
        assert results[0].score == SUBSTRING_SCORE
        assert results[0].task.id == "t01"

    def test_case_insensitive_substring(self) -> None:
        results = find_tasks("MILK", TASKS)
        assert results[0].task.id == "t01"

    def test_multiple_substring_matches(self) -> None:
        results = find_tasks("buy", TASKS)
        ids = {r.task.id for r in results}
        assert "t01" in ids
        assert "t02" in ids

    def test_token_overlap(self) -> None:
        results = find_tasks("production deploy", TASKS)
        assert any(r.task.id == "t03" for r in results)

    def test_difflib_fallback(self) -> None:
        results = find_tasks("milkk", TASKS)
        # "milkk" vs "Buy milk" — difflib should still find it
        matching = [r for r in results if r.task.id == "t01"]
        assert len(matching) == 1

    def test_no_match(self) -> None:
        results = find_tasks("zzznomatch999", TASKS)
        assert results == []

    def test_empty_query(self) -> None:
        # Empty query might match everything via difflib or nothing
        results = find_tasks("", TASKS)
        # Should not raise; result may be empty or partial
        assert isinstance(results, list)

    def test_results_sorted_by_score_descending(self) -> None:
        results = find_tasks("buy", TASKS)
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    def test_limit_respected(self) -> None:
        results = find_tasks("b", TASKS, limit=2)
        assert len(results) <= 2

    def test_empty_task_list(self) -> None:
        assert find_tasks("anything", []) == []

    def test_unicode_task_name(self) -> None:
        results = find_tasks("zakupy", TASKS)
        assert any(r.task.id == "t06" for r in results)

    def test_emoji_in_query(self) -> None:
        results = find_tasks("🛒", TASKS)
        assert any(r.task.id == "t07" for r in results)


class TestScore:
    def test_substring_returns_08(self) -> None:
        assert _score("milk", "buy milk") == SUBSTRING_SCORE

    def test_exact_match_substring(self) -> None:
        assert _score("buy milk", "buy milk") == SUBSTRING_SCORE

    def test_no_match_returns_zero(self) -> None:
        s = _score("zzz", "completely unrelated")
        assert s < MIN_SCORE or s == 0.0

    def test_token_overlap_partial(self) -> None:
        # "deploy production" vs "deploy to production": 2/2 tokens overlap
        s = _score("deploy production", "deploy to production")
        assert s > 0.0

    def test_token_overlap_one_of_two(self) -> None:
        s = _score("deploy staging", "deploy to production")
        assert 0.0 < s < SUBSTRING_SCORE
