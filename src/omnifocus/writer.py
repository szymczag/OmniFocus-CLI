"""OmniFocus transaction writer.

Creates transaction ZIP archives in the format expected by the OmniFocus
sync protocol. Each transaction ZIP contains a single ``contents.xml`` file.

Delta filename format::

    <YYYYMMDDHHMMSS>=<new_tail_id>+<parent_tail_id>.zip

The writer supports both single-delta operations (task/project upserts) and
multi-delta flows for task creation that more closely match OmniFocus.app.
"""

from __future__ import annotations

import io
import os
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from omnifocus.models import Project, Task

_NS = "http://www.omnigroup.com/namespace/OmniFocus/v2"
_APP_ID = "com.omnigroup.OmniFocus4"
_APP_VERSION = "185.9.1"
_APP_LIKE_INBOX_RANK_BASE = 2_147_482_647
WriteStrategy = str
ChainShape = str


@dataclass(frozen=True)
class WriterContext:
    """Metadata used for transaction root headers."""

    os_name: str | None = None
    os_version: str | None = None
    machine_model: str | None = None


@dataclass(frozen=True)
class DeltaUpload:
    """A single delta ZIP ready to upload."""

    filename: str
    data: bytes
    head_id: str
    parent_tail_id: str
    event_time: datetime
    refresh_client_after: bool = False


@dataclass(frozen=True)
class WritePlan:
    """A generic delta publication plan."""

    deltas: tuple[DeltaUpload, ...]

    def __iter__(self) -> Iterator[object]:
        """Iterate like the legacy ``(filename, data)`` tuple API."""
        if not self.deltas:
            return iter(())
        first = self.deltas[0]
        return iter((first.filename, first.data))


@dataclass(frozen=True)
class AddTaskPlan(WritePlan):
    """A multi-delta task creation plan."""

    task_id: str

    def __iter__(self) -> Iterator[object]:
        """Iterate like the legacy ``(filename, data, task_id)`` tuple API."""
        if not self.deltas:
            return iter((None, None, self.task_id))
        first = self.deltas[0]
        return iter((first.filename, first.data, self.task_id))


@dataclass(frozen=True)
class AddProjectPlan(WritePlan):
    """A multi-delta project creation plan."""

    project_id: str

    def __iter__(self) -> Iterator[object]:
        """Iterate like the legacy ``(filename, data, project_id)`` tuple API."""
        if not self.deltas:
            return iter((None, None, self.project_id))
        first = self.deltas[0]
        return iter((first.filename, first.data, self.project_id))


def generate_id() -> str:
    """Generate a random OmniFocus-style identifier."""
    import base64

    raw = os.urandom(8)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(tz=UTC)


def _format_ts(dt: datetime) -> str:
    """Format a datetime as a compact UTC timestamp for use in filenames."""
    return dt.strftime("%Y%m%d%H%M%S")


def _format_dt_utc(dt: datetime) -> str:
    """Format a UTC datetime as ISO 8601 with milliseconds and Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _format_dt_local(dt: datetime) -> str:
    """Format a naive (local) datetime as ISO 8601 with milliseconds."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


