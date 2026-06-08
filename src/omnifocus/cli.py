"""Click CLI entry point for omnifocus-cli.

Provides the ``of`` command group with production task, project, folder, and
tag workflows:

- ``of sync``      — pull the latest bundle from WebDAV
- ``of tasks``     — list tasks with filters
- ``of add``       — add a task
- ``of done``      — mark a task complete
- ``of projects``  — show projects grouped by folder
- ``of folders``   — show the folder hierarchy
- ``of tags``      — show the tag hierarchy
- ``of task-update`` — update a task
- ``of task-drop`` — drop a task
- ``of project-add`` — add a project
- ``of project-update`` — update a project
- ``of project-done`` — mark a project complete
- ``of folder-add`` / ``folder-update`` / ``folder-drop`` — manage folders
- ``of tag-add`` / ``tag-update`` / ``tag-drop`` — manage tags

All WebDAV credentials and the encryption passphrase are read from
environment variables (see :mod:`omnifocus.store`).
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import asyncio
from datetime import UTC, datetime

import click

from omnifocus import __version__
from omnifocus.dateparse import parse_due
from omnifocus.errors import (
    OFBundleNotFound,
    OFEncryptionError,
    OFError,
    OFWebDAVError,
)
from omnifocus.formatting import (
    render_folder_tree,
    render_folders_json,
    render_project_tree,
    render_projects_json,
    render_tag_tree,
    render_tags_json,
    render_tasks_json,
    render_tasks_table,
)
from omnifocus.fuzzy import find_tasks
from omnifocus.models import Folder, OFModel, Project, Tag, Task
from omnifocus.store import OFocusStore

_ROOT_HELP = """OmniFocus CLI for OmniFocus 4.

Independent task and project management over WebDAV sync and bundle decryption.

Author: Maciej Szymczak <maciej@szymczak.at>

Environment:
  OF_WEBDAV_URL             WebDAV bundle URL (required)
  OF_WEBDAV_USER            WebDAV username (optional override)
  OF_WEBDAV_PASS            WebDAV password (optional override)
  OF_WEBDAV_AUTH            WebDAV authentication scheme: 'basic' (default) or 'digest'
  OF_ENCRYPTION_PASSPHRASE  Encryption passphrase (defaults to WebDAV password)
  OF_CACHE_DIR              Cache directory (default: /tmp/of-cache)

Common commands:
  of sync
  of tasks --inbox
  of add "Buy milk" --project Errands
  of done "Write tests" --yes
  of projects --status active
  of folders
  of tags
  of tag-add "@home"

Container usage:
  podman run --rm IMAGE sync
  podman run --rm IMAGE add "Buy milk"
  podman run --rm -i IMAGE
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: object) -> object:
    """Run an async coroutine from a synchronous Click command."""
    return asyncio.run(coro)  # type: ignore[arg-type]


def _parse_due(value: str) -> datetime:
    """Parse a due/defer token using the shared date parser."""
    return parse_due(value)


async def _get_model(force_refresh: bool = False) -> OFModel:
    """Load the OFModel from the store, propagating errors as ClickExceptions."""
    try:
        async with OFocusStore.from_env() as store:
            return await store.load(force_refresh=force_refresh)
    except OFWebDAVError as exc:
        raise click.ClickException(f"WebDAV error: {exc}") from exc
    except OFEncryptionError as exc:
        raise click.ClickException(f"Encryption error: {exc}") from exc
    except OFBundleNotFound as exc:
        raise click.ClickException(f"Bundle not found: {exc}") from exc
    except OFError as exc:
        raise click.ClickException(str(exc)) from exc


def _match_active_project(model: OFModel, query: str) -> Project:
    """Resolve a single active project by fuzzy substring."""
    needle = query.lower()
    matches = [
        project
        for project in model.projects.values()
        if needle in project.name.lower() and project.status == "active"
    ]
    if not matches:
        raise click.ClickException(f"No active project matching {query!r}")
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches[:5])
        raise click.ClickException(f"Multiple projects match {query!r}: {names}. Be more specific.")
    return matches[0]


