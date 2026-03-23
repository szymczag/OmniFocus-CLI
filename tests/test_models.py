"""Tests for :mod:`omnifocus.models`."""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from omnifocus.models import Folder, OFModel, Project, Tag, Task

NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


def _folder(fid: str = "f1", name: str = "Work") -> Folder:
    return Folder(
        id=fid,
        name=name,
        parent_folder_id=None,
        rank=100,
        added=NOW,
        modified=NOW,
    )


def _tag(tid: str = "t1", name: str = "@home") -> Tag:
    return Tag(id=tid, name=name, parent_tag_id=None, rank=10)


def _project(pid: str = "p1", name: str = "My Project", status: str = "active") -> Project:
    return Project(
        id=pid,
        name=name,
        folder_id=None,
        status=status,
        singleton=False,
        rank=1000,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )


def _task(
    tid: str = "t1",
    name: str = "Do thing",
    inbox: bool = False,
    project_id: str | None = "p1",
    completed: datetime | None = None,
    hidden: datetime | None = None,
    flagged: bool = False,
) -> Task:
    return Task(
        id=tid,
        name=name,
        parent_task_id=None,
        project_id=project_id,
        inbox=inbox,
        completed=completed,
        flagged=flagged,
        due=None,
        start=None,
        hidden=hidden,
        note="",
        rank=100,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )


class TestFolder:
    def test_fields(self) -> None:
        f = _folder()
        assert f.id == "f1"
        assert f.name == "Work"
        assert f.parent_folder_id is None
        assert f.rank == 100

    def test_frozen(self) -> None:
        f = _folder()

        with pytest.raises(FrozenInstanceError):
            f.name = "Other"  # type: ignore[misc]


class TestTag:
    def test_fields(self) -> None:
        t = _tag()
        assert t.id == "t1"
        assert t.name == "@home"
        assert t.parent_tag_id is None

    def test_child_tag(self) -> None:
        t = Tag(id="t2", name="@desk", parent_tag_id="t1", rank=20)
        assert t.parent_tag_id == "t1"


class TestProject:
    def test_active(self) -> None:
        p = _project()
        assert p.status == "active"
        assert not p.singleton

    def test_singleton(self) -> None:
        p = Project(
            id="p1",
            name="Single",
            folder_id=None,
            status="active",
            singleton=True,
            rank=1,
            added=NOW,
            modified=NOW,
            flagged=False,
            due=None,
            start=None,
            note="",
            completed=None,
        )
        assert p.singleton

    def test_tag_ids_default_empty(self) -> None:
        p = _project()
        assert p.tag_ids == ()


class TestTask:
    def test_fields(self) -> None:
        t = _task()
        assert t.name == "Do thing"
        assert not t.inbox
        assert t.completed is None

    def test_tag_ids_default_empty(self) -> None:
        t = _task()
        assert t.tag_ids == ()

    def test_order_default(self) -> None:
        t = _task()
        assert t.order == "parallel"


class TestOFModel:
    def _make_model(self) -> OFModel:
        model = OFModel()
        model.folders["f1"] = _folder()
        model.projects["p1"] = _project()
        model.tasks["t1"] = _task(inbox=False, project_id="p1")
        model.tasks["t2"] = _task(tid="t2", name="Inbox item", inbox=True, project_id=None)
        model.tasks["t3"] = _task(tid="t3", name="Done task", completed=NOW, project_id="p1")
        model.tasks["t4"] = _task(tid="t4", name="Hidden task", hidden=NOW, project_id="p1")
        model.tags["tag1"] = _tag()
        return model

    def test_active_tasks(self) -> None:
        model = self._make_model()
        active = model.active_tasks
        ids = {t.id for t in active}
        assert "t1" in ids
        assert "t2" in ids
        # completed and hidden should be excluded
        assert "t3" not in ids
        assert "t4" not in ids

    def test_inbox_tasks(self) -> None:
        model = self._make_model()
        inbox = model.inbox_tasks
        assert len(inbox) == 1
        assert inbox[0].id == "t2"

    def test_active_projects(self) -> None:
        model = self._make_model()
        model.projects["p2"] = _project(pid="p2", name="Inactive", status="inactive")
        active = model.active_projects
        assert len(active) == 1
        assert active[0].id == "p1"

    def test_empty_model(self) -> None:
        model = OFModel()
        assert model.active_tasks == []
        assert model.inbox_tasks == []
        assert model.active_projects == []
