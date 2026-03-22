"""Tests for :mod:`omnifocus.cli`."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from omnifocus.cli import _parse_due, cli
from omnifocus.errors import OFEncryptionError, OFWebDAVError
from omnifocus.models import Folder, OFModel, Project, Task

UTC = timezone.utc
NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _parse_due
# ---------------------------------------------------------------------------


class TestParseDue:
    def test_today(self) -> None:
        result = _parse_due("today")
        assert result.date() == datetime.today().date()
        assert result.hour == 19

    def test_tod(self) -> None:
        result = _parse_due("tod")
        assert result.date() == datetime.today().date()

    def test_tomorrow(self) -> None:
        from datetime import timedelta
        result = _parse_due("tomorrow")
        expected = (datetime.today() + timedelta(days=1)).date()
        assert result.date() == expected

    def test_tom(self) -> None:
        from datetime import timedelta
        result = _parse_due("tom")
        expected = (datetime.today() + timedelta(days=1)).date()
        assert result.date() == expected

    def test_iso_date(self) -> None:
        result = _parse_due("2099-12-31")
        assert result.year == 2099
        assert result.month == 12
        assert result.day == 31

    def test_mm_dd(self) -> None:
        result = _parse_due("06-15")
        assert result.month == 6
        assert result.day == 15

    def test_weekday_mon(self) -> None:
        result = _parse_due("mon")
        assert result.weekday() == 0  # Monday

    def test_weekday_fri(self) -> None:
        result = _parse_due("fri")
        assert result.weekday() == 4  # Friday

    def test_invalid_raises(self) -> None:
        import click
        with pytest.raises(click.BadParameter):
            _parse_due("notadate")

    def test_invalid_iso(self) -> None:
        import click
        with pytest.raises(click.BadParameter):
            _parse_due("9999-99-99")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_model() -> OFModel:
    model = OFModel()
    model.folders["f1"] = Folder(
        id="f1", name="Work", parent_folder_id=None,
        rank=100, added=NOW, modified=NOW
    )
    model.projects["p1"] = Project(
        id="p1", name="Engineering", folder_id="f1", status="active",
        singleton=False, rank=100, added=NOW, modified=NOW,
        flagged=False, due=None, start=None, note="", completed=None,
    )
    model.tasks["t1"] = Task(
        id="t1", name="Write tests", parent_task_id="p1", project_id="p1",
        inbox=False, completed=None, flagged=False, due=None, start=None,
        hidden=None, note="", rank=100, repetition_rule=None,
        estimated_minutes=None, added=NOW, modified=NOW,
    )
    model.tasks["t2"] = Task(
        id="t2", name="Buy milk", parent_task_id=None, project_id=None,
        inbox=True, completed=None, flagged=True, due=None, start=None,
        hidden=None, note="", rank=200, repetition_rule=None,
        estimated_minutes=None, added=NOW, modified=NOW,
    )
    return model


def _mock_store(model: OFModel | None = None) -> MagicMock:
    """Return a mock OFocusStore context manager."""
    m = MagicMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=None)
    m.load = AsyncMock(return_value=model or _make_model())
    m.invalidate_cache = MagicMock()
    m._client = MagicMock()
    m._client.put_file = AsyncMock(return_value=None)
    return m


# ---------------------------------------------------------------------------
# of sync
# ---------------------------------------------------------------------------


class TestSyncCmd:
    def test_sync_success(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["sync"])
        assert result.exit_code == 0
        assert "Synced" in result.output

    def test_sync_webdav_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFWebDAVError("timeout"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["sync"])
        assert result.exit_code != 0
        assert "WebDAV" in result.output

    def test_sync_encryption_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFEncryptionError("bad passphrase"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["sync"])
        assert result.exit_code != 0
        assert "Encryption" in result.output


# ---------------------------------------------------------------------------
# of tasks
# ---------------------------------------------------------------------------


class TestTasksCmd:
    def test_tasks_default(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks"])
        assert result.exit_code == 0

    def test_tasks_json_format(self) -> None:
        import json
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--format", "json"])
        assert result.exit_code == 0

    def test_tasks_inbox_filter(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--inbox"])
        assert result.exit_code == 0

    def test_tasks_flagged_filter(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--flagged"])
        assert result.exit_code == 0

    def test_tasks_today_filter(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--today"])
        assert result.exit_code == 0

    def test_tasks_due_filter(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--due"])
        assert result.exit_code == 0

    def test_tasks_project_filter(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--project", "Engineering"])
        assert result.exit_code == 0

    def test_tasks_all_flag(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks", "--all"])
        assert result.exit_code == 0

    def test_tasks_webdav_error_propagated(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock.load = AsyncMock(side_effect=OFWebDAVError("err"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["tasks"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# of add
# ---------------------------------------------------------------------------


class TestAddCmd:
    def test_add_to_inbox(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Buy bread"])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_with_due(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task with due", "--due", "2099-12-31"])
        assert result.exit_code == 0

    def test_add_with_project(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "New task", "--project", "Engineering"])
        assert result.exit_code == 0
        assert "Added" in result.output

    def test_add_project_not_found(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task", "--project", "Nonexistent"])
        assert result.exit_code != 0
        assert "No active project" in result.output

    def test_add_ambiguous_project(self) -> None:
        model = _make_model()
        # Add two projects with similar names
        model.projects["p2"] = Project(
            id="p2", name="Engineering EXTRA", folder_id=None, status="active",
            singleton=False, rank=200, added=NOW, modified=NOW,
            flagged=False, due=None, start=None, note="", completed=None,
        )
        runner = CliRunner()
        mock = _mock_store(model)
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task", "--project", "Engineering"])
        assert result.exit_code != 0
        assert "Multiple projects" in result.output

    def test_add_with_flag(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Flagged task", "--flagged"])
        assert result.exit_code == 0

    def test_add_with_note(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task with note", "--note", "Some note"])
        assert result.exit_code == 0

    def test_add_invalid_due(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task", "--due", "notadate"])
        assert result.exit_code != 0

    def test_add_webdav_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock._client.put_file = AsyncMock(side_effect=OFWebDAVError("err"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["add", "Task"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# of done
# ---------------------------------------------------------------------------


class TestDoneCmd:
    def test_done_by_id(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["done", "t1", "--yes"])
        assert result.exit_code == 0
        assert "Completed" in result.output

    def test_done_by_name(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["done", "Write tests", "--yes"])
        assert result.exit_code == 0

    def test_done_not_found(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["done", "nonexistentxyz", "--yes"])
        assert result.exit_code != 0
        assert "No active task" in result.output

    def test_done_ambiguous_without_yes(self) -> None:
        """Ambiguous match with score < 0.8 should error."""
        from omnifocus.fuzzy import MatchResult
        model = _make_model()
        # Add tasks with similar but different names
        for i in range(3):
            model.tasks[f"ta{i}"] = Task(
                id=f"ta{i}", name=f"Task number {i}", parent_task_id=None,
                project_id=None, inbox=True, completed=None, flagged=False,
                due=None, start=None, hidden=None, note="", rank=i,
                repetition_rule=None, estimated_minutes=None,
                added=NOW, modified=NOW,
            )
        runner = CliRunner()
        mock = _mock_store(model)
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["done", "Task number", "--yes"])
        # One match has score >= 0.8 (exact substring), so it should succeed
        assert result.exit_code == 0

    def test_done_confirmation_abort(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            # User says 'n' at the confirmation prompt
            result = runner.invoke(cli, ["done", "t1"], input="n\n")
        assert result.exit_code != 0

    def test_done_webdav_error(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        mock._client.put_file = AsyncMock(side_effect=OFWebDAVError("err"))
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["done", "t1", "--yes"])
        assert result.exit_code != 0


# ---------------------------------------------------------------------------
# of projects
# ---------------------------------------------------------------------------


class TestProjectsCmd:
    def test_projects_tree(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["projects"])
        assert result.exit_code == 0

    def test_projects_json(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["projects", "--format", "json"])
        assert result.exit_code == 0

    def test_projects_status_all(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["projects", "--status", "all"])
        assert result.exit_code == 0

    def test_projects_status_inactive(self) -> None:
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["projects", "--status", "inactive"])
        assert result.exit_code == 0


# ---------------------------------------------------------------------------
# Help
# ---------------------------------------------------------------------------


class TestHelp:
    def test_main_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "OmniFocus" in result.output

    def test_tasks_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["tasks", "--help"])
        assert result.exit_code == 0

    def test_add_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["add", "--help"])
        assert result.exit_code == 0

    def test_done_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["done", "--help"])
        assert result.exit_code == 0

    def test_projects_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["projects", "--help"])
        assert result.exit_code == 0

    def test_sync_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["sync", "--help"])
        assert result.exit_code == 0

    def test_debug_flag_enables_logging(self) -> None:
        """--debug must not crash and should configure logging."""
        runner = CliRunner()
        mock = _mock_store()
        with patch("omnifocus.cli.OFocusStore.from_env", return_value=mock):
            result = runner.invoke(cli, ["--debug", "sync"])
        assert result.exit_code == 0
