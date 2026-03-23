"""Tests for :mod:`omnifocus.mcp_server`."""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnifocus.mcp_server import (
    _handle_add_project,
    _handle_add_task,
    _handle_complete_project,
    _handle_complete_task,
    _handle_get_task,
    _handle_list_folders,
    _handle_list_projects,
    _handle_list_tasks,
    _handle_search_tasks,
    _handle_sync_now,
    _handle_sync_status,
    _handle_update_project,
    _handle_update_task,
    _serialise,
    _task_summary,
    _text,
    call_tool,
    list_tools,
)
from omnifocus.models import Folder, OFModel, Project, Task

NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------


def _make_model() -> OFModel:
    model = OFModel()
    model.folders["f1"] = Folder(
        id="f1",
        name="Work",
        parent_folder_id=None,
        rank=100,
        added=NOW,
        modified=NOW,
    )
    model.projects["p1"] = Project(
        id="p1",
        name="Engineering",
        folder_id="f1",
        status="active",
        singleton=False,
        rank=100,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )
    model.tasks["t1"] = Task(
        id="t1",
        name="Write tests",
        parent_task_id="p1",
        project_id="p1",
        inbox=False,
        completed=None,
        flagged=True,
        due=datetime(2026, 4, 1, 19, 0, 0),
        start=None,
        hidden=None,
        note="Use pytest",
        rank=100,
        repetition_rule=None,
        estimated_minutes=60,
        added=NOW,
        modified=NOW,
    )
    model.tasks["t2"] = Task(
        id="t2",
        name="Buy milk",
        parent_task_id=None,
        project_id=None,
        inbox=True,
        completed=None,
        flagged=False,
        due=datetime.today().replace(hour=19, minute=0, second=0, microsecond=0),
        start=None,
        hidden=None,
        note="",
        rank=200,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )
    return model


def _mock_store(model: OFModel | None = None) -> MagicMock:
    m = MagicMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=None)
    m.load = AsyncMock(return_value=model or _make_model())
    m.add_task = AsyncMock(
        return_value={"status": "created", "task_id": "new-task", "name": "New task"}
    )
    m.complete_task = AsyncMock(
        return_value={"status": "completed", "task_id": "t1", "name": "Write tests"}
    )
    m.update_task = AsyncMock(
        return_value={"status": "updated", "task_id": "t1", "name": "Write tests"}
    )
    m.drop_task = AsyncMock(
        return_value={"status": "dropped", "task_id": "t1", "name": "Write tests"}
    )
    m.add_project = AsyncMock(
        return_value={
            "status": "created",
            "project_id": "new-project",
            "name": "New project",
        }
    )
    m.update_project = AsyncMock(
        return_value={"status": "updated", "project_id": "p1", "name": "Engineering"}
    )
    m.complete_project = AsyncMock(
        return_value={
            "status": "completed",
            "project_id": "p1",
            "name": "Engineering",
        }
    )
    m.drop_project = AsyncMock(
        return_value={
            "status": "dropped",
            "project_id": "p1",
            "name": "Engineering",
        }
    )
    m.invalidate_cache = MagicMock()
    m._client = MagicMock()
    m._client.put_file = AsyncMock(return_value=None)
    m.sync_status = AsyncMock(
        return_value={
            "last_synced": "2026-03-22T12:00:00+00:00",
            "cached": True,
            "cache_age_seconds": 5.0,
            "cache_valid": True,
        }
    )
    return m


def _parse_response(contents: list) -> Any:
    """Parse the JSON text from the first TextContent in a tool response."""
    assert contents
    return json.loads(contents[0].text)


# ---------------------------------------------------------------------------
# list_tools
# ---------------------------------------------------------------------------


class TestListTools:
    @pytest.mark.asyncio
    async def test_returns_thirteen_tools(self) -> None:
        tools = await list_tools()
        assert len(tools) == 13

    @pytest.mark.asyncio
    async def test_tool_names(self) -> None:
        tools = await list_tools()
        names = {t.name for t in tools}
        expected = {
            "list_tasks",
            "search_tasks",
            "get_task",
            "add_task",
            "complete_task",
            "update_task",
            "add_project",
            "update_project",
            "complete_project",
            "list_projects",
            "list_folders",
            "sync_now",
            "sync_status",
        }
        assert names == expected


