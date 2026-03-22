"""Tests for :mod:`omnifocus.formatting`."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from io import StringIO

import pytest
from rich.console import Console

from omnifocus.formatting import (
    _format_due,
    _project_icon,
    _project_name,
    render_project_tree,
    render_projects_json,
    render_tasks_json,
    render_tasks_table,
)
from omnifocus.models import Folder, Project, Task

UTC = timezone.utc
NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


def _console() -> tuple[Console, StringIO]:
    buf = StringIO()
    con = Console(file=buf, highlight=False, markup=False, no_color=True, width=200)
    return con, buf


def _task(
    tid: str = "t1",
    name: str = "Task",
    project_id: str | None = None,
    due: datetime | None = None,
    flagged: bool = False,
) -> Task:
    return Task(
        id=tid,
        name=name,
        parent_task_id=None,
        project_id=project_id,
        inbox=project_id is None,
        completed=None,
        flagged=flagged,
        due=due,
        start=None,
        hidden=None,
        note="",
        rank=100,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )


def _project(
    pid: str = "p1",
    name: str = "My Project",
    folder_id: str | None = None,
    status: str = "active",
    flagged: bool = False,
) -> Project:
    return Project(
        id=pid,
        name=name,
        folder_id=folder_id,
        status=status,
        singleton=False,
        rank=100,
        added=NOW,
        modified=NOW,
        flagged=flagged,
        due=None,
        start=None,
        note="",
        completed=None,
    )


def _folder(fid: str = "f1", name: str = "Work") -> Folder:
    return Folder(
        id=fid,
        name=name,
        parent_folder_id=None,
        rank=100,
        added=NOW,
        modified=NOW,
    )


# ---------------------------------------------------------------------------
# render_tasks_table
# ---------------------------------------------------------------------------


class TestRenderTasksTable:
    def test_renders_without_error(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Hello World")]
        render_tasks_table(tasks, {}, console=con)
        assert "Hello World" in buf.getvalue()

    def test_shows_task_id(self) -> None:
        con, buf = _console()
        tasks = [_task("abc123", "Foo")]
        render_tasks_table(tasks, {}, console=con)
        assert "abc123" in buf.getvalue()

    def test_shows_inbox_when_no_project(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Task", project_id=None)]
        render_tasks_table(tasks, {}, console=con)
        assert "Inbox" in buf.getvalue()

    def test_shows_project_name(self) -> None:
        con, buf = _console()
        projects = {"p1": _project("p1", "Engineering")}
        tasks = [_task("t1", "Task", project_id="p1")]
        render_tasks_table(tasks, projects, console=con)
        assert "Engineering" in buf.getvalue()

    def test_shows_flag_symbol(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Flagged", flagged=True)]
        render_tasks_table(tasks, {}, console=con)
        assert "★" in buf.getvalue()

    def test_shows_due_date(self) -> None:
        con, buf = _console()
        due = datetime(2026, 6, 15, 19, 0, 0)
        tasks = [_task("t1", "Due task", due=due)]
        render_tasks_table(tasks, {}, console=con)
        assert "06-15" in buf.getvalue()

    def test_shows_full_distinct_ids_for_same_name_tasks(self) -> None:
        con, buf = _console()
        tasks = [
            _task("weekly-review-1", "Weekly Review"),
            _task("weekly-review-2", "Weekly Review"),
        ]
        render_tasks_table(tasks, {}, console=con)
        output = buf.getvalue()
        assert "weekly-review-1" in output
        assert "weekly-review-2" in output

    def test_empty_task_list(self) -> None:
        con, buf = _console()
        render_tasks_table([], {}, console=con)
        # Should not raise

    def test_unicode_task_name(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Zrób zakupy 🛒")]
        render_tasks_table(tasks, {}, console=con)
        assert "Zrób zakupy" in buf.getvalue()


# ---------------------------------------------------------------------------
# render_tasks_json
# ---------------------------------------------------------------------------


class TestRenderTasksJson:
    def test_outputs_valid_json(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Hello")]
        render_tasks_json(tasks, console=con)
        parsed = json.loads(buf.getvalue())
        assert isinstance(parsed, list)
        assert len(parsed) == 1

    def test_task_fields_present(self) -> None:
        con, buf = _console()
        tasks = [_task("t1", "Test task")]
        render_tasks_json(tasks, console=con)
        data = json.loads(buf.getvalue())
        assert data[0]["id"] == "t1"
        assert data[0]["name"] == "Test task"

    def test_empty_list(self) -> None:
        con, buf = _console()
        render_tasks_json([], console=con)
        assert json.loads(buf.getvalue()) == []

    def test_repeated_names_keep_exact_ids_and_due_values(self) -> None:
        con, buf = _console()
        tasks = [
            _task("weekly-review-1", "Weekly Review", due=datetime(2026, 3, 22, 19, 0, 0)),
            _task("weekly-review-2", "Weekly Review", due=datetime(2026, 4, 5, 19, 0, 0)),
        ]
        render_tasks_json(tasks, console=con)
        data = json.loads(buf.getvalue())
        assert [task["id"] for task in data] == ["weekly-review-1", "weekly-review-2"]
        assert [task["due"] for task in data] == [
            "2026-03-22T19:00:00",
            "2026-04-05T19:00:00",
        ]


# ---------------------------------------------------------------------------
# render_project_tree
# ---------------------------------------------------------------------------


class TestRenderProjectTree:
    def test_renders_without_error(self) -> None:
        con, buf = _console()
        folders = {"f1": _folder()}
        projects = {"p1": _project("p1", "Alpha", folder_id="f1")}
        render_project_tree(folders, projects, console=con)
        assert "Alpha" in buf.getvalue()

    def test_shows_folder_name(self) -> None:
        con, buf = _console()
        folders = {"f1": _folder("f1", "Engineering")}
        render_project_tree(folders, {}, console=con)
        assert "Engineering" in buf.getvalue()

    def test_project_without_folder(self) -> None:
        con, buf = _console()
        projects = {"p1": _project("p1", "Orphan Project", folder_id=None)}
        render_project_tree({}, projects, console=con)
        assert "Orphan Project" in buf.getvalue()

    def test_status_filter_active_only(self) -> None:
        con, buf = _console()
        projects = {
            "p1": _project("p1", "Active Project", status="active"),
            "p2": _project("p2", "Inactive Project", status="inactive"),
        }
        render_project_tree({}, projects, status_filter="active", console=con)
        output = buf.getvalue()
        assert "Active Project" in output
        assert "Inactive Project" not in output

    def test_status_filter_all(self) -> None:
        con, buf = _console()
        projects = {
            "p1": _project("p1", "Active Project", status="active"),
            "p2": _project("p2", "Dropped Project", status="dropped"),
        }
        render_project_tree({}, projects, status_filter="all", console=con)
        output = buf.getvalue()
        assert "Active Project" in output
        assert "Dropped Project" in output

    def test_flagged_project_shows_star(self) -> None:
        con, buf = _console()
        projects = {"p1": _project("p1", "Flagged", flagged=True)}
        render_project_tree({}, projects, console=con)
        assert "★" in buf.getvalue()

    def test_nested_folders(self) -> None:
        con, buf = _console()
        folders = {
            "f1": _folder("f1", "Work"),
            "f2": Folder(
                id="f2",
                name="Engineering",
                parent_folder_id="f1",
                rank=200,
                added=NOW,
                modified=NOW,
            ),
        }
        render_project_tree(folders, {}, console=con)
        output = buf.getvalue()
        assert "Work" in output
        assert "Engineering" in output

    def test_empty_everything(self) -> None:
        con, _ = _console()
        render_project_tree({}, {}, console=con)
        # Must not raise


# ---------------------------------------------------------------------------
# render_projects_json
# ---------------------------------------------------------------------------


class TestRenderProjectsJson:
    def test_outputs_valid_json(self) -> None:
        con, buf = _console()
        projects = {"p1": _project("p1", "Alpha")}
        render_projects_json(projects, console=con)
        data = json.loads(buf.getvalue())
        assert isinstance(data, list)

    def test_project_fields(self) -> None:
        con, buf = _console()
        projects = {"p1": _project("p1", "Beta")}
        render_projects_json(projects, console=con)
        data = json.loads(buf.getvalue())
        assert data[0]["id"] == "p1"
        assert data[0]["name"] == "Beta"


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_project_name_inbox(self) -> None:
        assert _project_name(None, {}) == "Inbox"

    def test_project_name_known(self) -> None:
        projects = {"p1": _project("p1", "Work")}
        assert _project_name("p1", projects) == "Work"

    def test_project_name_unknown_id(self) -> None:
        result = _project_name("unknown_id", {})
        assert "unknown_id" in result

    def test_format_due_none(self) -> None:
        assert _format_due(None) == ""

    def test_format_due_today(self) -> None:
        today = datetime.today().replace(hour=19, minute=0, second=0, microsecond=0)
        result = _format_due(today)
        assert "yellow" in result or today.strftime("%m-%d") in result

    def test_format_due_past(self) -> None:
        past = datetime(2020, 1, 1, 19, 0, 0)
        result = _format_due(past)
        assert "red" in result or "01-01" in result

    def test_format_due_future(self) -> None:
        future = datetime(2099, 12, 31, 19, 0, 0)
        result = _format_due(future)
        assert "12-31" in result

    def test_project_icon_active(self) -> None:
        assert _project_icon("active") == "●"

    def test_project_icon_inactive(self) -> None:
        assert _project_icon("inactive") == "○"

    def test_project_icon_done(self) -> None:
        assert _project_icon("done") == "✓"

    def test_project_icon_dropped(self) -> None:
        assert _project_icon("dropped") == "✗"

    def test_project_icon_unknown(self) -> None:
        assert _project_icon("bogus") == "?"
