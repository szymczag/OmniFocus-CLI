"""Tests for :mod:`omnifocus.writer`."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime

from omnifocus.models import Project, Task
from omnifocus.writer import (
    TaskWriter,
    TransactionBuilder,
    _format_dt_local,
    _format_dt_utc,
    _format_ts,
    generate_id,
)

NOW = datetime(2026, 3, 22, 15, 40, 11, 347_000, tzinfo=UTC)
NS = "{http://www.omnigroup.com/namespace/OmniFocus/v2}"


# ---------------------------------------------------------------------------
# generate_id
# ---------------------------------------------------------------------------


class TestGenerateId:
    def test_length(self) -> None:
        # 8 bytes → 11 base64url chars (8*8 / 6 = 10.67, rounded up to 11 with padding stripped)
        eid = generate_id()
        assert 10 <= len(eid) <= 12

    def test_unique(self) -> None:
        ids = {generate_id() for _ in range(100)}
        assert len(ids) == 100

    def test_url_safe_chars(self) -> None:
        for _ in range(50):
            eid = generate_id()
            assert all(
                c in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for c in eid
            )


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


class TestTimestampFormatting:
    def test_format_ts(self) -> None:
        assert _format_ts(NOW) == "20260322T154011Z"

    def test_format_dt_utc(self) -> None:
        result = _format_dt_utc(NOW)
        assert result == "2026-03-22T15:40:11.347Z"

    def test_format_dt_local(self) -> None:
        local = datetime(2026, 6, 1, 19, 0, 0)
        result = _format_dt_local(local)
        assert result == "2026-06-01T19:00:00.000"


# ---------------------------------------------------------------------------
# TransactionBuilder
# ---------------------------------------------------------------------------


def _parse_transaction(xml_bytes: bytes) -> ET.Element:
    return ET.fromstring(xml_bytes.decode("utf-8"))  # noqa: S314


class TestTransactionBuilder:
    def test_empty_builder(self) -> None:
        builder = TransactionBuilder()
        xml = builder.to_xml_bytes()
        root = ET.fromstring(xml)  # noqa: S314
        assert root.tag == f"{NS}omnifocus"
        assert list(root) == []

    def test_add_task_element(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="abc123",
            name="Buy milk",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1000,
            added_dt=NOW,
            modified_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("id") == "abc123"
        name_el = task_el.find(f"{NS}name")
        assert name_el is not None and name_el.text == "Buy milk"

    def test_inbox_flag_in_xml(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x1",
            name="Inbox",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        inbox_el = task_el.find(f"{NS}inbox")
        assert inbox_el is not None and inbox_el.text == "true"

    def test_parent_task_idref(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="child1",
            name="Child",
            parent_task_id="parent1",
            inbox=False,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        parent_ref = task_el.find(f"{NS}task")
        assert parent_ref is not None
        assert parent_ref.get("idref") == "parent1"

    def test_no_parent_task_element(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x",
            name="Top",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        # There should be no <task idref="..."> child
        inner_task = task_el.find(f"{NS}task")
        assert inner_task is None

    def test_due_date_in_xml(self) -> None:
        builder = TransactionBuilder()
        due = datetime(2026, 6, 1, 19, 0, 0)
        builder.add_task(
            task_id="x",
            name="Due",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
            due_dt=due,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        due_el = task_el.find(f"{NS}due")
        assert due_el is not None and due_el.text == "2026-06-01T19:00:00.000"

    def test_xml_injection_in_name_escaped(self) -> None:
        """Task names with XML special chars must be escaped."""
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x",
            name="<script>alert(\"xss\")</script> & 'quote'",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
        )
        xml_bytes = builder.to_xml_bytes()
        # Must parse without error
        root = ET.fromstring(xml_bytes)  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        name_el = task_el.find(f"{NS}name")
        assert name_el is not None
        # The text should be the raw string (ElementTree unescapes)
        assert "<script>" in (name_el.text or "")

    def test_deletion_marker(self) -> None:
        builder = TransactionBuilder()
        builder.add_deletion("del1", NOW)
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("id") == "del1"
        # No <name> child = deletion
        assert task_el.find(f"{NS}name") is None

    def test_estimated_minutes_in_xml(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x",
            name="Timed",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
            estimated_minutes=90,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        em_el = task_el.find(f"{NS}estimated-minutes")
        assert em_el is not None and em_el.text == "90"

    def test_repetition_rule_in_xml(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x",
            name="Repeat",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
            repetition_rule="FREQ=DAILY",
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        rr_el = task_el.find(f"{NS}repetition-rule")
        assert rr_el is not None and rr_el.text == "FREQ=DAILY"

    def test_hidden_dt_in_xml(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="x",
            name="Hidden",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
            hidden_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        hidden_el = task_el.find(f"{NS}hidden")
        assert hidden_el is not None and hidden_el.text is not None

    def test_multiple_tasks_in_transaction(self) -> None:
        builder = TransactionBuilder()
        for i in range(3):
            builder.add_task(
                task_id=f"t{i}",
                name=f"Task {i}",
                parent_task_id=None,
                inbox=True,
                flagged=False,
                rank=i,
                added_dt=NOW,
                modified_dt=NOW,
            )
        root = _parse_transaction(builder.to_xml_bytes())
        assert len(list(root)) == 3

    def test_add_project_element(self) -> None:
        builder = TransactionBuilder()
        builder.add_project(
            project_id="proj1",
            name="Project One",
            folder_id="folder1",
            status="active",
            singleton=True,
            flagged=True,
            rank=100,
            added_dt=NOW,
            modified_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        project_el = task_el.find(f"{NS}project")
        assert project_el is not None
        folder_el = project_el.find(f"{NS}folder")
        assert folder_el is not None
        assert folder_el.get("idref") == "folder1"
        status_el = project_el.find(f"{NS}status")
        singleton_el = project_el.find(f"{NS}singleton")
        assert status_el is not None and status_el.text == "active"
        assert singleton_el is not None and singleton_el.text == "true"

    def test_task_includes_tag_contexts(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="abc123",
            name="Tagged task",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1000,
            added_dt=NOW,
            modified_dt=NOW,
            tag_ids=("tag1", "tag2"),
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        contexts = task_el.findall(f"{NS}context")
        assert [context.get("idref") for context in contexts] == ["tag1", "tag2"]

    def test_project_includes_tag_contexts(self) -> None:
        builder = TransactionBuilder()
        builder.add_project(
            project_id="proj1",
            name="Tagged project",
            folder_id=None,
            status="inactive",
            singleton=False,
            flagged=False,
            rank=100,
            added_dt=NOW,
            modified_dt=NOW,
            tag_ids=("tagA",),
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        contexts = task_el.findall(f"{NS}context")
        assert [context.get("idref") for context in contexts] == ["tagA"]


# ---------------------------------------------------------------------------
# TaskWriter
# ---------------------------------------------------------------------------


def _unzip_contents(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read("contents.xml").decode("utf-8")


class TestTaskWriter:
    def test_add_task_returns_tuple(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        fname, data, new_id = writer.add_task("Test task")
        assert fname.endswith(".zip")
        assert b"PK" == data[:2]
        assert len(new_id) >= 10

    def test_add_task_filename_format(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        fname, _, _ = writer.add_task("Test task")
        # Format: 20260322T154011Z=cli01+parent01.zip
        assert "=cli01+parent01.zip" in fname
        # Timestamp part must be numeric-ish
        ts_part = fname.split("=")[0]
        assert len(ts_part) == 16 and ts_part.endswith("Z")

    def test_add_task_xml_has_task_element(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        fname, data, new_id = writer.add_task("Buy bread", inbox=True, task_id="fixed123")
        xml = _unzip_contents(data)
        root = ET.fromstring(xml)  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("id") == "fixed123"
        name_el = task_el.find(f"{NS}name")
        assert name_el is not None and name_el.text == "Buy bread"

    def test_add_task_with_project_parent(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="p1")
        fname, data, _ = writer.add_task(
            "Sub task",
            parent_task_id="proj_abc",
            inbox=False,
        )
        xml = _unzip_contents(data)
        root = ET.fromstring(xml)  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        parent_ref = task_el.find(f"{NS}task")
        assert parent_ref is not None
        assert parent_ref.get("idref") == "proj_abc"

    def test_complete_task(self) -> None:
        task = Task(
            id="task001",
            name="Do the thing",
            parent_task_id="proj1",
            project_id="proj1",
            inbox=False,
            completed=None,
            flagged=True,
            due=None,
            start=None,
            hidden=None,
            note="",
            rank=500,
            repetition_rule=None,
            estimated_minutes=None,
            added=NOW,
            modified=NOW,
        )
        writer = TaskWriter(client_id="cli01", parent_id="p1")
        fname, data = writer.complete_task(task)
        xml = _unzip_contents(data)
        root = ET.fromstring(xml)  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        completed_el = task_el.find(f"{NS}completed")
        assert completed_el is not None and completed_el.text is not None
        assert completed_el.text.endswith("Z")

    def test_complete_task_filename(self) -> None:
        task = Task(
            id="t1",
            name="X",
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
        writer = TaskWriter(client_id="myid", parent_id="myid")
        fname, _ = writer.complete_task(task)
        assert "=myid+myid.zip" in fname

    def test_default_client_id_generated(self) -> None:
        writer = TaskWriter()
        assert len(writer._client_id) >= 10

    def test_add_project_returns_tuple(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        fname, data, project_id = writer.add_project("Launch")
        assert fname.endswith(".zip")
        assert b"PK" == data[:2]
        assert len(project_id) >= 10

    def test_add_project_filename_format(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        fname, _, _ = writer.add_project("Launch")
        assert "=cli01+parent01.zip" in fname

    def test_add_project_xml_has_project_payload(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        _, data, _ = writer.add_project(
            "Launch",
            project_id="proj-fixed",
            folder_id="folder-1",
            status="inactive",
            flagged=True,
        )
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("id") == "proj-fixed"
        project_el = task_el.find(f"{NS}project")
        assert project_el is not None
        assert project_el.find(f"{NS}status").text == "inactive"
        assert project_el.find(f"{NS}folder").get("idref") == "folder-1"

    def test_upsert_project_keeps_project_fields(self) -> None:
        project = Project(
            id="p1",
            name="Engineering",
            folder_id="f1",
            status="inactive",
            singleton=True,
            rank=200,
            added=NOW,
            modified=NOW,
            flagged=True,
            due=datetime(2026, 6, 1, 19, 0, 0),
            start=datetime(2026, 5, 1, 19, 0, 0),
            note="Keep fields",
            completed=None,
            tag_ids=("tag1",),
        )
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        _, data = writer.upsert_project(project, when=NOW)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.find(f"{NS}name").text == "Engineering"
        assert task_el.find(f"{NS}flagged").text == "true"
        assert task_el.find(f"{NS}note").text == "Keep fields"
        assert task_el.find(f"{NS}context").get("idref") == "tag1"
        project_el = task_el.find(f"{NS}project")
        assert project_el.find(f"{NS}status").text == "inactive"
        assert project_el.find(f"{NS}singleton").text == "true"

    def test_complete_project_sets_done_and_completed(self) -> None:
        project = Project(
            id="p1",
            name="Engineering",
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
        )
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        _, data = writer.complete_project(project)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        project_el = task_el.find(f"{NS}project")
        assert project_el is not None
        assert project_el.find(f"{NS}status").text == "done"
        completed_el = task_el.find(f"{NS}completed")
        assert completed_el is not None
        assert completed_el.text is not None and completed_el.text.endswith("Z")

    def test_upsert_task_includes_tag_contexts(self) -> None:
        task = Task(
            id="task001",
            name="Tagged task",
            parent_task_id=None,
            project_id=None,
            inbox=True,
            completed=None,
            flagged=False,
            due=None,
            start=None,
            hidden=None,
            note="",
            rank=500,
            repetition_rule=None,
            estimated_minutes=None,
            tag_ids=("tag1", "tag2"),
            added=NOW,
            modified=NOW,
        )
        writer = TaskWriter(client_id="cli01", parent_id="parent01")
        _, data = writer.upsert_task(task, when=NOW)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        contexts = task_el.findall(f"{NS}context")
        assert [context.get("idref") for context in contexts] == ["tag1", "tag2"]

    def test_writer_uses_explicit_parent_without_guessing(self) -> None:
        writer = TaskWriter(client_id="cli01", parent_id="remote-head-123")
        fname, _, _ = writer.add_task("Test task")
        assert "=cli01+remote-head-123.zip" in fname