def _match_project(model: OFModel, query: str) -> Project:
    """Resolve a single project by fuzzy substring."""
    needle = query.lower()
    matches = [project for project in model.projects.values() if needle in project.name.lower()]
    if not matches:
        raise click.ClickException(f"No project matching {query!r}")
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches[:5])
        raise click.ClickException(f"Multiple projects match {query!r}: {names}. Be more specific.")
    return matches[0]


def _match_folder_id(model: OFModel, query: str) -> str:
    """Resolve a folder id by fuzzy substring."""
    needle = query.lower()
    matches = [folder for folder in model.folders.values() if needle in folder.name.lower()]
    if not matches:
        raise click.ClickException(f"No folder matching {query!r}")
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches[:5])
        raise click.ClickException(f"Multiple folders match {query!r}: {names}. Be more specific.")
    return matches[0].id


def _match_folder(model: OFModel, query: str) -> Folder:
    """Resolve a single folder by fuzzy substring."""
    return model.folders[_match_folder_id(model, query)]


def _match_task(model: OFModel, query: str) -> Task:
    """Resolve a single active task by id or fuzzy name."""
    results = find_tasks(query, model.active_tasks)
    if not results:
        raise click.ClickException(f"No active task matching {query!r}")
    if len(results) > 1 and results[0].score < 0.8:
        choices = "\n".join(
            f"  [{i + 1}] {result.task.id}  {result.task.name}"
            for i, result in enumerate(results[:5])
        )
        raise click.ClickException(
            f"Ambiguous match for {query!r}. Did you mean one of:\n{choices}"
        )
    return results[0].task


def _visible_tags(model: OFModel) -> dict[str, Tag]:
    """Return visible, non-dropped tags."""
    return {tag_id: tag for tag_id, tag in model.tags.items() if tag.hidden is None}


def _matching_tag_ids(
    model: OFModel,
    query: str,
    *,
    include_hidden: bool = False,
) -> set[str]:
    """Return all matching tag ids for a fuzzy substring query."""
    haystack = model.tags if include_hidden else _visible_tags(model)
    needle = query.lower()
    return {tag.id for tag in haystack.values() if needle in tag.name.lower()}


def _match_tag_id(model: OFModel, query: str, *, include_hidden: bool = False) -> str:
    """Resolve a tag id by fuzzy substring."""
    haystack = model.tags if include_hidden else _visible_tags(model)
    matches = [tag for tag in haystack.values() if query.lower() in tag.name.lower()]
    if not matches:
        raise click.ClickException(f"No tag matching {query!r}")
    if len(matches) > 1:
        names = ", ".join(match.name for match in matches[:5])
        raise click.ClickException(f"Multiple tags match {query!r}: {names}. Be more specific.")
    return matches[0].id


def _match_tag(model: OFModel, query: str, *, include_hidden: bool = False) -> Tag:
    """Resolve a single tag by fuzzy substring."""
    return model.tags[_match_tag_id(model, query, include_hidden=include_hidden)]


def _validate_folder_parent_change(
    *,
    model: OFModel,
    folder_id: str,
    parent_folder_id: str | None,
    clear_parent: bool,
) -> None:
    """Validate requested folder reparenting."""
    if parent_folder_id and clear_parent:
        raise click.ClickException("--parent-id and --clear-parent cannot be combined")
    if parent_folder_id is None:
        return
    if parent_folder_id not in model.folders:
        raise click.ClickException(f"Folder not found: {parent_folder_id}")
    if parent_folder_id == folder_id:
        raise click.ClickException("Folder cannot be its own parent")
    seen: set[str] = {folder_id}
    current_id: str | None = parent_folder_id
    while current_id is not None:
        if current_id in seen:
            raise click.ClickException("Folder move would create a cycle")
        seen.add(current_id)
        current = model.folders.get(current_id)
        current_id = None if current is None else current.parent_folder_id