def _default_task_rank(created_at: datetime, *, inbox: bool) -> int:
    """Return a default rank for a newly created task.

    OmniFocus.app creates new inbox tasks with ranks clustered near the top of
    the signed 32-bit range. Reproducing that shape is important for write
    interoperability with the app.
    """
    if not inbox:
        return int(created_at.timestamp() * 1000) & 0x7FFFFFFF
    return _APP_LIKE_INBOX_RANK_BASE + (created_at.microsecond // 1000)


class TransactionBuilder:
    """Build the XML for a single OmniFocus transaction document."""

    def __init__(self, context: WriterContext | None = None) -> None:
        self._elements: list[str] = []
        self._context = context or WriterContext()

    def _el(self, tag: str, children: list[str], extra_attrs: str = "") -> str:
        """Render an XML element with children."""
        inner = "".join(children)
        if not inner:
            return f"<{tag}{extra_attrs}/>"
        return f"<{tag}{extra_attrs}>{inner}</{tag}>"

    def _leaf(self, tag: str, text: str | None) -> str:
        """Render a leaf element. Empty or ``None`` text yields a self-closing tag."""
        if text is None or text == "":
            return f"<{tag}/>"
        escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f"<{tag}>{escaped}</{tag}>"

    def _project_container(
        self,
        *,
        folder_id: str | None,
        status: str,
        singleton: bool,
    ) -> str:
        """Render the nested ``<project>`` payload for project transactions."""
        children: list[str] = []
        if folder_id:
            children.append(f'<folder idref="{folder_id}"/>')
        children.append(self._leaf("status", status))
        children.append(self._leaf("singleton", "true" if singleton else "false"))
        return self._el("project", children)

    def _task_element(
        self,
        *,
        task_id: str,
        op: str | None,
        name: str | None,
        parent_task_id: str | None,
        inbox: bool | None,
        flagged: bool | None,
        rank: int | None,
        added_dt: datetime | None,
        modified_dt: datetime | None,
        due_dt: datetime | None,
        start_dt: datetime | None,
        planned_dt: datetime | None,
        completed_dt: datetime | None,
        note: str | None,
        order: str | None,
        estimated_minutes: int | None,
        repetition_rule: str | None = None,
        repetition_method: str | None = None,
        repetition_schedule_type: str | None = None,
        repetition_anchor_date: str | None = None,
        catch_up_automatically: bool | None = None,
        next_clone_identifier: int | None = None,
        due_date_alarm_policy: str | None = None,
        defer_date_alarm_policy: str | None = None,
        latest_time_to_start_alarm_policy: str | None = None,
        planned_date_alarm_policy: str | None = None,
        hidden_dt: datetime | None = None,
        project_xml: str | None = None,
        tag_ids: tuple[str, ...] = (),
        include_snapshot_defaults: bool = False,
    ) -> str:
        """Render a task XML element."""
        children: list[str] = []
        if project_xml is not None:
            children.append(project_xml)
        if inbox is not None:
            children.append(self._leaf("inbox", "true" if inbox else "false"))
        if parent_task_id is not None:
            children.append(f'<task idref="{parent_task_id}"/>')
        elif include_snapshot_defaults:
            children.append("<task/>")
        if added_dt is not None:
            children.append(self._leaf("added", _format_dt_utc(added_dt)))
        if modified_dt is not None:
            children.append(self._leaf("modified", _format_dt_utc(modified_dt)))
        if name is not None:
            children.append(self._leaf("name", name))
        if note is not None:
            children.append(self._leaf("note", note))
        if rank is not None:
            children.append(self._leaf("rank", str(rank)))
        if hidden_dt is not None:
            children.append(self._leaf("hidden", _format_dt_utc(hidden_dt)))
        elif include_snapshot_defaults:
            children.append("<hidden/>")
        if tag_ids:
            for tag_id in tag_ids:
                children.append(f'<context idref="{tag_id}"/>')
        elif include_snapshot_defaults:
            children.append("<context/>")
        if start_dt is not None:
            children.append(self._leaf("start", _format_dt_local(start_dt)))
        elif include_snapshot_defaults:
            children.append("<start/>")
        if planned_dt is not None:
            children.append(self._leaf("planned", _format_dt_local(planned_dt)))
        elif include_snapshot_defaults:
            children.append("<planned/>")
        if due_dt is not None:
            children.append(self._leaf("due", _format_dt_local(due_dt)))
        elif include_snapshot_defaults:
            children.append("<due/>")
        if completed_dt is not None:
            children.append(self._leaf("completed", _format_dt_utc(completed_dt)))
        elif include_snapshot_defaults:
            children.append("<completed/>")
        if estimated_minutes is not None:
            children.append(self._leaf("estimated-minutes", str(estimated_minutes)))
        elif include_snapshot_defaults:
            children.append("<estimated-minutes/>")
        if order is not None:
            children.append(self._leaf("order", order))
        if flagged is not None:
            children.append(self._leaf("flagged", "true" if flagged else "false"))
        if include_snapshot_defaults:
            children.append("<completed-by-children>false</completed-by-children>")
            children.append(self._leaf("repetition-rule", repetition_rule))
            children.append(self._leaf("repetition-method", repetition_method))
            children.append(self._leaf("repetition-schedule-type", repetition_schedule_type))
            children.append(self._leaf("repetition-anchor-date", repetition_anchor_date))
            children.append(
                self._leaf(
                    "catch-up-automatically",
                    "true" if catch_up_automatically else "false",
                )
            )
            children.append(
                self._leaf(
                    "next-clone-identifier",
                    str(0 if next_clone_identifier is None else next_clone_identifier),
                )
            )
            children.append(self._leaf("due-date-alarm-policy", due_date_alarm_policy))
            children.append(self._leaf("defer-date-alarm-policy", defer_date_alarm_policy))
            children.append(
                self._leaf(
                    "latest-time-to-start-alarm-policy",
                    latest_time_to_start_alarm_policy,
                )
            )
            children.append(self._leaf("planned-date-alarm-policy", planned_date_alarm_policy))
        else:
            if repetition_rule is not None:
                children.append(self._leaf("repetition-rule", repetition_rule))
            if repetition_method is not None:
                children.append(self._leaf("repetition-method", repetition_method))
            if repetition_schedule_type is not None:
                children.append(self._leaf("repetition-schedule-type", repetition_schedule_type))
            if repetition_anchor_date is not None:
                children.append(self._leaf("repetition-anchor-date", repetition_anchor_date))
            if catch_up_automatically is not None:
                children.append(
                    self._leaf(
                        "catch-up-automatically",
                        "true" if catch_up_automatically else "false",
                    )
                )
            if next_clone_identifier is not None:
                children.append(self._leaf("next-clone-identifier", str(next_clone_identifier)))
            if due_date_alarm_policy is not None:
                children.append(self._leaf("due-date-alarm-policy", due_date_alarm_policy))
            if defer_date_alarm_policy is not None:
                children.append(self._leaf("defer-date-alarm-policy", defer_date_alarm_policy))
            if latest_time_to_start_alarm_policy is not None:
                children.append(
                    self._leaf(
                        "latest-time-to-start-alarm-policy",
                        latest_time_to_start_alarm_policy,
                    )
                )
            if planned_date_alarm_policy is not None:
                children.append(self._leaf("planned-date-alarm-policy", planned_date_alarm_policy))

        op_attr = f' op="{op}"' if op else ""
        return f'<task id="{task_id}"{op_attr}>{"".join(children)}</task>'

    def add_task(
        self,
        task_id: str,
        name: str,
        parent_task_id: str | None,
        inbox: bool,
        flagged: bool,
        rank: int,
        added_dt: datetime,
        modified_dt: datetime | None = None,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        completed_dt: datetime | None = None,
        note: str = "",
        order: str = "parallel",
        estimated_minutes: int | None = None,
        repetition_rule: str | None = None,
        repetition_method: str | None = None,
        repetition_schedule_type: str | None = None,
        repetition_anchor_date: str | None = None,
        catch_up_automatically: bool = False,
        next_clone_identifier: int = 0,
        due_date_alarm_policy: str | None = None,
        defer_date_alarm_policy: str | None = None,
        latest_time_to_start_alarm_policy: str | None = None,
        planned_date_alarm_policy: str | None = None,
        hidden_dt: datetime | None = None,
        tag_ids: tuple[str, ...] = (),
    ) -> None:
        """Add a task element to the transaction."""
        self._elements.append(
            self._task_element(
                task_id=task_id,
                op=None,
                name=name,
                parent_task_id=parent_task_id,
                inbox=inbox,
                flagged=flagged,
                rank=rank,
                added_dt=added_dt,
                modified_dt=modified_dt,
                due_dt=due_dt,
                start_dt=start_dt,
                planned_dt=None,
                completed_dt=completed_dt,
                note=note,
                order=order,
                estimated_minutes=estimated_minutes,
                repetition_rule=repetition_rule,
                repetition_method=repetition_method,
                repetition_schedule_type=repetition_schedule_type,
                repetition_anchor_date=repetition_anchor_date,
                catch_up_automatically=catch_up_automatically,
                next_clone_identifier=next_clone_identifier,
                due_date_alarm_policy=due_date_alarm_policy,
                defer_date_alarm_policy=defer_date_alarm_policy,
                latest_time_to_start_alarm_policy=latest_time_to_start_alarm_policy,
                planned_date_alarm_policy=planned_date_alarm_policy,
                hidden_dt=hidden_dt,
                project_xml="<project/>",
                tag_ids=tag_ids,
                include_snapshot_defaults=False,
            )
        )

    def add_task_snapshot(
        self,
        *,
        task_id: str,
        parent_task_id: str | None,
        inbox: bool,
        flagged: bool,
        rank: int,
        added_dt: datetime,
        modified_dt: datetime | None = None,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        note: str = "",
        order: str = "sequential",
        estimated_minutes: int | None = None,
        repetition_rule: str | None = None,
        repetition_method: str | None = None,
        repetition_schedule_type: str | None = None,
        repetition_anchor_date: str | None = None,
        catch_up_automatically: bool = False,
        next_clone_identifier: int = 0,
        due_date_alarm_policy: str | None = None,
        defer_date_alarm_policy: str | None = None,
        latest_time_to_start_alarm_policy: str | None = None,
        planned_date_alarm_policy: str | None = None,
        hidden_dt: datetime | None = None,
        tag_ids: tuple[str, ...] = (),
    ) -> None:
        """Add an app-style skeleton task snapshot with an empty name."""
        self._elements.append(
            self._task_element(
                task_id=task_id,
                op=None,
                name="",
                parent_task_id=parent_task_id,
                inbox=inbox,
                flagged=flagged,
                rank=rank,
                added_dt=added_dt,
                modified_dt=modified_dt,
                due_dt=due_dt,
                start_dt=start_dt,
                planned_dt=None,
                completed_dt=None,
                note=note,
                order=order,
                estimated_minutes=estimated_minutes,
                repetition_rule=repetition_rule,
                repetition_method=repetition_method,
                repetition_schedule_type=repetition_schedule_type,
                repetition_anchor_date=repetition_anchor_date,
                catch_up_automatically=catch_up_automatically,
                next_clone_identifier=next_clone_identifier,
                due_date_alarm_policy=due_date_alarm_policy,
                defer_date_alarm_policy=defer_date_alarm_policy,
                latest_time_to_start_alarm_policy=latest_time_to_start_alarm_policy,
                planned_date_alarm_policy=planned_date_alarm_policy,
                hidden_dt=hidden_dt,
                project_xml="<project/>",
                tag_ids=tag_ids,
                include_snapshot_defaults=True,
            )
        )

    def update_task_fields(
        self,
        *,
        task_id: str,
        added_dt: datetime,
        modified_dt: datetime,
        name: str | None = None,
        note: str | None = None,
        flagged: bool | None = None,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        estimated_minutes: int | None = None,
        parent_task_id: str | None = None,
        inbox: bool | None = None,
        completed_dt: datetime | None = None,
        hidden_dt: datetime | None = None,
        tag_ids: tuple[str, ...] | None = None,
    ) -> None:
        """Add an ``op="update"`` task element with only changed fields."""
        self._elements.append(
            self._task_element(
                task_id=task_id,
                op="update",
                name=name,
                parent_task_id=parent_task_id,
                inbox=inbox,
                flagged=flagged,
                rank=None,
                added_dt=added_dt,
                modified_dt=modified_dt,
                due_dt=due_dt,
                start_dt=start_dt,
                planned_dt=None,
                completed_dt=completed_dt,
                note=note,
                order=None,
                estimated_minutes=estimated_minutes,
                repetition_rule=None,
                repetition_method=None,
                repetition_schedule_type=None,
                repetition_anchor_date=None,
                catch_up_automatically=None,
                next_clone_identifier=None,
                due_date_alarm_policy=None,
                defer_date_alarm_policy=None,
                latest_time_to_start_alarm_policy=None,
                planned_date_alarm_policy=None,
                hidden_dt=hidden_dt,
                project_xml=None,
                tag_ids=tag_ids or (),
                include_snapshot_defaults=False,
            )
        )

    def add_project(
        self,
        project_id: str,
        name: str,
        *,
        folder_id: str | None,
        status: str,
        singleton: bool,
        flagged: bool,
        rank: int,
        added_dt: datetime,
        modified_dt: datetime,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        completed_dt: datetime | None = None,
        note: str = "",
        tag_ids: tuple[str, ...] = (),
        repetition_rule: str | None = None,
        repetition_method: str | None = None,
        repetition_schedule_type: str | None = None,
        repetition_anchor_date: str | None = None,
        catch_up_automatically: bool = False,
        next_clone_identifier: int = 0,
        due_date_alarm_policy: str | None = None,
        defer_date_alarm_policy: str | None = None,
        latest_time_to_start_alarm_policy: str | None = None,
        planned_date_alarm_policy: str | None = None,
    ) -> None:
        """Add a project element to the transaction."""
        self._elements.append(
            self._task_element(
                task_id=project_id,
                op=None,
                name=name,
                parent_task_id=None,
                inbox=False,
                flagged=flagged,
                rank=rank,
                added_dt=added_dt,
                modified_dt=modified_dt,
                due_dt=due_dt,
                start_dt=start_dt,
                planned_dt=None,
                completed_dt=completed_dt,
                note=note,
                order="parallel",
                estimated_minutes=None,
                repetition_rule=repetition_rule,
                repetition_method=repetition_method,
                repetition_schedule_type=repetition_schedule_type,
                repetition_anchor_date=repetition_anchor_date,
                catch_up_automatically=catch_up_automatically,
                next_clone_identifier=next_clone_identifier,
                due_date_alarm_policy=due_date_alarm_policy,
                defer_date_alarm_policy=defer_date_alarm_policy,
                latest_time_to_start_alarm_policy=latest_time_to_start_alarm_policy,
                planned_date_alarm_policy=planned_date_alarm_policy,
                hidden_dt=None,
                project_xml=self._project_container(
                    folder_id=folder_id,
                    status=status,
                    singleton=singleton,
                ),
                tag_ids=tag_ids,
                include_snapshot_defaults=False,
            )
        )

    def add_deletion(self, task_id: str, deleted_dt: datetime) -> None:
        """Add a deletion marker for a task."""
        self._elements.append(
            f'<task id="{task_id}">{self._leaf("added", _format_dt_utc(deleted_dt))}</task>'
        )

    def add_delete_snapshot(self, task: Task) -> None:
        """Add an app-style delete snapshot for a task."""
        snapshot_builder = TransactionBuilder(context=self._context)
        snapshot_builder.add_task_snapshot(
            task_id=task.id,
            parent_task_id=task.parent_task_id,
            inbox=task.inbox,
            flagged=task.flagged,
            rank=task.rank,
            added_dt=task.added,
            modified_dt=task.modified,
            due_dt=task.due,
            start_dt=task.start,
            note=task.note,
            order=task.order,
            estimated_minutes=task.estimated_minutes,
            repetition_rule=task.repetition_rule,
            repetition_method=task.repetition_method,
            repetition_schedule_type=task.repetition_schedule_type,
            repetition_anchor_date=task.repetition_anchor_date,
            catch_up_automatically=task.catch_up_automatically,
            next_clone_identifier=task.next_clone_identifier,
            due_date_alarm_policy=task.due_date_alarm_policy,
            defer_date_alarm_policy=task.defer_date_alarm_policy,
            latest_time_to_start_alarm_policy=task.latest_time_to_start_alarm_policy,
            planned_date_alarm_policy=task.planned_date_alarm_policy,
            hidden_dt=task.hidden,
            tag_ids=task.tag_ids,
        )
        task_xml = snapshot_builder._elements[0]
        inner = task_xml.split(">", 1)[1].rsplit("</task>", 1)[0]
        self._elements.append(
            f'<task id="{task.id}" op="delete"><delete-snapshot>{inner}</delete-snapshot></task>'
        )

    def to_xml_bytes(self) -> bytes:
        """Serialise the transaction to UTF-8 XML bytes."""
        body = "".join(self._elements)
        attrs = [
            f'xmlns="{_NS}"',
            f'app-id="{_APP_ID}"',
            f'app-version="{_APP_VERSION}"',
        ]
        if self._context.os_name:
            attrs.append(f'os-name="{self._context.os_name}"')
        if self._context.os_version:
            attrs.append(f'os-version="{self._context.os_version}"')
        if self._context.machine_model:
            attrs.append(f'machine-model="{self._context.machine_model}"')
        doc = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<omnifocus {" ".join(attrs)}>'
            f"{body}"
            f"</omnifocus>"
        )
        return doc.encode("utf-8")