# ---------------------------------------------------------------------------
# call_tool dispatch
# ---------------------------------------------------------------------------


class TestCallToolDispatch:
    @pytest.mark.asyncio
    async def test_unknown_tool_returns_error(self) -> None:
        result = await call_tool("nonexistent_tool", {})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_of_error_returned_as_error_dict(self) -> None:
        from omnifocus.errors import OFWebDAVError

        with patch(
            "omnifocus.mcp_server._load_model",
            AsyncMock(side_effect=OFWebDAVError("timeout")),
        ):
            result = await call_tool("list_tasks", {})
        data = _parse_response(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# list_tasks
# ---------------------------------------------------------------------------


class TestHandleListTasks:
    @pytest.mark.asyncio
    async def test_returns_all_active(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({})
        data = _parse_response(result)
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_inbox_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"inbox": True})
        data = _parse_response(result)
        assert all(t["inbox"] for t in data)

    @pytest.mark.asyncio
    async def test_flagged_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"flagged": True})
        data = _parse_response(result)
        assert all(t["flagged"] for t in data)

    @pytest.mark.asyncio
    async def test_today_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"today": True})
        data = _parse_response(result)
        # t2 has due=today, t1 has due=future
        assert len(data) == 1
        assert data[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_due_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"due": True})
        data = _parse_response(result)
        # Both t1 and t2 have due dates
        assert len(data) == 2

    @pytest.mark.asyncio
    async def test_project_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"project": "Engineering"})
        data = _parse_response(result)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_limit(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"limit": 1})
        data = _parse_response(result)
        assert len(data) == 1

    @pytest.mark.asyncio
    async def test_repeated_names_remain_distinct_by_id(self) -> None:
        model = _make_model()
        model.tasks["dup"] = dataclasses.replace(
            model.tasks["t1"],
            id="dup",
            name="Write tests",
            due=datetime(2026, 4, 2, 19, 0, 0),
        )
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_list_tasks({})
        data = _parse_response(result)
        repeated = [task for task in data if task["name"] == "Write tests"]
        assert {task["id"] for task in repeated} == {"t1", "dup"}


# ---------------------------------------------------------------------------
# search_tasks
# ---------------------------------------------------------------------------