def _validate_tag_parent_change(
    *,
    model: OFModel,
    tag_id: str,
    parent_tag_id: str | None,
    clear_parent: bool,
) -> None:
    """Validate requested tag reparenting."""
    if parent_tag_id and clear_parent:
        raise click.ClickException("--parent-id and --clear-parent cannot be combined")
    if parent_tag_id is None:
        return
    if parent_tag_id not in model.tags:
        raise click.ClickException(f"Tag not found: {parent_tag_id}")
    if parent_tag_id == tag_id:
        raise click.ClickException("Tag cannot be its own parent")
    seen: set[str] = {tag_id}
    current_id: str | None = parent_tag_id
    while current_id is not None:
        if current_id in seen:
            raise click.ClickException("Tag move would create a cycle")
        seen.add(current_id)
        current = model.tags.get(current_id)
        current_id = None if current is None else current.parent_tag_id


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=_ROOT_HELP,
)
@click.version_option(version=__version__, prog_name="of")
def cli() -> None:
    """Run the OmniFocus task and project CLI."""


# ---------------------------------------------------------------------------
# of sync
# ---------------------------------------------------------------------------


@cli.command("sync")
def sync_cmd() -> None:
    """Pull the latest bundle from the WebDAV server."""

    async def _sync() -> None:
        try:
            async with OFocusStore.from_env() as store:
                model = await store.load(force_refresh=True)
                click.echo(
                    f"Synced: {len(model.tasks)} tasks, "
                    f"{len(model.projects)} projects, "
                    f"{len(model.folders)} folders."
                )
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

    _run(_sync())


@cli.command("tasks")
@click.option("--inbox", is_flag=True, help="Show only inbox tasks.")
@click.option("--today", is_flag=True, help="Show tasks due today or overdue.")
@click.option("--flagged", is_flag=True, help="Show only flagged tasks.")
@click.option("--due", "due_only", is_flag=True, help="Show only tasks with a due date.")
@click.option(
    "--project",
    "project_name",
    default=None,
    metavar="NAME",
    help="Filter by project name (substring, case-insensitive).",
)
@click.option(
    "--tag",
    "tag_name",
    default=None,
    metavar="NAME",
    help="Filter by tag name (substring, case-insensitive).",
)
@click.option(
    "--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format."
)
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks.")
def tasks_cmd(
    inbox: bool,
    today: bool,
    flagged: bool,
    due_only: bool,
    project_name: str | None,
    tag_name: str | None,
    fmt: str,
    show_all: bool,
) -> None:
    """List tasks with optional filters (AND logic)."""

    async def _run_tasks() -> None:
        model = await _get_model()
        tasks = list(model.tasks.values()) if show_all else model.active_tasks

        if inbox:
            tasks = [t for t in tasks if t.inbox]

        if today:
            now_date = datetime.today().date()
            tasks = [t for t in tasks if t.due is not None and t.due.date() <= now_date]

        if flagged:
            tasks = [t for t in tasks if t.flagged]

        if due_only:
            tasks = [t for t in tasks if t.due is not None]

        if project_name:
            needle = project_name.lower()
            matching_proj_ids = {
                pid for pid, p in model.projects.items() if needle in p.name.lower()
            }
            tasks = [t for t in tasks if t.project_id in matching_proj_ids]

        if tag_name:
            matching_tag_ids = _matching_tag_ids(model, tag_name)
            if not matching_tag_ids:
                raise click.ClickException(f"No tag matching {tag_name!r}")
            tasks = [task for task in tasks if matching_tag_ids.intersection(task.tag_ids)]

        if fmt == "json":
            render_tasks_json(tasks)
        else:
            render_tasks_table(tasks, model.projects)

        click.echo(f"{len(tasks)} task(s) shown.", err=True)

    _run(_run_tasks())