class TaskWriter:
    """High-level API for creating OmniFocus transactions."""

    def __init__(
        self,
        head_id: str | None = None,
        parent_tail_id: str | None = None,
        context: WriterContext | None = None,
    ) -> None:
        self._head_id = head_id or generate_id()
        self._parent_tail_id = parent_tail_id or self._head_id
        self._context = context or WriterContext()

    def _build_write_plan(
        self,
        *,
        builders: tuple[tuple[TransactionBuilder, datetime], ...],
        write_strategy: WriteStrategy,
        chain_shape: ChainShape,
    ) -> WritePlan:
        """Build a generic write plan from pre-rendered builders."""
        deltas: list[DeltaUpload] = []
        plan_ids = [generate_id() for _ in builders]
        for index, (builder, timestamp) in enumerate(builders):
            if chain_shape == "linear":
                head_id = plan_ids[index]
                parent_tail_id = self._parent_tail_id if index == 0 else plan_ids[index - 1]
            else:
                head_id = self._parent_tail_id if index == 0 else plan_ids[index - 1]
                parent_tail_id = plan_ids[index]
            filename, data = self._build_zip(builder, timestamp, head_id, parent_tail_id)
            deltas.append(
                DeltaUpload(
                    filename=filename,
                    data=data,
                    head_id=head_id,
                    parent_tail_id=parent_tail_id,
                    event_time=timestamp,
                    refresh_client_after=write_strategy == "client_after_each_delta",
                )
            )
        if write_strategy == "chain_then_client" and deltas:
            last = deltas[-1]
            deltas[-1] = DeltaUpload(
                filename=last.filename,
                data=last.data,
                head_id=last.head_id,
                parent_tail_id=last.parent_tail_id,
                event_time=last.event_time,
                refresh_client_after=True,
            )
        return WritePlan(deltas=tuple(deltas))

    def add_task(
        self,
        name: str,
        *,
        parent_task_id: str | None = None,
        inbox: bool = True,
        flagged: bool = False,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        note: str = "",
        estimated_minutes: int | None = None,
        task_id: str | None = None,
        rank: int | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> AddTaskPlan:
        """Create an app-style multi-delta plan that adds a new task."""
        new_id = task_id or generate_id()
        created_at = _now_utc()
        task_rank = rank if rank is not None else _default_task_rank(created_at, inbox=inbox)

        delta_index = 0
        builders: list[tuple[TransactionBuilder, datetime]] = []

        def _event_time() -> datetime:
            return created_at + timedelta(milliseconds=delta_index)

        def _next_second_time() -> datetime:
            nonlocal delta_index
            delta_index += 1
            return created_at + timedelta(seconds=delta_index)

        builder = TransactionBuilder(context=self._context)
        skeleton_time = _event_time()
        builder.add_task_snapshot(
            task_id=new_id,
            parent_task_id=parent_task_id,
            inbox=inbox,
            flagged=False,
            rank=task_rank,
            added_dt=created_at,
            order="sequential" if inbox else "parallel",
        )
        builders.append((builder, skeleton_time))

        update_builder = TransactionBuilder(context=self._context)
        name_time = _next_second_time()
        update_builder.update_task_fields(
            task_id=new_id,
            added_dt=created_at,
            modified_dt=name_time,
            name=name,
        )
        builders.append((update_builder, name_time))

        if note:
            note_builder = TransactionBuilder(context=self._context)
            note_time = _next_second_time()
            note_builder.update_task_fields(
                task_id=new_id,
                added_dt=created_at,
                modified_dt=note_time,
                note=note,
            )
            builders.append((note_builder, note_time))

        if flagged:
            flagged_builder = TransactionBuilder(context=self._context)
            flagged_time = _next_second_time()
            flagged_builder.update_task_fields(
                task_id=new_id,
                added_dt=created_at,
                modified_dt=flagged_time,
                flagged=True,
            )
            builders.append((flagged_builder, flagged_time))

        if due_dt is not None or start_dt is not None or estimated_minutes is not None:
            extra_builder = TransactionBuilder(context=self._context)
            extra_time = _next_second_time()
            extra_builder.update_task_fields(
                task_id=new_id,
                added_dt=created_at,
                modified_dt=extra_time,
                due_dt=due_dt,
                start_dt=start_dt,
                estimated_minutes=estimated_minutes,
            )
            builders.append((extra_builder, extra_time))

        plan = self._build_write_plan(
            builders=tuple(builders),
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )
        return AddTaskPlan(task_id=new_id, deltas=plan.deltas)

    def update_task(
        self,
        task: Task,
        *,
        when: datetime | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that upserts an existing task."""
        now = when or _now_utc()
        builder = TransactionBuilder(context=self._context)
        builder.add_task(
            task_id=task.id,
            name=task.name,
            parent_task_id=task.parent_task_id,
            inbox=task.inbox,
            flagged=task.flagged,
            rank=task.rank,
            added_dt=task.added,
            modified_dt=task.modified,
            due_dt=task.due,
            start_dt=task.start,
            completed_dt=task.completed,
            note=task.note,
            order=task.order,
            estimated_minutes=task.estimated_minutes,
            repetition_rule=task.repetition_rule,
            repetition_method=task.repetition_method,
            repetition_schedule_type=task.repetition_schedule_type,
            repetition_anchor_date=task.repetition_anchor_date,
            catch_up_automatically=task.catch_up_automatically,
            next_clone_identifier=task.next_clone_identifier,
            due_date_alarm_policy=task.due_date_alarm_policy,
            defer_date_alarm_policy=task.defer_date_alarm_policy,
            latest_time_to_start_alarm_policy=task.latest_time_to_start_alarm_policy,
            planned_date_alarm_policy=task.planned_date_alarm_policy,
            hidden_dt=task.hidden,
            tag_ids=task.tag_ids,
        )
        return self._build_write_plan(
            builders=((builder, now),),
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def upsert_task(
        self,
        task: Task,
        *,
        when: datetime | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Backward-compatible alias for :meth:`update_task`."""
        return self.update_task(
            task,
            when=when,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def complete_task(
        self,
        task: Task,
        *,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that marks *task* as completed."""
        now = _now_utc()
        return self.update_task(
            Task(
                id=task.id,
                name=task.name,
                parent_task_id=task.parent_task_id,
                project_id=task.project_id,
                inbox=task.inbox,
                completed=now,
                flagged=task.flagged,
                due=task.due,
                start=task.start,
                hidden=task.hidden,
                note=task.note,
                rank=task.rank,
                repetition_rule=task.repetition_rule,
                estimated_minutes=task.estimated_minutes,
                tag_ids=task.tag_ids,
                added=task.added,
                modified=now,
                order=task.order,
                repetition_method=task.repetition_method,
                repetition_schedule_type=task.repetition_schedule_type,
                repetition_anchor_date=task.repetition_anchor_date,
                catch_up_automatically=task.catch_up_automatically,
                next_clone_identifier=task.next_clone_identifier,
                due_date_alarm_policy=task.due_date_alarm_policy,
                defer_date_alarm_policy=task.defer_date_alarm_policy,
                latest_time_to_start_alarm_policy=task.latest_time_to_start_alarm_policy,
                planned_date_alarm_policy=task.planned_date_alarm_policy,
            ),
            when=now,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def drop_task(
        self,
        task: Task,
        *,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that marks *task* as dropped/hidden."""
        now = _now_utc()
        return self.update_task(
            Task(
                id=task.id,
                name=task.name,
                parent_task_id=task.parent_task_id,
                project_id=task.project_id,
                inbox=task.inbox,
                completed=task.completed,
                flagged=task.flagged,
                due=task.due,
                start=task.start,
                hidden=now,
                note=task.note,
                rank=task.rank,
                repetition_rule=task.repetition_rule,
                estimated_minutes=task.estimated_minutes,
                tag_ids=task.tag_ids,
                added=task.added,
                modified=now,
                order=task.order,
                repetition_method=task.repetition_method,
                repetition_schedule_type=task.repetition_schedule_type,
                repetition_anchor_date=task.repetition_anchor_date,
                catch_up_automatically=task.catch_up_automatically,
                next_clone_identifier=task.next_clone_identifier,
                due_date_alarm_policy=task.due_date_alarm_policy,
                defer_date_alarm_policy=task.defer_date_alarm_policy,
                latest_time_to_start_alarm_policy=task.latest_time_to_start_alarm_policy,
                planned_date_alarm_policy=task.planned_date_alarm_policy,
            ),
            when=now,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def add_project(
        self,
        name: str,
        *,
        folder_id: str | None = None,
        status: str = "active",
        flagged: bool = False,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        note: str = "",
        project_id: str | None = None,
        singleton: bool = False,
        rank: int | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> AddProjectPlan:
        """Create a write plan that adds a new project."""
        now = _now_utc()
        new_id = project_id or generate_id()
        project_rank = rank if rank is not None else int(now.timestamp() * 1000) & 0x7FFFFFFF
        builder = TransactionBuilder(context=self._context)
        builder.add_project(
            project_id=new_id,
            name=name,
            folder_id=folder_id,
            status=status,
            singleton=singleton,
            flagged=flagged,
            rank=project_rank,
            added_dt=now,
            modified_dt=now,
            due_dt=due_dt,
            start_dt=start_dt,
            note=note,
        )
        plan = self._build_write_plan(
            builders=((builder, now),),
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )
        return AddProjectPlan(project_id=new_id, deltas=plan.deltas)

    def update_project(
        self,
        project: Project,
        *,
        when: datetime | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that upserts an existing project."""
        now = when or _now_utc()
        builder = TransactionBuilder(context=self._context)
        builder.add_project(
            project_id=project.id,
            name=project.name,
            folder_id=project.folder_id,
            status=project.status,
            singleton=project.singleton,
            flagged=project.flagged,
            rank=project.rank,
            added_dt=project.added,
            modified_dt=project.modified,
            due_dt=project.due,
            start_dt=project.start,
            completed_dt=project.completed,
            note=project.note,
            tag_ids=project.tag_ids,
            repetition_rule=project.repetition_rule,
            repetition_method=project.repetition_method,
            repetition_schedule_type=project.repetition_schedule_type,
            repetition_anchor_date=project.repetition_anchor_date,
            catch_up_automatically=project.catch_up_automatically,
            next_clone_identifier=project.next_clone_identifier,
            due_date_alarm_policy=project.due_date_alarm_policy,
            defer_date_alarm_policy=project.defer_date_alarm_policy,
            latest_time_to_start_alarm_policy=project.latest_time_to_start_alarm_policy,
            planned_date_alarm_policy=project.planned_date_alarm_policy,
        )
        return self._build_write_plan(
            builders=((builder, now),),
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def upsert_project(
        self,
        project: Project,
        *,
        when: datetime | None = None,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Backward-compatible alias for :meth:`update_project`."""
        return self.update_project(
            project,
            when=when,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def complete_project(
        self,
        project: Project,
        *,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that marks a project as completed."""
        now = _now_utc()
        updated = Project(
            id=project.id,
            name=project.name,
            folder_id=project.folder_id,
            status="done",
            singleton=project.singleton,
            rank=project.rank,
            added=project.added,
            modified=now,
            flagged=project.flagged,
            due=project.due,
            start=project.start,
            note=project.note,
            completed=now,
            tag_ids=project.tag_ids,
            repetition_rule=project.repetition_rule,
            repetition_method=project.repetition_method,
            repetition_schedule_type=project.repetition_schedule_type,
            repetition_anchor_date=project.repetition_anchor_date,
            catch_up_automatically=project.catch_up_automatically,
            next_clone_identifier=project.next_clone_identifier,
            due_date_alarm_policy=project.due_date_alarm_policy,
            defer_date_alarm_policy=project.defer_date_alarm_policy,
            latest_time_to_start_alarm_policy=project.latest_time_to_start_alarm_policy,
            planned_date_alarm_policy=project.planned_date_alarm_policy,
        )
        return self.update_project(
            updated,
            when=now,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def drop_project(
        self,
        project: Project,
        *,
        write_strategy: WriteStrategy = "client_after_each_delta",
        chain_shape: ChainShape = "app_rebase",
    ) -> WritePlan:
        """Create a write plan that marks a project as dropped."""
        now = _now_utc()
        updated = Project(
            id=project.id,
            name=project.name,
            folder_id=project.folder_id,
            status="dropped",
            singleton=project.singleton,
            rank=project.rank,
            added=project.added,
            modified=now,
            flagged=project.flagged,
            due=project.due,
            start=project.start,
            note=project.note,
            completed=project.completed,
            tag_ids=project.tag_ids,
            repetition_rule=project.repetition_rule,
            repetition_method=project.repetition_method,
            repetition_schedule_type=project.repetition_schedule_type,
            repetition_anchor_date=project.repetition_anchor_date,
            catch_up_automatically=project.catch_up_automatically,
            next_clone_identifier=project.next_clone_identifier,
            due_date_alarm_policy=project.due_date_alarm_policy,
            defer_date_alarm_policy=project.defer_date_alarm_policy,
            latest_time_to_start_alarm_policy=project.latest_time_to_start_alarm_policy,
            planned_date_alarm_policy=project.planned_date_alarm_policy,
        )
        return self.update_project(
            updated,
            when=now,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )

    def _build_zip(
        self,
        builder: TransactionBuilder,
        ts: datetime,
        head_id: str,
        parent_tail_id: str,
    ) -> tuple[str, bytes]:
        """Serialise *builder* content into a ZIP archive."""
        xml_bytes = builder.to_xml_bytes()
        filename = f"{_format_ts(ts)}={head_id}+{parent_tail_id}.zip"
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("contents.xml", xml_bytes)
        return filename, buf.getvalue()
