"""Tests for :mod:`omnifocus.writer`."""

from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile
from datetime import UTC, datetime

from omnifocus.models import Project, Task
from omnifocus.writer import (
    AddProjectPlan,
    AddTaskPlan,
    TaskWriter,
    TransactionBuilder,
    WritePlan,
    WriterContext,
    _default_task_rank,
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
        assert _format_ts(NOW) == "20260322154011"

    def test_format_dt_utc(self) -> None:
        result = _format_dt_utc(NOW)
        assert result == "2026-03-22T15:40:11.347Z"

    def test_format_dt_local(self) -> None:
        local = datetime(2026, 6, 1, 19, 0, 0)
        result = _format_dt_local(local)
        assert result == "2026-06-01T19:00:00.000"

    def test_default_task_rank_for_inbox_uses_app_like_range(self) -> None:
        assert _default_task_rank(NOW, inbox=True) == 2147482994

    def test_default_task_rank_for_non_inbox_keeps_timestamp_shape(self) -> None:
        assert _default_task_rank(NOW, inbox=False) == int(NOW.timestamp() * 1000) & 0x7FFFFFFF


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

    def test_root_includes_writer_context_metadata(self) -> None:
        builder = TransactionBuilder(
            context=WriterContext(
                os_name="macOS",
                os_version="26.3.1",
                machine_model="Mac16,12",
            )
        )
        root = _parse_transaction(builder.to_xml_bytes())
        assert root.get("app-id") == "com.omnigroup.OmniFocus4"
        assert root.get("app-version") == "185.9.1"
        assert root.get("os-name") == "macOS"
        assert root.get("os-version") == "26.3.1"
        assert root.get("machine-model") == "Mac16,12"

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

    def test_planned_dt_in_xml(self) -> None:
        builder = TransactionBuilder()
        planned = datetime(2026, 6, 1, 19, 0, 0)
        builder._elements.append(
            builder._task_element(
                task_id="x",
                op=None,
                name="Planned",
                parent_task_id=None,
                inbox=True,
                flagged=False,
                rank=1,
                added_dt=NOW,
                modified_dt=NOW,
                due_dt=None,
                start_dt=None,
                planned_dt=planned,
                completed_dt=None,
                note="",
                order="parallel",
                estimated_minutes=None,
                repetition_rule=None,
                hidden_dt=None,
                project_xml="<project/>",
                tag_ids=(),
                include_snapshot_defaults=False,
            )
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        planned_el = task_el.find(f"{NS}planned")
        assert planned_el is not None and planned_el.text == "2026-06-01T19:00:00.000"

    def test_task_element_can_omit_added_and_modified(self) -> None:
        builder = TransactionBuilder()
        builder._elements.append(
            builder._task_element(
                task_id="x",
                op="update",
                name=None,
                parent_task_id=None,
                inbox=None,
                flagged=None,
                rank=None,
                added_dt=None,
                modified_dt=None,
                due_dt=None,
                start_dt=None,
                planned_dt=None,
                completed_dt=None,
                note=None,
                order=None,
                estimated_minutes=None,
                repetition_rule=None,
                hidden_dt=None,
                project_xml=None,
                tag_ids=(),
                include_snapshot_defaults=False,
            )
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.find(f"{NS}added") is None
        assert task_el.find(f"{NS}modified") is None
        assert task_el.find(f"{NS}name") is None

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

    def test_add_task_snapshot_matches_app_like_shape(self) -> None:
        builder = TransactionBuilder()
        builder.add_task_snapshot(
            task_id="abc123",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1000,
            added_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.find(f"{NS}modified") is None
        assert task_el.find(f"{NS}name").text is None
        assert task_el.find(f"{NS}project") is not None
        assert task_el.find(f"{NS}task") is not None
        assert task_el.find(f"{NS}context") is not None
        assert task_el.find(f"{NS}planned") is not None
        assert task_el.find(f"{NS}completed-by-children").text == "false"
        assert task_el.find(f"{NS}repetition-method") is not None
        assert task_el.find(f"{NS}next-clone-identifier").text == "0"
        assert task_el.find(f"{NS}order").text == "sequential"

    def test_add_task_snapshot_field_order_matches_app_capture(self) -> None:
        builder = TransactionBuilder()
        builder.add_task_snapshot(
            task_id="abc123",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1000,
            added_dt=NOW,
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert [child.tag.removeprefix(NS) for child in list(task_el[:10])] == [
            "project",
            "inbox",
            "task",
            "added",
            "name",
            "note",
            "rank",
            "hidden",
            "context",
            "start",
        ]

    def test_update_task_fields_renders_minimal_update_payload(self) -> None:
        builder = TransactionBuilder()
        builder.update_task_fields(
            task_id="abc123",
            added_dt=NOW,
            modified_dt=NOW,
            name="APP_SMOKE_1",
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("op") == "update"
        assert task_el.find(f"{NS}name").text == "APP_SMOKE_1"
        assert task_el.find(f"{NS}flagged") is None
        assert task_el.find(f"{NS}project") is None

    def test_add_delete_snapshot_renders_delete_snapshot_payload(self) -> None:
        task = Task(
            id="task-delete",
            name="Delete me",
            parent_task_id=None,
            project_id=None,
            inbox=True,
            completed=None,
            flagged=False,
            due=None,
            start=None,
            hidden=None,
            note="",
            rank=12,
            repetition_rule=None,
            estimated_minutes=None,
            added=NOW,
            modified=NOW,
        )
        builder = TransactionBuilder()
        builder.add_delete_snapshot(task)
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.get("op") == "delete"
        snapshot = task_el.find(f"{NS}delete-snapshot")
        assert snapshot is not None
        assert snapshot.find(f"{NS}inbox").text == "true"
        assert snapshot.find(f"{NS}name").text is None

    def test_add_task_renders_non_snapshot_repeat_and_alarm_fields(self) -> None:
        builder = TransactionBuilder()
        builder.add_task(
            task_id="repeat1",
            name="Repeated",
            parent_task_id=None,
            inbox=True,
            flagged=False,
            rank=1,
            added_dt=NOW,
            modified_dt=NOW,
            repetition_rule="FREQ=DAILY",
            repetition_method="fixed",
            repetition_schedule_type="start-after-completion",
            repetition_anchor_date="2026-03-22",
            catch_up_automatically=True,
            next_clone_identifier=3,
            due_date_alarm_policy="policy-due",
            defer_date_alarm_policy="policy-defer",
            latest_time_to_start_alarm_policy="policy-latest",
            planned_date_alarm_policy="policy-planned",
        )
        root = _parse_transaction(builder.to_xml_bytes())
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.find(f"{NS}repetition-method").text == "fixed"
        assert task_el.find(f"{NS}repetition-schedule-type").text == "start-after-completion"
        assert task_el.find(f"{NS}repetition-anchor-date").text == "2026-03-22"
        assert task_el.find(f"{NS}catch-up-automatically").text == "true"
        assert task_el.find(f"{NS}next-clone-identifier").text == "3"
        assert task_el.find(f"{NS}due-date-alarm-policy").text == "policy-due"
        assert task_el.find(f"{NS}defer-date-alarm-policy").text == "policy-defer"
        assert task_el.find(f"{NS}latest-time-to-start-alarm-policy").text == "policy-latest"
        assert task_el.find(f"{NS}planned-date-alarm-policy").text == "policy-planned"


# ---------------------------------------------------------------------------
# TaskWriter
# ---------------------------------------------------------------------------


def _unzip_contents(zip_bytes: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        return zf.read("contents.xml").decode("utf-8")


class TestTaskWriter:
    def test_empty_plan_iter_compatibility(self) -> None:
        assert tuple(iter(WritePlan(deltas=()))) == ()
        assert tuple(iter(AddTaskPlan(task_id="task1", deltas=()))) == (None, None, "task1")
        assert tuple(iter(AddProjectPlan(project_id="proj1", deltas=()))) == (
            None,
            None,
            "proj1",
        )

    def test_add_task_plan_iter_compatibility(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        filename, data, task_id = tuple(writer.add_task("Compat task"))
        assert filename is not None
        assert data is not None
        assert task_id is not None

    def test_add_task_returns_multi_delta_plan(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task("Test task")
        assert isinstance(plan, AddTaskPlan)
        assert len(plan.deltas) >= 2
        assert len(plan.task_id) >= 10
        assert all(delta.filename.endswith(".zip") for delta in plan.deltas)
        assert all(delta.data[:2] == b"PK" for delta in plan.deltas)

    def test_add_task_uses_parent_chaining_between_deltas(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task("Test task", chain_shape="linear")
        assert plan.deltas[0].parent_tail_id == "tail00"
        assert plan.deltas[1].parent_tail_id == plan.deltas[0].head_id
        assert plan.deltas[0].event_time < plan.deltas[1].event_time
        assert plan.deltas[-1].refresh_client_after is True

    def test_add_task_with_client_after_each_delta_marks_all_refreshes(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task("Test task", write_strategy="client_after_each_delta")
        assert all(delta.refresh_client_after for delta in plan.deltas)

    def test_add_task_defaults_to_app_rebase(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="accepted-tail")
        plan = writer.add_task("Test task")
        assert plan.deltas[0].head_id == "accepted-tail"

    def test_add_task_with_app_rebase_reuses_accepted_tail_as_first_head(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="accepted-tail")
        plan = writer.add_task("Test task", chain_shape="app_rebase")
        assert plan.deltas[0].head_id == "accepted-tail"
        assert plan.deltas[1].head_id == plan.deltas[0].parent_tail_id
        assert plan.deltas[1].parent_tail_id != "accepted-tail"

    def test_add_task_xml_has_skeleton_then_name_update(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task("Buy bread", inbox=True, task_id="fixed123", chain_shape="linear")
        skeleton = ET.fromstring(_unzip_contents(plan.deltas[0].data))  # noqa: S314
        update = ET.fromstring(_unzip_contents(plan.deltas[1].data))  # noqa: S314
        skeleton_task = skeleton.find(f"{NS}task")
        update_task = update.find(f"{NS}task")
        assert skeleton_task is not None
        assert update_task is not None
        assert skeleton_task.get("id") == "fixed123"
        assert skeleton_task.find(f"{NS}modified") is None
        assert skeleton_task.find(f"{NS}name").text is None
        assert skeleton_task.find(f"{NS}order").text == "sequential"
        assert update_task.get("op") == "update"
        assert update_task.find(f"{NS}name").text == "Buy bread"

    def test_add_task_appends_extra_update_deltas_for_optional_fields(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task(
            "Buy bread",
            note="Some note",
            flagged=True,
            due_dt=datetime(2026, 6, 1, 19, 0, 0),
            start_dt=datetime(2026, 5, 1, 19, 0, 0),
            estimated_minutes=45,
            chain_shape="linear",
        )
        assert len(plan.deltas) == 5

        note_xml = ET.fromstring(_unzip_contents(plan.deltas[2].data))  # noqa: S314
        note_task = note_xml.find(f"{NS}task")
        assert note_task is not None
        assert note_task.get("op") == "update"
        assert note_task.find(f"{NS}note").text == "Some note"

        flagged_xml = ET.fromstring(_unzip_contents(plan.deltas[3].data))  # noqa: S314
        flagged_task = flagged_xml.find(f"{NS}task")
        assert flagged_task is not None
        assert flagged_task.find(f"{NS}flagged").text == "true"

        extra_xml = ET.fromstring(_unzip_contents(plan.deltas[4].data))  # noqa: S314
        extra_task = extra_xml.find(f"{NS}task")
        assert extra_task is not None
        assert extra_task.find(f"{NS}due").text == "2026-06-01T19:00:00.000"
        assert extra_task.find(f"{NS}start").text == "2026-05-01T19:00:00.000"
        assert extra_task.find(f"{NS}estimated-minutes").text == "45"

    def test_add_task_app_rebase_relinks_optional_update_deltas(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="accepted-tail")
        plan = writer.add_task(
            "Buy bread",
            note="Some note",
            flagged=True,
            due_dt=datetime(2026, 6, 1, 19, 0, 0),
            start_dt=datetime(2026, 5, 1, 19, 0, 0),
            estimated_minutes=45,
            chain_shape="app_rebase",
        )
        assert len(plan.deltas) == 5
        assert plan.deltas[2].head_id == plan.deltas[1].parent_tail_id
        assert plan.deltas[2].parent_tail_id != plan.deltas[2].head_id
        assert plan.deltas[3].head_id == plan.deltas[2].parent_tail_id
        assert plan.deltas[4].head_id == plan.deltas[3].parent_tail_id

    def test_add_task_with_project_parent(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        plan = writer.add_task(
            "Sub task",
            parent_task_id="proj_abc",
            inbox=False,
            chain_shape="linear",
        )
        xml = _unzip_contents(plan.deltas[0].data)
        root = ET.fromstring(xml)  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        parent_ref = task_el.find(f"{NS}task")
        assert parent_ref is not None
        assert parent_ref.get("idref") == "proj_abc"
        assert task_el.find(f"{NS}order").text == "parallel"

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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        fname, _ = writer.complete_task(task)
        assert fname.endswith(".zip")
        assert "=tail00+" in fname

    def test_drop_task_marks_hidden(self) -> None:
        task = Task(
            id="drop1",
            name="Drop me",
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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        _, data = writer.drop_task(task)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        assert task_el.find(f"{NS}hidden") is not None

    def test_default_head_id_generated(self) -> None:
        writer = TaskWriter()
        assert len(writer._head_id) >= 10

    def test_add_project_returns_tuple(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        fname, data, project_id = writer.add_project("Launch")
        assert fname.endswith(".zip")
        assert b"PK" == data[:2]
        assert len(project_id) >= 10

    def test_add_project_filename_format(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        fname, _, _ = writer.add_project("Launch")
        assert fname.endswith(".zip")
        assert "=tail00+" in fname

    def test_add_project_xml_has_project_payload(self) -> None:
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
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

    def test_drop_project_sets_dropped_status(self) -> None:
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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        _, data = writer.drop_project(project)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        project_el = task_el.find(f"{NS}project")
        assert project_el is not None
        assert project_el.find(f"{NS}status").text == "dropped"

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
        writer = TaskWriter(head_id="tail01", parent_tail_id="tail00")
        _, data = writer.upsert_task(task, when=NOW)
        root = ET.fromstring(_unzip_contents(data))  # noqa: S314
        task_el = root.find(f"{NS}task")
        assert task_el is not None
        contexts = task_el.findall(f"{NS}context")
        assert [context.get("idref") for context in contexts] == ["tag1", "tag2"]

    def test_writer_uses_explicit_parent_without_guessing(self) -> None:
        writer = TaskWriter(head_id="new-tail-123", parent_tail_id="remote-tail-123")
        plan = writer.add_task("Test task")
        assert plan.deltas[0].head_id == "remote-tail-123"
