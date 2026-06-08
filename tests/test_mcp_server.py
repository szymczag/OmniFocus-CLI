"""Tests for :mod:`omnifocus.mcp_server`."""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import asyncio
import dataclasses
import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnifocus.errors import OFError
from omnifocus.mcp_server import (
    _folder_summary,
    _handle_add_folder,
    _handle_add_project,
    _handle_add_tag,
    _handle_add_task,
    _handle_complete_project,
    _handle_complete_task,
    _handle_drop_folder,
    _handle_drop_tag,
    _handle_get_folder,
    _handle_get_folder_tree,
    _handle_get_project,
    _handle_get_tag,
    _handle_get_task,
    _handle_list_folders,
    _handle_list_projects,
    _handle_list_projects_for_review,
    _handle_list_tags,
    _handle_list_tasks,
    _handle_mark_project_reviewed,
    _handle_search_tasks,
    _handle_sync_now,
    _handle_update_folder,
    _handle_update_project,
    _handle_update_tag,
    _handle_update_task,
    _parse_optional_date,
    _parse_optional_utc_datetime,
    _project_review_sort_key,
    _project_summary,
    _serialise,
    _tag_summary,
    _task_summary,
    _text,
    _validate_folder_parent_change,
    _validate_tag_parent_change,
    call_tool,
    list_tools,
    main,
)
from omnifocus.models import Folder, OFModel, Project, Tag, Task

NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)
# Reference "today" for the due/today task filters. Chosen after t1's due date
# (2026-04-01) so that t1 reads as overdue while t2 is due exactly today.
TODAY = datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC)


