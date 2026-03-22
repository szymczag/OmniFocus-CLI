"""OmniFocus transaction writer.

Creates transaction ZIP archives in the format expected by the OmniFocus
sync protocol.  Each transaction ZIP contains a single ``contents.xml`` file
with an ``<omnifocus>`` root element holding only the changed elements.

Transaction filename format::

    <UTC_ISO8601>=<client_id>+<parent_id>.zip

where ``<UTC_ISO8601>`` is a compact timestamp like ``20260322T154011Z``.

Usage::

    from omnifocus.writer import TaskWriter

    writer = TaskWriter(client_id="myCLI01")

    # Build an "add task" transaction
    fname, data = writer.add_task(
        name="Buy milk",
        inbox=True,
        flagged=False,
    )
    # fname = "20260322T154011Z=myCLI01+<parent_id>.zip"
"""

from __future__ import annotations

import io
import os
import zipfile
from datetime import datetime, timezone
from typing import Optional

from omnifocus.models import Task

# OmniFocus v2 XML namespace
_NS = "http://www.omnigroup.com/namespace/OmniFocus/v2"

# App metadata embedded in every transaction header
_APP_ID = "com.omnigroup.OmniFocus4"
_APP_VERSION = "185.9.1"


def generate_id() -> str:
    """Generate a random OmniFocus-style identifier.

    OmniFocus uses URL-safe base64-encoded random bytes (11 characters).
    We use 8 random bytes encoded as URL-safe base64 with padding stripped,
    which yields an 11-character string matching the observed format.

    Returns:
        An 11-character alphanumeric-plus identifier string.
    """
    import base64
    raw = os.urandom(8)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _now_utc() -> datetime:
    """Return the current UTC datetime."""
    return datetime.now(tz=timezone.utc)