# ---------------------------------------------------------------------------
# of add
# ---------------------------------------------------------------------------


@cli.command("add")
@click.argument("name")
@click.option(
    "--project",
    "project_name",
    default=None,
    metavar="NAME",
    help="Add to this project (substring match).",
)
@click.option(
    "--due",
    "due_str",
    default=None,
    metavar="DATE",
    help="Due date: YYYY-MM-DD, today, tomorrow, mon-sun.",
)
@click.option("--flagged", is_flag=True, help="Mark as flagged.")
@click.option("--note", default=None, metavar="TEXT", help="Task note.")
def add_cmd(
    name: str,
    project_name: str | None,
    due_str: str | None,
    flagged: bool,
    note: str | None,
) -> None:
    """Add a task to inbox or a specific project.

    NAME is the task display name.
    """

    async def _run_add() -> None:
        due_dt: datetime | None = None
        if due_str:
            due_dt = _parse_due(due_str)

        model = await _get_model()
        parent_task_id: str | None = None
        inbox = True

        if project_name:
            parent_task_id = _match_active_project(model, project_name).id
            inbox = False

        try:
            async with OFocusStore.from_env() as store:
                result = await store.add_task(
                    name=name,
                    parent_task_id=parent_task_id,
                    inbox=inbox,
                    flagged=flagged,
                    due_dt=due_dt,
                    note=note or "",
                )
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(f"Added task: {name!r} (id={result['task_id']})")

    _run(_run_add())


# ---------------------------------------------------------------------------
# of done
# ---------------------------------------------------------------------------


@cli.command("done")
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def done_cmd(query: str, yes: bool) -> None:
    """Mark a task complete.

    QUERY can be a task ID or a fuzzy name match.
    """

    async def _run_done() -> None:
        model = await _get_model()
        task = _match_task(model, query)

        if not yes:
            click.confirm(f"Complete task: {task.name!r}?", abort=True)

        try:
            async with OFocusStore.from_env() as store:
                await store.complete_task(task)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(f"Completed: {task.name!r}")

    _run(_run_done())