@contextmanager
def _frozen_review_clock(model: OFModel) -> Iterator[None]:
    """Patch the loaded model and pin the review clock to ``NOW``.

    Review-due state is computed against ``datetime.now(UTC)`` in the service
    layer, so the fixture's review timestamps (which treat ``NOW`` as "today")
    only behave as intended when the clock is pinned. Pinning keeps the
    due/not-due split deterministic regardless of the real wall-clock date.
    """
    with (
        patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)),
        patch("omnifocus.api_service.datetime") as mock_datetime,
    ):
        mock_datetime.now.return_value = NOW
        yield


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
        last_review=datetime(2026, 2, 1, 12, 0, 0, tzinfo=UTC),
        next_review=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
        review_interval="@1m",
        tag_ids=("tag1",),
    )
    model.projects["p2"] = Project(
        id="p2",
        name="Operations",
        folder_id="f1",
        status="active",
        singleton=False,
        rank=200,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
        last_review=datetime(2026, 3, 15, 12, 0, 0, tzinfo=UTC),
        next_review=datetime(2026, 4, 15, 12, 0, 0, tzinfo=UTC),
        review_interval="@1m",
        tag_ids=("tag2",),
    )
    model.projects["p3"] = Project(
        id="p3",
        name="Dormant",
        folder_id="f1",
        status="inactive",
        singleton=False,
        rank=300,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
        last_review=None,
        next_review=None,
        review_interval="@1w",
    )
    model.tags["tag1"] = Tag(id="tag1", name="@home", parent_tag_id=None, rank=100)
    model.tags["tag2"] = Tag(id="tag2", name="@desk", parent_tag_id=None, rank=200)
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
        tag_ids=("tag1",),
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
        due=TODAY.replace(tzinfo=None, hour=19, minute=0, second=0, microsecond=0),
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
    m.mark_project_reviewed = AsyncMock(
        return_value={
            "status": "reviewed",
            "project_id": "p1",
            "name": "Engineering",
            "next_review_recalculated": True,
        }
    )
    m.drop_project = AsyncMock(
        return_value={
            "status": "dropped",
            "project_id": "p1",
            "name": "Engineering",
        }
    )
    m.add_folder = AsyncMock(
        return_value={"status": "created", "folder_id": "new-folder", "name": "Folder"}
    )
    m.update_folder = AsyncMock(
        return_value={"status": "updated", "folder_id": "f1", "name": "Work"}
    )
    m.drop_folder = AsyncMock(return_value={"status": "dropped", "folder_id": "f1", "name": "Work"})
    m.add_tag = AsyncMock(return_value={"status": "created", "tag_id": "tag3", "name": "@new"})
    m.update_tag = AsyncMock(return_value={"status": "updated", "tag_id": "tag1", "name": "@home"})
    m.drop_tag = AsyncMock(return_value={"status": "dropped", "tag_id": "tag1", "name": "@home"})
    m.invalidate_cache = MagicMock()
    m._client = MagicMock()
    m._client.put_file = AsyncMock(return_value=None)
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
    async def test_returns_twenty_five_tools(self) -> None:
        tools = await list_tools()
        assert len(tools) == 25

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
            "get_project",
            "add_project",
            "update_project",
            "complete_project",
            "list_projects",
            "list_projects_for_review",
            "mark_project_reviewed",
            "list_folders",
            "get_folder",
            "get_folder_tree",
            "add_folder",
            "update_folder",
            "drop_folder",
            "list_tags",
            "get_tag",
            "add_tag",
            "update_tag",
            "drop_tag",
            "sync_now",
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
        # The "today" filter compares due dates against datetime.today(); pin it
        # so the due-today (t2) and overdue (t1) split stays deterministic.
        with (
            patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())),
            patch("omnifocus.api_service.datetime") as mock_datetime,
        ):
            mock_datetime.today.return_value = TODAY
            result = await _handle_list_tasks({"today": True})
        data = _parse_response(result)
        # "today" includes tasks due today (t2) and overdue tasks (t1).
        assert {task["id"] for task in data} == {"t1", "t2"}

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
    async def test_tag_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"tag": "@home"})
        data = _parse_response(result)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_tag_id_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"tag_id": "tag1"})
        data = _parse_response(result)
        assert len(data) == 1
        assert data[0]["id"] == "t1"

    @pytest.mark.asyncio
    async def test_tag_id_filter_rejects_missing_tag(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_tasks({"tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"

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

    @pytest.mark.asyncio
    async def test_update_project_id_moves_task_into_project(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t2", "project_id": "p2"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.parent_task_id == "p2"
        assert updated_task.project_id == "p2"
        assert updated_task.inbox is False

    @pytest.mark.asyncio
    async def test_update_clear_project_moves_task_to_inbox(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "clear_project": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.parent_task_id is None
        assert updated_task.project_id is None
        assert updated_task.inbox is True

    @pytest.mark.asyncio
    async def test_update_inbox_true_moves_task_to_inbox(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "inbox": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.parent_task_id is None
        assert updated_task.project_id is None
        assert updated_task.inbox is True

    @pytest.mark.asyncio
    async def test_update_project_id_must_exist(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "t1", "project_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Project not found: missing"

    @pytest.mark.asyncio
    async def test_update_project_id_must_be_active(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "t1", "project_id": "p3"})
        data = _parse_response(result)
        assert data["error"] == "Project is not active: p3"

    @pytest.mark.asyncio
    async def test_update_project_id_conflicts_with_clear_project(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task(
                {"task_id": "t1", "project_id": "p2", "clear_project": True}
            )
        data = _parse_response(result)
        assert data["error"] == "project_id and clear_project cannot be combined"

    @pytest.mark.asyncio
    async def test_update_project_id_conflicts_with_inbox(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "t1", "project_id": "p2", "inbox": True})
        data = _parse_response(result)
        assert data["error"] == "project_id and inbox=true cannot be combined"

    @pytest.mark.asyncio
    async def test_update_clear_project_conflicts_with_inbox_false(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task(
                {"task_id": "t1", "clear_project": True, "inbox": False}
            )
        data = _parse_response(result)
        assert data["error"] == "clear_project cannot be combined with inbox=false"

    @pytest.mark.asyncio
    async def test_update_tag_ids_replace_tags(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "tag_ids": ["tag1", "tag2"]})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.tag_ids == ("tag1", "tag2")

    @pytest.mark.asyncio
    async def test_update_clear_tags_empties_tags(self) -> None:
        model = _make_model()
        model.tasks["t1"] = dataclasses.replace(model.tasks["t1"], tag_ids=("tag1",))
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_task({"task_id": "t1", "clear_tags": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated_task = mock.update_task.await_args.args[0]
        assert updated_task.tag_ids == ()

    @pytest.mark.asyncio
    async def test_update_unknown_tag_id_returns_error(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task({"task_id": "t1", "tag_ids": ["missing"]})
        data = _parse_response(result)
        assert data["error"] == "Unknown tag IDs: missing"

    @pytest.mark.asyncio
    async def test_update_clear_tags_conflicts_with_tag_ids(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_task(
                {"task_id": "t1", "tag_ids": ["tag1"], "clear_tags": True}
            )
        data = _parse_response(result)
        assert data["error"] == "tag_ids and clear_tags cannot be combined"


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
    async def test_update_project_sets_folder_id(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_project({"project_id": "p1", "folder_id": "f1"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_project.await_args.args[0]
        assert updated.folder_id == "f1"

    @pytest.mark.asyncio
    async def test_update_project_clears_folder(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_project({"project_id": "p1", "clear_folder": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_project.await_args.args[0]
        assert updated.folder_id is None

    @pytest.mark.asyncio
    async def test_update_project_rejects_folder_conflict(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_project(
                {"project_id": "p1", "folder_id": "f1", "clear_folder": True}
            )
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_project_rejects_unknown_folder(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_project({"project_id": "p1", "folder_id": "missing"})
        data = _parse_response(result)
        assert "error" in data

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
# get_project
# ---------------------------------------------------------------------------


class TestHandleGetProject:
    @pytest.mark.asyncio
    async def test_returns_project_summary_with_review_fields(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_project({"project_id": "p1"})
        data = _parse_response(result)
        assert data["id"] == "p1"
        assert data["review_interval"] == "@1m"
        assert data["review_basis"] == "next_review"

    @pytest.mark.asyncio
    async def test_missing_project_returns_error(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_project({"project_id": "missing"})
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
        assert {project["id"] for project in data} == {"p1", "p2"}
        assert data[0]["review_basis"] == "next_review"

    @pytest.mark.asyncio
    async def test_all_status(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({"status": "all"})
        data = _parse_response(result)
        assert len(data) >= 1

    @pytest.mark.asyncio
    async def test_tag_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({"tag": "@desk"})
        data = _parse_response(result)
        assert {project["id"] for project in data} == {"p2"}

    @pytest.mark.asyncio
    async def test_tag_id_filter(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({"tag_id": "tag1"})
        data = _parse_response(result)
        assert {project["id"] for project in data} == {"p1"}

    @pytest.mark.asyncio
    async def test_tag_id_filter_rejects_missing_tag(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_list_projects({"tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"


class TestHandleListProjectsForReview:
    @pytest.mark.asyncio
    async def test_returns_due_review_projects_only_by_default(self) -> None:
        with _frozen_review_clock(_make_model()):
            result = await _handle_list_projects_for_review({})
        data = _parse_response(result)
        assert [project["id"] for project in data] == ["p1", "p3"]
        assert all(project["review_due"] for project in data)

    @pytest.mark.asyncio
    async def test_can_include_non_due_review_projects(self) -> None:
        with _frozen_review_clock(_make_model()):
            result = await _handle_list_projects_for_review({"due_only": False})
        data = _parse_response(result)
        assert {project["id"] for project in data} == {"p1", "p2", "p3"}

    @pytest.mark.asyncio
    async def test_unknown_review_schedule_sorts_last(self) -> None:
        model = _make_model()
        model.projects["p4"] = dataclasses.replace(
            model.projects["p1"],
            id="p4",
            name="Unknown Schedule",
            last_review=None,
            next_review=None,
            review_interval="bogus",
        )
        with _frozen_review_clock(model):
            result = await _handle_list_projects_for_review({"due_only": False})
        data = _parse_response(result)
        assert data[-1]["id"] == "p4"
        assert data[-1]["review_basis"] == "unknown"


class TestHandleMarkProjectReviewed:
    @pytest.mark.asyncio
    async def test_marks_project_reviewed(self) -> None:
        model = _make_model()
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_mark_project_reviewed(
                    {"project_id": "p1", "reviewed_at": "2026-03-25T10:00:00+00:00"}
                )
        data = _parse_response(result)
        assert data["id"] == "p1"
        assert data["status"] == "reviewed"
        assert data["next_review_recalculated"] is True
        mock.mark_project_reviewed.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_marks_project_reviewed_with_naive_timestamp_as_utc(self) -> None:
        model = _make_model()
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_mark_project_reviewed(
                    {"project_id": "p1", "reviewed_at": "2026-03-25T10:00:00"}
                )
        data = _parse_response(result)
        assert data["last_review"] == "2026-03-25T10:00:00+00:00"
        called_reviewed_at = mock.mark_project_reviewed.await_args.kwargs["reviewed_at"]
        assert called_reviewed_at == datetime(2026, 3, 25, 10, 0, 0, tzinfo=UTC)

    @pytest.mark.asyncio
    async def test_invalid_reviewed_at_returns_error(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_mark_project_reviewed(
                {"project_id": "p1", "reviewed_at": "not-a-date"}
            )
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_missing_project_returns_error(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_mark_project_reviewed({"project_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Project not found: missing"


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


class TestHandleFolderTools:
    @pytest.mark.asyncio
    async def test_get_folder_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_folder({"folder_id": "missing"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_get_folder_returns_summary(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_folder({"folder_id": "f1"})
        data = _parse_response(result)
        assert data["id"] == "f1"
        assert "project_ids" in data

    @pytest.mark.asyncio
    async def test_get_folder_tree_returns_nested_data(self) -> None:
        model = _make_model()
        model.folders["f2"] = Folder(
            id="f2",
            name="Engineering Subfolder",
            parent_folder_id="f1",
            rank=200,
            added=NOW,
            modified=NOW,
        )
        model.projects["p1"] = dataclasses.replace(model.projects["p1"], folder_id="f2")
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_get_folder_tree({})
        data = _parse_response(result)
        assert data["folders"][0]["children"][0]["folder"]["id"] == "f2"

    @pytest.mark.asyncio
    async def test_add_folder(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_folder({"name": "Engineering", "parent_folder_id": "f1"})
        data = _parse_response(result)
        assert data["status"] == "created"
        mock.add_folder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_folder_missing_name(self) -> None:
        result = await _handle_add_folder({})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_add_folder_rejects_unknown_parent(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_folder(
                {"name": "Engineering", "parent_folder_id": "missing"}
            )
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_folder_renames_and_reparents(self) -> None:
        model = _make_model()
        model.folders["f2"] = Folder(
            id="f2",
            name="Ops",
            parent_folder_id=None,
            rank=200,
            added=NOW,
            modified=NOW,
        )
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_folder(
                    {"folder_id": "f1", "name": "Engineering", "parent_folder_id": "f2"}
                )
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_folder.await_args.args[0]
        assert updated.name == "Engineering"
        assert updated.parent_folder_id == "f2"

    @pytest.mark.asyncio
    async def test_update_folder_keeps_existing_parent_when_only_renaming(self) -> None:
        model = _make_model()
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_folder({"folder_id": "f1", "name": "Engineering"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_folder.await_args.args[0]
        assert updated.parent_folder_id == model.folders["f1"].parent_folder_id

    @pytest.mark.asyncio
    async def test_update_folder_rejects_cycle(self) -> None:
        model = _make_model()
        model.folders["f2"] = Folder(
            id="f2",
            name="Ops",
            parent_folder_id="f1",
            rank=200,
            added=NOW,
            modified=NOW,
        )
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_update_folder({"folder_id": "f1", "parent_folder_id": "f2"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_update_folder_clear_parent(self) -> None:
        model = _make_model()
        model.folders["f1"] = dataclasses.replace(model.folders["f1"], parent_folder_id="f2")
        model.folders["f2"] = Folder(
            id="f2",
            name="Ops",
            parent_folder_id=None,
            rank=200,
            added=NOW,
            modified=NOW,
        )
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_folder({"folder_id": "f1", "clear_parent": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_folder.await_args.args[0]
        assert updated.parent_folder_id is None

    @pytest.mark.asyncio
    async def test_update_folder_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_folder({"folder_id": "missing"})
        data = _parse_response(result)
        assert "error" in data

    @pytest.mark.asyncio
    async def test_drop_folder(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_drop_folder({"folder_id": "f1"})
        data = _parse_response(result)
        assert data["status"] == "dropped"
        mock.drop_folder.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drop_folder_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_drop_folder({"folder_id": "missing"})
        data = _parse_response(result)
        assert "error" in data


class TestHandleTagTools:
    @pytest.mark.asyncio
    async def test_list_tags_defaults_to_visible(self) -> None:
        model = _make_model()
        model.tags["tag2"] = dataclasses.replace(model.tags["tag2"], hidden=NOW)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_list_tags({})
        data = _parse_response(result)
        assert {tag["id"] for tag in data} == {"tag1"}

    @pytest.mark.asyncio
    async def test_list_tags_all_includes_hidden(self) -> None:
        model = _make_model()
        model.tags["tag2"] = dataclasses.replace(model.tags["tag2"], hidden=NOW)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            result = await _handle_list_tags({"all": True})
        data = _parse_response(result)
        assert {tag["id"] for tag in data} == {"tag1", "tag2"}

    @pytest.mark.asyncio
    async def test_get_tag(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_tag({"tag_id": "tag1"})
        data = _parse_response(result)
        assert data["id"] == "tag1"

    @pytest.mark.asyncio
    async def test_get_tag_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_get_tag({"tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"

    @pytest.mark.asyncio
    async def test_add_tag(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_add_tag({"name": "@new", "parent_tag_id": "tag1"})
        data = _parse_response(result)
        assert data["status"] == "created"
        mock.add_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_add_tag_missing_name(self) -> None:
        result = await _handle_add_tag({})
        data = _parse_response(result)
        assert data["error"] == "name is required"

    @pytest.mark.asyncio
    async def test_add_tag_rejects_unknown_parent(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_add_tag({"name": "@new", "parent_tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"

    @pytest.mark.asyncio
    async def test_update_tag(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_tag({"tag_id": "tag1", "name": "@house"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_tag.await_args.args[0]
        assert updated.name == "@house"

    @pytest.mark.asyncio
    async def test_update_tag_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_tag({"tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"

    @pytest.mark.asyncio
    async def test_update_tag_clear_parent(self) -> None:
        model = _make_model()
        model.tags["tag2"] = dataclasses.replace(model.tags["tag2"], parent_tag_id="tag1")
        mock = _mock_store(model)
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=model)):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_tag({"tag_id": "tag2", "clear_parent": True})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_tag.await_args.args[0]
        assert updated.parent_tag_id is None

    @pytest.mark.asyncio
    async def test_update_tag_sets_parent(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_update_tag({"tag_id": "tag2", "parent_tag_id": "tag1"})
        data = _parse_response(result)
        assert data["status"] == "updated"
        updated = mock.update_tag.await_args.args[0]
        assert updated.parent_tag_id == "tag1"

    @pytest.mark.asyncio
    async def test_update_tag_rejects_conflicting_parent_inputs(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_tag(
                {"tag_id": "tag1", "parent_tag_id": "tag2", "clear_parent": True}
            )
        data = _parse_response(result)
        assert data["error"] == "parent_tag_id and clear_parent cannot be combined"

    @pytest.mark.asyncio
    async def test_update_tag_rejects_unknown_parent(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_update_tag({"tag_id": "tag1", "parent_tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"

    @pytest.mark.asyncio
    async def test_drop_tag(self) -> None:
        mock = _mock_store()
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            with patch("omnifocus.mcp_server.OFocusStore.from_env", return_value=mock):
                result = await _handle_drop_tag({"tag_id": "tag1"})
        data = _parse_response(result)
        assert data["status"] == "dropped"
        mock.drop_tag.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_drop_tag_not_found(self) -> None:
        with patch("omnifocus.mcp_server._load_model", AsyncMock(return_value=_make_model())):
            result = await _handle_drop_tag({"tag_id": "missing"})
        data = _parse_response(result)
        assert data["error"] == "Tag not found: missing"


class TestCompatibilityWrappers:
    def test_project_summary_wrapper(self) -> None:
        model = _make_model()
        summary = _project_summary(model.projects["p1"], model, now=NOW)
        assert summary["id"] == "p1"
        assert summary["review_due"] is True

    def test_project_review_sort_key_wrapper(self) -> None:
        model = _make_model()
        summary = _project_summary(model.projects["p1"], model, now=NOW)
        from omnifocus.review import compute_project_review_state

        state = compute_project_review_state(model.projects["p1"], now=NOW)
        key = _project_review_sort_key(summary, state)
        assert isinstance(key, tuple)

    def test_folder_summary_wrapper(self) -> None:
        model = _make_model()
        summary = _folder_summary(model.folders["f1"], model)
        assert summary["id"] == "f1"
        assert summary["project_ids"] == ["p1", "p2", "p3"]

    def test_tag_summary_wrapper(self) -> None:
        model = _make_model()
        summary = _tag_summary(model.tags["tag1"], model)
        assert summary["id"] == "tag1"
        assert summary["parent_name"] is None

    def test_parse_optional_date_wrapper(self) -> None:
        assert _parse_optional_date("2026-04-06T12:00:00+00:00") == datetime(
            2026,
            4,
            6,
            12,
            0,
            0,
            tzinfo=UTC,
        )

    def test_parse_optional_utc_datetime_wrapper(self) -> None:
        assert _parse_optional_utc_datetime("2026-04-06T12:00:00+00:00") == datetime(
            2026,
            4,
            6,
            12,
            0,
            0,
            tzinfo=UTC,
        )


class TestValidateFolderParentChange:
    def test_conflicting_inputs(self) -> None:
        result = _validate_folder_parent_change(
            model=_make_model(),
            folder_id="f1",
            parent_folder_id="f1",
            clear_parent=True,
        )
        assert result is not None

    def test_none_parent_is_allowed(self) -> None:
        result = _validate_folder_parent_change(
            model=_make_model(),
            folder_id="f1",
            parent_folder_id=None,
            clear_parent=False,
        )
        assert result is None


class TestValidateTagParentChange:
    def test_conflicting_inputs(self) -> None:
        result = _validate_tag_parent_change(
            model=_make_model(),
            tag_id="tag1",
            parent_tag_id="tag2",
            clear_parent=True,
        )
        assert result == "parent_tag_id and clear_parent cannot be combined"

    def test_missing_parent_rejected(self) -> None:
        result = _validate_tag_parent_change(
            model=_make_model(),
            tag_id="tag1",
            parent_tag_id="missing",
            clear_parent=False,
        )
        assert result == "Tag not found: missing"

    def test_self_parent_rejected(self) -> None:
        result = _validate_tag_parent_change(
            model=_make_model(),
            tag_id="tag1",
            parent_tag_id="tag1",
            clear_parent=False,
        )
        assert result == "Tag cannot be its own parent"

    def test_cycle_rejected(self) -> None:
        model = _make_model()
        model.tags["tag3"] = Tag(id="tag3", name="@nested", parent_tag_id="tag1", rank=300)
        model.tags["tag1"] = dataclasses.replace(model.tags["tag1"], parent_tag_id="tag3")
        result = _validate_tag_parent_change(
            model=model,
            tag_id="tag3",
            parent_tag_id="tag1",
            clear_parent=False,
        )
        assert result == "Tag move would create a cycle"

    def test_valid_parent_chain_returns_none(self) -> None:
        result = _validate_tag_parent_change(
            model=_make_model(),
            tag_id="tag2",
            parent_tag_id="tag1",
            clear_parent=False,
        )
        assert result is None

    def test_missing_parent_is_rejected(self) -> None:
        result = _validate_folder_parent_change(
            model=_make_model(),
            folder_id="f1",
            parent_folder_id="missing",
            clear_parent=False,
        )
        assert result is not None

    def test_self_parent_is_rejected(self) -> None:
        result = _validate_folder_parent_change(
            model=_make_model(),
            folder_id="f1",
            parent_folder_id="f1",
            clear_parent=False,
        )
        assert result is not None


# ---------------------------------------------------------------------------
# sync_now
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


class TestCallToolErrors:
    @pytest.mark.asyncio
    async def test_call_tool_catches_of_error_from_handler(self) -> None:
        async def _boom(_args: dict[str, Any]) -> list[Any]:
            raise OFError("boom")

        with patch("omnifocus.mcp_server._handle_list_tasks", _boom):
            result = await call_tool("list_tasks", {})

        assert _parse_response(result)["error"] == "boom"


class TestMain:
    def test_main_runs_stdio_server(self) -> None:
        real_asyncio_run = asyncio.run
        run_mock = AsyncMock(return_value=None)

        class _FakeContext:
            async def __aenter__(self) -> tuple[str, str]:
                return ("reader", "writer")

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                return False

        with patch("omnifocus.mcp_server.stdio_server", return_value=_FakeContext()):
            with patch("omnifocus.mcp_server.server.run", run_mock):
                with patch(
                    "omnifocus.mcp_server.asyncio.run",
                    side_effect=real_asyncio_run,
                ) as run_async:
                    main()

        run_async.assert_called_once()
        run_mock.assert_awaited_once()


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