def _format_ts(dt: datetime) -> str:
    """Format a datetime as a compact UTC timestamp for use in filenames.

    Args:
        dt: A UTC-aware datetime.

    Returns:
        String like ``"20260322T154011Z"``.
    """
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _format_dt_utc(dt: datetime) -> str:
    """Format a UTC datetime as ISO 8601 with milliseconds and Z suffix."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _format_dt_local(dt: datetime) -> str:
    """Format a naive (local) datetime as ISO 8601 with milliseconds."""
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"


class TransactionBuilder:
    """Builds the XML for a single OmniFocus transaction document.

    Usage::

        builder = TransactionBuilder()
        builder.add_element(elem)
        xml_bytes = builder.to_xml_bytes()
    """

    def __init__(self) -> None:
        self._elements: list[str] = []

    def _el(self, tag: str, children: list[str], extra_attrs: str = "") -> str:
        """Render an XML element with children."""
        inner = "".join(children)
        if not inner:
            return f"<{tag}{extra_attrs}/>"
        return f"<{tag}{extra_attrs}>{inner}</{tag}>"

    def _leaf(self, tag: str, text: str | None) -> str:
        """Render a leaf element.  Empty string text → self-closing tag."""
        if text is None or text == "":
            return f"<{tag}/>"
        escaped = (
            text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
        )
        return f"<{tag}>{escaped}</{tag}>"

    def add_task(
        self,
        task_id: str,
        name: str,
        parent_task_id: Optional[str],
        inbox: bool,
        flagged: bool,
        rank: int,
        added_dt: datetime,
        modified_dt: datetime,
        due_dt: Optional[datetime] = None,
        start_dt: Optional[datetime] = None,
        completed_dt: Optional[datetime] = None,
        note: str = "",
        order: str = "parallel",
        estimated_minutes: Optional[int] = None,
        repetition_rule: Optional[str] = None,
        hidden_dt: Optional[datetime] = None,
    ) -> None:
        """Add a task element to the transaction.

        Args:
            task_id: The new task's unique identifier.
            name: Display name.
            parent_task_id: Parent task id, or ``None`` for top-level inbox tasks.
            inbox: Whether this is an inbox task.
            flagged: Whether the task is flagged.
            rank: Sort key.
            added_dt: UTC creation timestamp.
            modified_dt: UTC modification timestamp.
            due_dt: Optional local-time due datetime.
            start_dt: Optional local-time defer datetime.
            completed_dt: Optional UTC completion timestamp.
            note: Plain-text note.
            order: ``"sequential"`` or ``"parallel"``.
            estimated_minutes: Optional duration estimate.
            repetition_rule: RFC 5545 RRULE string, or ``None``.
            hidden_dt: Optional UTC hidden/dropped timestamp.
        """
        children: list[str] = []
        children.append("<project/>")
        if parent_task_id:
            children.append(f'<task idref="{parent_task_id}"/>')
        children.append(self._leaf("inbox", "true" if inbox else "false"))
        children.append(self._leaf("added", _format_dt_utc(added_dt)))
        children.append(self._leaf("name", name))
        children.append(self._leaf("note", note))
        children.append(self._leaf("rank", str(rank)))
        children.append(self._leaf("flagged", "true" if flagged else "false"))
        children.append(self._leaf("due", _format_dt_local(due_dt) if due_dt else ""))
        children.append(self._leaf("start", _format_dt_local(start_dt) if start_dt else ""))
        children.append(
            self._leaf(
                "completed",
                _format_dt_utc(completed_dt) if completed_dt else "",
            )
        )
        if hidden_dt:
            children.append(self._leaf("hidden", _format_dt_utc(hidden_dt)))
        else:
            children.append("<hidden/>")
        if estimated_minutes is not None:
            children.append(self._leaf("estimated-minutes", str(estimated_minutes)))
        else:
            children.append("<estimated-minutes/>")
        if repetition_rule:
            children.append(self._leaf("repetition-rule", repetition_rule))
        children.append(self._leaf("order", order))
        children.append(self._leaf("modified", _format_dt_utc(modified_dt)))

        self._elements.append(
            f'<task id="{task_id}">{"".join(children)}</task>'
        )

    def add_deletion(self, task_id: str, deleted_dt: datetime) -> None:
        """Add a deletion marker for a task.

        A deletion in the OmniFocus transaction format is a ``<task>`` element
        with only an ``<added>`` child and no ``<name>``.

        Args:
            task_id: The id of the task to delete.
            deleted_dt: UTC timestamp for the deletion marker.
        """
        self._elements.append(
            f'<task id="{task_id}">'
            f'{self._leaf("added", _format_dt_utc(deleted_dt))}'
            f"</task>"
        )

    def to_xml_bytes(self) -> bytes:
        """Serialise the transaction to UTF-8 XML bytes.

        Returns:
            A ``contents.xml`` byte string with the ``<omnifocus>`` root.
        """
        body = "".join(self._elements)
        doc = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            f'<omnifocus xmlns="{_NS}" app-id="{_APP_ID}" app-version="{_APP_VERSION}">'
            f"{body}"
            f"</omnifocus>"
        )
        return doc.encode("utf-8")


class TaskWriter:
    """High-level API for creating OmniFocus transactions.

    Args:
        client_id: A stable identifier for this client (used in filenames).
        parent_id: The parent/predecessor transaction id.  Defaults to the
            client id, which is valid for the first transaction from a new client.
    """

    def __init__(
        self,
        client_id: str | None = None,
        parent_id: str | None = None,
    ) -> None:
        self._client_id = client_id or generate_id()
        self._parent_id = parent_id or self._client_id

    # ------------------------------------------------------------------
    # Public operations
    # ------------------------------------------------------------------

    def add_task(
        self,
        name: str,
        *,
        parent_task_id: Optional[str] = None,
        inbox: bool = True,
        flagged: bool = False,
        due_dt: Optional[datetime] = None,
        start_dt: Optional[datetime] = None,
        note: str = "",
        estimated_minutes: Optional[int] = None,
        task_id: Optional[str] = None,
    ) -> tuple[str, bytes, str]:
        """Create a transaction ZIP that adds a new task.

        Args:
            name: Task display name.
            parent_task_id: Parent project/task id, or ``None`` for inbox.
            inbox: Whether to add to the inbox.
            flagged: Whether to flag the task.
            due_dt: Optional local-time due date.
            start_dt: Optional local-time defer date.
            note: Optional plain-text note.
            estimated_minutes: Optional duration estimate in minutes.
            task_id: Override the generated id (useful for testing).

        Returns:
            ``(filename, zip_bytes, new_task_id)`` — the ZIP filename, raw
            bytes to upload, and the id assigned to the new task.
        """
        new_id = task_id or generate_id()
        now = _now_utc()
        rank = int(now.timestamp() * 1000) & 0x7FFFFFFF  # monotonic-ish

        builder = TransactionBuilder()
        builder.add_task(
            task_id=new_id,
            name=name,
            parent_task_id=parent_task_id,
            inbox=inbox,
            flagged=flagged,
            rank=rank,
            added_dt=now,
            modified_dt=now,
            due_dt=due_dt,
            start_dt=start_dt,
            note=note,
            estimated_minutes=estimated_minutes,
        )
        fname, data = self._build_zip(builder, now)
        return fname, data, new_id

    def complete_task(self, task: Task) -> tuple[str, bytes]:
        """Create a transaction ZIP that marks *task* as completed.

        Args:
            task: The :class:`~omnifocus.models.Task` to complete.

        Returns:
            ``(filename, zip_bytes)``.
        """
        now = _now_utc()
        builder = TransactionBuilder()
        builder.add_task(
            task_id=task.id,
            name=task.name,
            parent_task_id=task.parent_task_id,
            inbox=task.inbox,
            flagged=task.flagged,
            rank=task.rank,
            added_dt=task.added,
            modified_dt=now,
            due_dt=task.due,
            start_dt=task.start,
            completed_dt=now,
            note=task.note,
            order=task.order,
            estimated_minutes=task.estimated_minutes,
            repetition_rule=task.repetition_rule,
            hidden_dt=task.hidden,
        )
        return self._build_zip(builder, now)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_zip(
        self, builder: TransactionBuilder, ts: datetime
    ) -> tuple[str, bytes]:
        """Serialise *builder* content into a ZIP archive.

        Args:
            builder: A populated :class:`TransactionBuilder`.
            ts: Timestamp to use in the filename.

        Returns:
            ``(filename, zip_bytes)``
        """
        xml_bytes = builder.to_xml_bytes()
        filename = (
            f"{_format_ts(ts)}={self._client_id}+{self._parent_id}.zip"
        )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("contents.xml", xml_bytes)
        return filename, buf.getvalue()