@cli.command("task-update")
@click.argument("query")
@click.option("--name", "new_name", default=None, metavar="NAME")
@click.option("--note", default=None, metavar="TEXT")
@click.option("--flagged/--unflagged", default=None)
@click.option("--due", "due_str", default=None, metavar="DATE")
@click.option("--clear-due", is_flag=True)
@click.option("--defer", "defer_str", default=None, metavar="DATE")
@click.option("--clear-defer", is_flag=True)
@click.option("--estimate", "estimate_minutes", default=None, type=int)
@click.option("--clear-estimate", is_flag=True)
@click.option("--project-id", default=None, metavar="PROJECT_ID")
@click.option("--clear-project", is_flag=True)
@click.option("--inbox", "move_to_inbox", is_flag=True)
@click.option("--tag-id", "tag_ids", multiple=True)
@click.option("--clear-tags", is_flag=True)
def task_update_cmd(
    query: str,
    new_name: str | None,
    note: str | None,
    flagged: bool | None,
    due_str: str | None,
    clear_due: bool,
    defer_str: str | None,
    clear_defer: bool,
    estimate_minutes: int | None,
    clear_estimate: bool,
    project_id: str | None,
    clear_project: bool,
    move_to_inbox: bool,
    tag_ids: tuple[str, ...],
    clear_tags: bool,
) -> None:
    """Update an existing task."""

    async def _run_task_update() -> None:
        model = await _get_model()
        task = _match_task(model, query)
        if project_id and clear_project:
            raise click.ClickException("--project-id and --clear-project cannot be combined")
        if project_id and move_to_inbox:
            raise click.ClickException("--project-id and --inbox cannot be combined")
        if clear_tags and tag_ids:
            raise click.ClickException("--tag-id and --clear-tags cannot be combined")

        if project_id:
            project = model.projects.get(project_id)
            if project is None:
                raise click.ClickException(f"Project not found: {project_id}")
            if project.status != "active":
                raise click.ClickException(f"Project is not active: {project_id}")
            parent_task_id = project.id
            containing_project_id = project.id
            inbox = False
        elif clear_project or move_to_inbox:
            parent_task_id = None
            containing_project_id = None
            inbox = True
        else:
            parent_task_id = task.parent_task_id
            containing_project_id = task.project_id
            inbox = task.inbox
        if tag_ids:
            missing_tag_ids = [tag_id for tag_id in tag_ids if tag_id not in model.tags]
            if missing_tag_ids:
                joined = ", ".join(missing_tag_ids)
                raise click.ClickException(f"Unknown tag IDs: {joined}")

        due_dt = None if clear_due else (_parse_due(due_str) if due_str else task.due)
        defer_dt = None if clear_defer else (_parse_due(defer_str) if defer_str else task.start)
        estimated = (
            None
            if clear_estimate
            else (estimate_minutes if estimate_minutes is not None else task.estimated_minutes)
        )
        now = datetime.now(UTC)
        updated = Task(
            id=task.id,
            name=new_name or task.name,
            parent_task_id=parent_task_id,
            project_id=containing_project_id,
            inbox=inbox,
            completed=task.completed,
            flagged=task.flagged if flagged is None else flagged,
            due=due_dt,
            start=defer_dt,
            hidden=task.hidden,
            note=task.note if note is None else note,
            rank=task.rank,
            repetition_rule=task.repetition_rule,
            estimated_minutes=estimated,
            tag_ids=() if clear_tags else (tag_ids if tag_ids else task.tag_ids),
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
        )
        try:
            async with OFocusStore.from_env() as store:
                await store.update_task(updated)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated task: {updated.name!r}")

    _run(_run_task_update())


@cli.command("task-drop")
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def task_drop_cmd(query: str, yes: bool) -> None:
    """Mark a task as dropped/hidden."""

    async def _run_task_drop() -> None:
        model = await _get_model()
        task = _match_task(model, query)

        if not yes:
            click.confirm(f"Drop task: {task.name!r}?", abort=True)

        try:
            async with OFocusStore.from_env() as store:
                await store.drop_task(task)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

        click.echo(f"Dropped: {task.name!r}")

    _run(_run_task_drop())


# ---------------------------------------------------------------------------
# of projects
# ---------------------------------------------------------------------------


@cli.command("projects")
@click.option(
    "--status",
    type=click.Choice(["active", "all", "inactive"]),
    default="active",
    help="Filter by project status.",
)
@click.option(
    "--tag",
    "tag_name",
    default=None,
    metavar="NAME",
    help="Filter by tag name (substring, case-insensitive).",
)
@click.option(
    "--format", "fmt", type=click.Choice(["tree", "json"]), default="tree", help="Output format."
)
def projects_cmd(status: str, tag_name: str | None, fmt: str) -> None:
    """Show projects grouped by folder with project details."""

    async def _run_projects() -> None:
        model = await _get_model()
        filtered_projects = {
            project.id: project
            for project in model.projects.values()
            if status == "all" or project.status == status
        }
        if tag_name:
            matching_tag_ids = _matching_tag_ids(model, tag_name)
            if not matching_tag_ids:
                raise click.ClickException(f"No tag matching {tag_name!r}")
            filtered_projects = {
                project_id: project
                for project_id, project in filtered_projects.items()
                if matching_tag_ids.intersection(project.tag_ids)
            }
        if fmt == "json":
            render_projects_json(filtered_projects)
        else:
            render_project_tree(model.folders, filtered_projects, status_filter="all")

    _run(_run_projects())