class TestHandleSearchTasks:
    @pytest.mark.asyncio
    async def test_finds_by_name(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_search_tasks({"query": "milk"})
        data = _parse_response(result)
        assert len(data) >= 1
        assert data[0]["id"] == "t2"

    @pytest.mark.asyncio
    async def test_no_match_returns_empty(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_search_tasks({"query": "xyznonexistent"})
        data = _parse_response(result)
        assert data == []

    @pytest.mark.asyncio
    async def test_score_in_result(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_search_tasks({"query": "milk"})
        data = _parse_response(result)
        assert "score" in data[0]


# ---------------------------------------------------------------------------
# get_task
# ---------------------------------------------------------------------------


class TestHandleGetTask:
    @pytest.mark.asyncio
    async def test_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_task({"task_id": "t1"})
        data = _parse_response(result)
        assert data["id"] == "t1"
        assert data["name"] == "Write tests"

    @pytest.mark.asyncio
    async def test_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_task({"task_id": "notexist"})
        data = _parse_response(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# add_task
# ---------------------------------------------------------------------------


class TestHandleAddTask:
    @pytest.mark.asyncio
    async def test_add_to_inbox(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_task({"name": "New task"})
        data = _parse_response(result)
        assert data["status"] == "created"
        assert "task_id" in data
        mock.add_task.assert_awaited_once()
        mock._client.put_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_with_project(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_task(
                    {
                        "name": "Subtask",
                        "project": "Engineering",
                    }
                )
        data = _parse_response(result)
        assert data["status"] == "created"

    @pytest.mark.asyncio
    async def test_add_missing_name(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_task({})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_project_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_task({"name": "T", "project": "Nonexistent"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_with_due(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_task({"name": "T", "due": "today"})
        data = _parse_response(result)
        assert data["status"] == "created"

    @pytest.mark.asyncio
    async def test_add_with_iso_due(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_task({"name": "T", "due": "2099-12-31T19:00:00"})
        data = _parse_response(result)
        assert data["status"] == "created"

    @pytest.mark.asyncio
    async def test_add_invalid_due(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_task({"name": "T", "due": "notadate!!!"})
        data = _parse_response(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# complete_task
# ---------------------------------------------------------------------------


class TestHandleCompleteTask:
    @pytest.mark.asyncio
    async def test_complete_by_id(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_complete_task({"query": "t1"})
        data = _parse_response(result)
        assert data["status"] == "completed"
        mock.complete_task.assert_awaited_once()
        mock._client.put_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_complete_by_name(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_complete_task({"query": "Buy milk"})
        data = _parse_response(result)
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_complete_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_complete_task({"query": "zzznomatch"})
        data = _parse_response(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# update_task
# ---------------------------------------------------------------------------


class TestHandleUpdateTask:
    @pytest.mark.asyncio
    async def test_update_name(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task(
                    {
                        "task_id": "t1",
                        "name": "Updated name",
                    }
                )
        data = _parse_response(result)
        assert data["status"] == "updated"
        mock.update_task.assert_awaited_once()
        mock._client.put_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_update_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "notexist"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_flagged(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task(
                    {
                        "task_id": "t1",
                        "flagged": False,
                    }
                )
        data = _parse_response(result)
        assert data["status"] == "updated"

    @pytest.mark.asyncio
    async def test_update_due(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task(
                    {
                        "task_id": "t1",
                        "due": "2099-12-31T19:00:00",
                    }
                )
        data = _parse_response(result)
        assert data["status"] == "updated"

    @pytest.mark.asyncio
    async def test_update_clear_due(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "due": ""})
        data = _parse_response(result)
        assert data["status"] == "updated"

    @pytest.mark.asyncio
    async def test_update_defer_estimate_and_drop(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task(
                    {
                        "task_id": "t1",
                        "defer": "2099-12-30T19:00:00",
                        "estimate": 15,
                        "dropped": True,
                    }
                )
        data = _parse_response(result)
        assert data["status"] == "dropped"
        dropped_task = mock.drop_task.await_args.args[0]
        assert dropped_task.start == datetime(2099, 12, 30, 19, 0, 0)
        assert dropped_task.estimated_minutes == 15
        assert dropped_task.hidden is not None

    @pytest.mark.asyncio
    async def test_update_invalid_estimate_returns_error(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "t1", "estimate": "abc"})
        data = _parse_response(result)
        assert data["error"] == "Invalid estimate: 'abc'"

    @pytest.mark.asyncio
    async def test_update_empty_estimate_clears_estimate(self) -> None:
        model = _make_model()
        model.tasks["t1"] = dataclasses.replace(model.tasks["t1"], estimated_minutes=30)
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "estimate": ""})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.estimated_minutes is None

    @pytest.mark.asyncio
    async def test_update_dropped_false_clears_hidden(self) -> None:
        model = _make_model()
        model.tasks["t1"] = dataclasses.replace(model.tasks["t1"], hidden=NOW)
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "dropped": False})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.hidden is None


# ---------------------------------------------------------------------------
# project write tools
# ---------------------------------------------------------------------------


class TestHandleAddProject:
    @pytest.mark.asyncio
    async def test_add_project(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_project({"name": "Project", "folder": "Work"})
        data = _parse_response(result)
        assert data["status"] == "created"
        assert data["project_id"] == "new-project"
        mock.add_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_project_missing_name(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_project({})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_project_folder_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_project({"name": "Project", "folder": "Missing"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_project_folder_ambiguous(self) -> None:
        model = _make_model()
        model.folders["f2"] = Folder(
            id="f2",
            name="Work Extra",
            parent_folder_id=None,
            rank=200,
            added=NOW,
            modified=NOW,
        )
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_add_project({"name": "Project", "folder": "Work"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_project_invalid_due(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_project({"name": "Project", "due": "notadate"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_project_invalid_defer(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_project({"name": "Project", "defer": "notadate"})
        data = _parse_response(result)
        assert "error" in data


class TestHandleUpdateProject:
    @pytest.mark.asyncio
    async def test_update_project(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_project({"project_id": "p1", "name": "Updated"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        mock.update_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_update_project_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_project({"project_id": "missing"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_project_done_sets_completion(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                await _handle_update_project({"project_id": "p1", "status": "done"})
        updated_project = mock.complete_project.await_args.args[0]
        assert updated_project.status == "done"
        assert updated_project.completed is not None

    @pytest.mark.asyncio
    async def test_update_project_dropped_routes_to_drop(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_project({"project_id": "p1", "status": "dropped"})
        data = _parse_response(result)
        assert data["status"] == "dropped"
        dropped_project = mock.drop_project.await_args.args[0]
        assert dropped_project.status == "dropped"


class TestHandleCompleteProject:
    @pytest.mark.asyncio
    async def test_complete_project_by_id(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_complete_project({"query": "p1"})
        data = _parse_response(result)
        assert data["status"] == "completed"
        mock.complete_project.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_complete_project_by_name(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_complete_project({"query": "Engineering"})
        data = _parse_response(result)
        assert data["status"] == "completed"

    @pytest.mark.asyncio
    async def test_complete_project_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_complete_project({"query": "missing"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_complete_project_ambiguous(self) -> None:
        model = _make_model()
        model.projects["p2"] = dataclasses.replace(
            model.projects["p1"],
            id="p2",
            name="Engineering Extra",
        )
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_complete_project({"query": "Engineering"})
        data = _parse_response(result)
        assert "error" in data


# ---------------------------------------------------------------------------
# list_projects
# ---------------------------------------------------------------------------


class TestHandleListProjects:
    @pytest.mark.asyncio
    async def test_returns_active(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({})
        data = _parse_response(result)
        assert len(data) == 1
        assert data[0]["id"] == "p1"

    @pytest.mark.asyncio
    async def test_all_status(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({"status": "all"})
        data = _parse_response(result)
        assert len(data) >= 1


# ---------------------------------------------------------------------------
# list_folders
# ---------------------------------------------------------------------------


class TestHandleListFolders:
    @pytest.mark.asyncio
    async def test_returns_folders(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_folders({})
        data = _parse_response(result)
        assert len(data) == 1
        assert data[0]["id"] == "f1"


# ---------------------------------------------------------------------------
# sync_now / sync_status
# ---------------------------------------------------------------------------


class TestHandleSyncNow:
    @pytest.mark.asyncio
    async def test_returns_summary(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
            result = await _handle_sync_now({})
        data = _parse_response(result)
        assert data["status"] == "synced"
        assert "tasks" in data


class TestHandleSyncStatus:
    @pytest.mark.asyncio
    async def test_returns_status(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
            result = await _handle_sync_status({})
        data = _parse_response(result)
        assert "cached" in data
        assert data["cache_valid"] is True


# ---------------------------------------------------------------------------
# _serialise and _text
# ---------------------------------------------------------------------------


class TestSerialise:
    def test_datetime_to_iso(self) -> None:
        result = _serialise(NOW)
        assert isinstance(result, str)
        assert "2026" in result

    def test_dict_recursed(self) -> None:
        result = _serialise({"dt": NOW})
        assert isinstance(result["dt"], str)

    def test_list_recursed(self) -> None:
        result = _serialise([NOW, NOW])
        assert all(isinstance(x, str) for x in result)

    def test_plain_value(self) -> None:
        assert _serialise(42) == 42
        assert _serialise("hello") == "hello"

    def test_dataclass(self) -> None:
        folder = Folder(
            id="f1",
            name="Work",
            parent_folder_id=None,
            rank=100,
            added=NOW,
            modified=NOW,
        )
        result = _serialise(folder)
        assert isinstance(result, dict)
        assert result["id"] == "f1"


class TestText:
    def test_wraps_in_text_content(self) -> None:
        result = _text({"key": "value"})
        assert len(result) == 1
        assert result[0].type == "text"
        parsed = json.loads(result[0].text)
        assert parsed["key"] == "value"


# ---------------------------------------------------------------------------
# _task_summary
# ---------------------------------------------------------------------------


class TestTaskSummary:
    def test_includes_project_name(self) -> None:
        model = _make_model()
        summary = _task_summary(model.tasks["t1"], model)
        assert summary["project"] == "Engineering"

    def test_inbox_task_no_project(self) -> None:
        model = _make_model()
        summary = _task_summary(model.tasks["t2"], model)
        assert summary["project"] is None

    def test_includes_due(self) -> None:
        model = _make_model()
        summary = _task_summary(model.tasks["t1"], model)
        assert summary["due"] is not None


# Type annotation for _parse_response return
from typing import Any  # noqa: E402
