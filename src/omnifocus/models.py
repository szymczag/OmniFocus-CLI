"""Data model dataclasses for OmniFocus entities.

These are plain, immutable-friendly dataclasses produced by :mod:`omnifocus.parser`
and consumed by all other modules.  No business logic lives here — just structure.

Date conventions
----------------
- ``added`` / ``modified`` / ``completed`` are :class:`datetime.datetime` in UTC
  (they have a ``Z`` suffix in the XML).
- ``due`` / ``start`` are :class:`datetime.datetime` in *local* time without a
  timezone (OmniFocus stores them without a ``Z`` suffix).  Consumers should
  treat them as wall-clock times in the user's local timezone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Folder:
    """A top-level or nested folder that groups projects.

    Attributes:
        id: Opaque OmniFocus identifier string.
        name: Display name (may contain Unicode and emoji).
        parent_folder_id: ``id`` of the parent :class:`Folder`, or ``None``
            for top-level folders.
        rank: Integer sort key used to order siblings.
        added: UTC datetime when the folder was created.
        modified: UTC datetime of the last modification.
    """

    id: str
    name: str
    parent_folder_id: str | None
    rank: int
    added: datetime
    modified: datetime


@dataclass(frozen=True)
class Tag:
    """An OmniFocus context / tag.

    Attributes:
        id: Opaque identifier.
        name: Display name.
        parent_tag_id: ``id`` of the parent tag, or ``None`` for root tags.
        rank: Integer sort key.
    """

    id: str
    name: str
    parent_tag_id: str | None
    rank: int


@dataclass(frozen=True)
class Project:
    """A project container (a task element with a populated ``<project>`` child).

    Attributes:
        id: Opaque identifier (same as the underlying ``<task>`` element's id).
        name: Display name.
        folder_id: ``id`` of the containing :class:`Folder`, or ``None`` for
            projects not assigned to any folder.
        status: One of ``"active"``, ``"inactive"``, ``"done"``, ``"dropped"``.
        singleton: ``True`` for single-action lists.
        rank: Integer sort key within the parent folder.
        added: UTC datetime of creation.
        modified: UTC datetime of last modification.
        flagged: Whether the project is flagged.
        due: Wall-clock due datetime (local time), or ``None``.
        start: Wall-clock defer datetime (local time), or ``None``.
        note: Plain-text note content (may be empty).
        completed: UTC datetime when the project was completed, or ``None``.
        tag_ids: Ordered list of :class:`Tag` identifiers assigned to this project.
    """

    id: str
    name: str
    folder_id: str | None
    status: str
    singleton: bool
    rank: int
    added: datetime
    modified: datetime
    flagged: bool
    due: datetime | None
    start: datetime | None
    note: str
    completed: datetime | None
    tag_ids: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Task:
    """A leaf or intermediate task inside a project.

    Attributes:
        id: Opaque identifier.
        name: Display name.
        parent_task_id: ``id`` of the immediate parent task or project, or
            ``None`` for inbox tasks with no explicit parent.
        project_id: ``id`` of the containing :class:`Project`, resolved by
            walking up the parent chain.  ``None`` for inbox tasks.
        inbox: ``True`` when the task lives in the OmniFocus inbox.
        completed: UTC datetime of completion, or ``None`` if still active.
        flagged: Whether the task is flagged.
        due: Wall-clock due datetime (local time), or ``None``.
        start: Wall-clock defer datetime (local time), or ``None``.
        hidden: UTC datetime when the task was dropped/hidden, or ``None`` if
            visible.
        note: Plain-text note (may be empty).
        rank: Integer sort key within the parent.
        repetition_rule: RFC 5545 RRULE string, or ``None`` for non-repeating tasks.
        estimated_minutes: Estimated duration in minutes, or ``None``.
        tag_ids: Ordered list of :class:`Tag` identifiers.
        added: UTC datetime of creation.
        modified: UTC datetime of last modification.
        order: Subtask ordering: ``"sequential"`` or ``"parallel"``.
    """

    id: str
    name: str
    parent_task_id: str | None
    project_id: str | None
    inbox: bool
    completed: datetime | None
    flagged: bool
    due: datetime | None
    start: datetime | None
    hidden: datetime | None
    note: str
    rank: int
    repetition_rule: str | None
    estimated_minutes: int | None
    tag_ids: tuple[str, ...] = field(default_factory=tuple)
    added: datetime = field(default_factory=lambda: datetime.utcnow())
    modified: datetime = field(default_factory=lambda: datetime.utcnow())
    order: str = "parallel"


@dataclass
class OFModel:
    """The fully-parsed, in-memory representation of an OmniFocus database.

    All lookup dictionaries are keyed by the entity's ``id`` field.

    Attributes:
        folders: All folders, keyed by id.
        projects: All projects, keyed by id.
        tasks: All non-project tasks, keyed by id.
        tags: All tags/contexts, keyed by id.
        parsed_at: UTC datetime when this model was built from the bundle.
    """

    folders: dict[str, Folder] = field(default_factory=dict)
    projects: dict[str, Project] = field(default_factory=dict)
    tasks: dict[str, Task] = field(default_factory=dict)
    tags: dict[str, Tag] = field(default_factory=dict)
    parsed_at: datetime = field(default_factory=datetime.utcnow)

    @property
    def active_tasks(self) -> list[Task]:
        """Return all tasks that are not completed and not hidden."""
        return [t for t in self.tasks.values() if t.completed is None and t.hidden is None]

    @property
    def inbox_tasks(self) -> list[Task]:
        """Return active tasks that are in the inbox."""
        return [t for t in self.active_tasks if t.inbox]

    @property
    def active_projects(self) -> list[Project]:
        """Return projects with status 'active'."""
        return [p for p in self.projects.values() if p.status == "active"]