@cli.command("folders")
@click.option(
    "--format", "fmt", type=click.Choice(["tree", "json"]), default="tree", help="Output format."
)
def folders_cmd(fmt: str) -> None:
    """Show the folder hierarchy with direct child projects."""

    async def _run_folders() -> None:
        model = await _get_model()
        if fmt == "json":
            render_folders_json(model.folders, model.projects)
        else:
            render_folder_tree(model.folders, model.projects)

    _run(_run_folders())


@cli.command("tags")
@click.option(
    "--format", "fmt", type=click.Choice(["tree", "json"]), default="tree", help="Output format."
)
@click.option("--all", "show_all", is_flag=True, help="Include dropped/hidden tags.")
def tags_cmd(fmt: str, show_all: bool) -> None:
    """Show the tag hierarchy."""

    async def _run_tags() -> None:
        model = await _get_model()
        if fmt == "json":
            render_tags_json(model.tags, include_hidden=show_all)
        else:
            render_tag_tree(model.tags, include_hidden=show_all)

    _run(_run_tags())


@cli.command("folder-add")
@click.argument("name")
@click.option("--parent-id", default=None, metavar="FOLDER_ID")
def folder_add_cmd(name: str, parent_id: str | None) -> None:
    """Add a new folder."""

    async def _run_folder_add() -> None:
        model = await _get_model()
        if parent_id and parent_id not in model.folders:
            raise click.ClickException(f"Folder not found: {parent_id}")
        try:
            async with OFocusStore.from_env() as store:
                result = await store.add_folder(name=name, parent_folder_id=parent_id)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Added folder: {name!r} (id={result['folder_id']})")

    _run(_run_folder_add())


@cli.command("folder-update")
@click.argument("query")
@click.option("--name", "new_name", default=None, metavar="NAME")
@click.option("--parent-id", default=None, metavar="FOLDER_ID")
@click.option("--clear-parent", is_flag=True)
def folder_update_cmd(
    query: str,
    new_name: str | None,
    parent_id: str | None,
    clear_parent: bool,
) -> None:
    """Rename a folder or move it under another folder."""

    async def _run_folder_update() -> None:
        model = await _get_model()
        folder = _match_folder(model, query)
        _validate_folder_parent_change(
            model=model,
            folder_id=folder.id,
            parent_folder_id=parent_id,
            clear_parent=clear_parent,
        )
        if parent_id is not None:
            new_parent_id = parent_id
        elif clear_parent:
            new_parent_id = None
        else:
            new_parent_id = folder.parent_folder_id
        updated = Folder(
            id=folder.id,
            name=new_name or folder.name,
            parent_folder_id=new_parent_id,
            rank=folder.rank,
            added=folder.added,
            modified=datetime.now(UTC),
        )
        try:
            async with OFocusStore.from_env() as store:
                await store.update_folder(updated)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated folder: {updated.name!r}")

    _run(_run_folder_update())


@cli.command("folder-drop")
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def folder_drop_cmd(query: str, yes: bool) -> None:
    """Drop a folder."""

    async def _run_folder_drop() -> None:
        model = await _get_model()
        folder = _match_folder(model, query)
        if not yes:
            click.confirm(f"Drop folder: {folder.name!r}?", abort=True)
        try:
            async with OFocusStore.from_env() as store:
                await store.drop_folder(folder)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Dropped folder: {folder.name!r}")

    _run(_run_folder_drop())


@cli.command("tag-add")
@click.argument("name")
@click.option("--parent", "parent_query", default=None, metavar="QUERY")
@click.option("--parent-id", default=None, metavar="TAG_ID")
@click.option("--note", default="", metavar="TEXT")
def tag_add_cmd(
    name: str,
    parent_query: str | None,
    parent_id: str | None,
    note: str,
) -> None:
    """Add a new tag."""

    async def _run_tag_add() -> None:
        model = await _get_model()
        if parent_query and parent_id:
            raise click.ClickException("--parent and --parent-id cannot be combined")
        resolved_parent_id = parent_id
        if parent_query:
            resolved_parent_id = _match_tag_id(model, parent_query)
        if resolved_parent_id and resolved_parent_id not in model.tags:
            raise click.ClickException(f"Tag not found: {resolved_parent_id}")
        try:
            async with OFocusStore.from_env() as store:
                result = await store.add_tag(
                    name=name,
                    parent_tag_id=resolved_parent_id,
                    note=note,
                )
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Added tag: {name!r} (id={result['tag_id']})")

    _run(_run_tag_add())


@cli.command("tag-update")
@click.argument("query")
@click.option("--name", "new_name", default=None, metavar="NAME")
@click.option("--parent", "parent_query", default=None, metavar="QUERY")
@click.option("--parent-id", default=None, metavar="TAG_ID")
@click.option("--clear-parent", is_flag=True)
@click.option("--note", default=None, metavar="TEXT")
def tag_update_cmd(
    query: str,
    new_name: str | None,
    parent_query: str | None,
    parent_id: str | None,
    clear_parent: bool,
    note: str | None,
) -> None:
    """Rename a tag or move it under another tag."""

    async def _run_tag_update() -> None:
        model = await _get_model()
        tag = _match_tag(model, query, include_hidden=True)
        if parent_query and parent_id:
            raise click.ClickException("--parent and --parent-id cannot be combined")
        resolved_parent_id = parent_id
        if parent_query:
            resolved_parent_id = _match_tag_id(model, parent_query)
        _validate_tag_parent_change(
            model=model,
            tag_id=tag.id,
            parent_tag_id=resolved_parent_id,
            clear_parent=clear_parent,
        )
        if resolved_parent_id is not None:
            new_parent_id = resolved_parent_id
        elif clear_parent:
            new_parent_id = None
        else:
            new_parent_id = tag.parent_tag_id
        updated = Tag(
            id=tag.id,
            name=new_name or tag.name,
            parent_tag_id=new_parent_id,
            rank=tag.rank,
            added=tag.added,
            modified=datetime.now(UTC),
            note=tag.note if note is None else note,
            hidden=tag.hidden,
        )
        try:
            async with OFocusStore.from_env() as store:
                await store.update_tag(updated)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated tag: {updated.name!r}")

    _run(_run_tag_update())


@cli.command("tag-drop")
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def tag_drop_cmd(query: str, yes: bool) -> None:
    """Drop a tag."""

    async def _run_tag_drop() -> None:
        model = await _get_model()
        tag = _match_tag(model, query)
        if not yes:
            click.confirm(f"Drop tag: {tag.name!r}?", abort=True)
        try:
            async with OFocusStore.from_env() as store:
                await store.drop_tag(tag)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Dropped tag: {tag.name!r}")

    _run(_run_tag_drop())


@cli.command("project-add")
@click.argument("name")
@click.option("--folder", "folder_name", default=None, metavar="NAME")
@click.option("--note", default="", metavar="TEXT")
@click.option("--flagged", is_flag=True)
@click.option("--due", "due_str", default=None, metavar="DATE")
@click.option("--defer", "defer_str", default=None, metavar="DATE")
@click.option(
    "--status",
    type=click.Choice(["active", "inactive"]),
    default="active",
)
def project_add_cmd(
    name: str,
    folder_name: str | None,
    note: str,
    flagged: bool,
    due_str: str | None,
    defer_str: str | None,
    status: str,
) -> None:
    """Add a new project."""

    async def _run_project_add() -> None:
        model = await _get_model()
        folder_id = _match_folder_id(model, folder_name) if folder_name else None
        due_dt = _parse_due(due_str) if due_str else None
        defer_dt = _parse_due(defer_str) if defer_str else None
        try:
            async with OFocusStore.from_env() as store:
                result = await store.add_project(
                    name=name,
                    folder_id=folder_id,
                    status=status,
                    flagged=flagged,
                    due_dt=due_dt,
                    start_dt=defer_dt,
                    note=note,
                )
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Added project: {name!r} (id={result['project_id']})")

    _run(_run_project_add())


@cli.command("project-update")
@click.argument("query")
@click.option("--name", "new_name", default=None, metavar="NAME")
@click.option("--note", default=None, metavar="TEXT")
@click.option("--flagged/--unflagged", default=None)
@click.option("--due", "due_str", default=None, metavar="DATE")
@click.option("--clear-due", is_flag=True)
@click.option("--defer", "defer_str", default=None, metavar="DATE")
@click.option("--clear-defer", is_flag=True)
@click.option("--tag-id", "tag_ids", multiple=True)
@click.option("--clear-tags", is_flag=True)
@click.option("--folder-id", default=None, metavar="FOLDER_ID")
@click.option("--clear-folder", is_flag=True)
@click.option(
    "--status",
    type=click.Choice(["active", "inactive", "done", "dropped"]),
    default=None,
)
def project_update_cmd(
    query: str,
    new_name: str | None,
    note: str | None,
    flagged: bool | None,
    due_str: str | None,
    clear_due: bool,
    defer_str: str | None,
    clear_defer: bool,
    tag_ids: tuple[str, ...],
    clear_tags: bool,
    folder_id: str | None,
    clear_folder: bool,
    status: str | None,
) -> None:
    """Update an existing project."""

    async def _run_project_update() -> None:
        model = await _get_model()
        project = _match_project(model, query)
        if folder_id and clear_folder:
            raise click.ClickException("--folder-id and --clear-folder cannot be combined")
        if clear_tags and tag_ids:
            raise click.ClickException("--tag-id and --clear-tags cannot be combined")
        if folder_id:
            if folder_id not in model.folders:
                raise click.ClickException(f"Folder not found: {folder_id}")
            new_folder_id = folder_id
        elif clear_folder:
            new_folder_id = None
        else:
            new_folder_id = project.folder_id
        if tag_ids:
            missing_tag_ids = [tag_id for tag_id in tag_ids if tag_id not in model.tags]
            if missing_tag_ids:
                joined = ", ".join(missing_tag_ids)
                raise click.ClickException(f"Unknown tag IDs: {joined}")
        due_dt = None if clear_due else (_parse_due(due_str) if due_str else project.due)
        defer_dt = None if clear_defer else (_parse_due(defer_str) if defer_str else project.start)
        now = datetime.now(UTC)
        updated = Project(
            id=project.id,
            name=new_name or project.name,
            folder_id=new_folder_id,
            status=status or project.status,
            singleton=project.singleton,
            rank=project.rank,
            added=project.added,
            modified=now,
            flagged=project.flagged if flagged is None else flagged,
            due=due_dt,
            start=defer_dt,
            note=project.note if note is None else note,
            completed=(
                now if status == "done" and project.completed is None else project.completed
            ),
            last_review=project.last_review,
            next_review=project.next_review,
            review_interval=project.review_interval,
            tag_ids=() if clear_tags else (tag_ids if tag_ids else project.tag_ids),
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
        try:
            async with OFocusStore.from_env() as store:
                if status == "done":
                    await store.complete_project(updated)
                elif status == "dropped":
                    await store.drop_project(updated)
                else:
                    await store.update_project(updated)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Updated project: {updated.name!r}")

    _run(_run_project_update())


@cli.command("project-done")
@click.argument("query")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def project_done_cmd(query: str, yes: bool) -> None:
    """Mark a project complete."""

    async def _run_project_done() -> None:
        model = await _get_model()
        project = _match_project(model, query)
        if not yes:
            click.confirm(f"Complete project: {project.name!r}?", abort=True)
        try:
            async with OFocusStore.from_env() as store:
                await store.complete_project(project)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"Completed project: {project.name!r}")

    _run(_run_project_done())
