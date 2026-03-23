"""Click CLI entry point for omnifocus-cli.

Provides the ``of`` command group with subcommands:

- ``of sync``      — pull the latest bundle from WebDAV
- ``of sync-status`` — show cache and tail diagnostics
- ``of bundle-state`` — show parsed baseline/delta/client refs
- ``of tasks``     — list tasks with filters
- ``of add``       — add a task
- ``of done``      — mark a task complete
- ``of projects``  — show the folder/project tree
- ``of fetch-file`` — download a raw bundle file
- ``of fetch-latest-deltas`` — download recent delta ZIPs
- ``of fetch-latest-client`` — download the newest client plist
- ``of decrypt-latest-delta`` — decrypt the newest remote delta
- ``of decrypt-delta`` — decrypt a transaction ZIP and print ``contents.xml``

All WebDAV credentials and the encryption passphrase are read from
environment variables (see :mod:`omnifocus.store`).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import click

from omnifocus.errors import (
    OFBundleNotFound,
    OFEncryptionError,
    OFError,
    OFWebDAVError,
)
from omnifocus.formatting import (
    render_project_tree,
    render_projects_json,
    render_tasks_json,
    render_tasks_table,
)
from omnifocus.fuzzy import find_tasks
from omnifocus.models import OFModel, Project, Task
from omnifocus.store import OFocusStore

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro: object) -> object:
    """Run an async coroutine from a synchronous Click command."""
    return asyncio.run(coro)  # type: ignore[arg-type]


def _parse_due(value: str) -> datetime:
    """Parse a human-friendly due-date string into a naive local datetime.

    Accepts:
        - ``today`` / ``tod``
        - ``tomorrow`` / ``tom``
        - ``mon`` / ``tue`` / ``wed`` / ``thu`` / ``fri`` / ``sat`` / ``sun``
        - ``YYYY-MM-DD``
        - ``MM-DD`` (current year assumed)

    Args:
        value: The raw string from the CLI option.

    Returns:
        A naive :class:`datetime` set to 19:00 on the target date.

    Raises:
        click.BadParameter: If the value cannot be parsed.
    """
    s = value.strip().lower()
    today = datetime.today().replace(hour=19, minute=0, second=0, microsecond=0)

    if s in ("today", "tod"):
        return today

    if s in ("tomorrow", "tom"):
        return today + timedelta(days=1)

    _DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    if s[:3] in _DAYS:
        target_wd = _DAYS[s[:3]]
        days_ahead = (target_wd - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        return today + timedelta(days=days_ahead)

    if re.match(r"^\d{4}-\d{2}-\d{2}$", s):
        try:
            d = datetime.fromisoformat(s)
            return d.replace(hour=19)
        except ValueError:
            pass

    if re.match(r"^\d{2}-\d{2}$", s):
        try:
            d = datetime.fromisoformat(f"{today.year}-{s}")
            return d.replace(hour=19)
        except ValueError:
            pass

    raise click.BadParameter(
        f"{value!r} is not a recognised date. "
        "Use YYYY-MM-DD, MM-DD, today, tomorrow, or mon/tue/wed/thu/fri/sat/sun.",
        param_hint="--due",
    )


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


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
@click.option("--debug", is_flag=True, default=False, help="Enable debug logging to stderr.")
@click.pass_context
def cli(ctx: click.Context, debug: bool) -> None:
    """OmniFocus 4 command-line interface.

    Reads credentials from environment variables:

    \b
    OF_WEBDAV_URL             WebDAV bundle URL (required)
    OF_WEBDAV_USER            WebDAV username (or embed in URL)
    OF_WEBDAV_PASS            WebDAV password (or embed in URL)
    OF_ENCRYPTION_PASSPHRASE  Passphrase (defaults to WebDAV password)
    OF_CACHE_DIR              Cache directory (default: /tmp/of-cache)
    """
    if debug:
        logging.basicConfig(
            level=logging.DEBUG,
            stream=sys.stderr,
            format="%(levelname)s %(name)s: %(message)s",
        )


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


@cli.command("sync-status")
def sync_status_cmd() -> None:
    """Show sync/cache/debug status."""

    async def _run_sync_status() -> None:
        try:
            async with OFocusStore.from_env() as store:
                click.echo(json.dumps(await store.sync_status(), indent=2, sort_keys=True))
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

    _run(_run_sync_status())


@cli.command("bundle-state")
def bundle_state_cmd() -> None:
    """Show parsed bundle refs and inferred tail state."""

    async def _run_bundle_state() -> None:
        try:
            async with OFocusStore.from_env() as store:
                click.echo(json.dumps(await store.bundle_state(), indent=2, sort_keys=True))
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc

    _run(_run_bundle_state())


@cli.command("fetch-file")
@click.argument("name")
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=str),
    help="Output path for the downloaded file.",
)
def fetch_file_cmd(name: str, out_path: str) -> None:
    """Download a raw file from the bundle."""

    async def _run_fetch_file() -> None:
        try:
            async with OFocusStore.from_env() as store:
                data = await store.fetch_file(name)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        Path(out_path).write_bytes(data)
        click.echo(out_path)

    _run(_run_fetch_file())


@cli.command("fetch-latest-deltas")
@click.option("--count", default=1, show_default=True, type=int)
@click.option(
    "--out-dir",
    default=".",
    show_default=True,
    type=click.Path(file_okay=False, path_type=str),
)
def fetch_latest_deltas_cmd(count: int, out_dir: str) -> None:
    """Download the newest delta ZIPs into a directory."""

    async def _run_fetch_latest_deltas() -> None:
        try:
            async with OFocusStore.from_env() as store:
                deltas = await store.fetch_latest_deltas(count=count)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        target_dir = Path(out_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        for filename, payload in deltas:
            (target_dir / filename).write_bytes(payload)
            click.echo(str(target_dir / filename))

    _run(_run_fetch_latest_deltas())


@cli.command("fetch-latest-client")
@click.option("--client-id", default=None, metavar="ID")
@click.option(
    "--out",
    "out_path",
    default=None,
    type=click.Path(dir_okay=False, path_type=str),
    help="Output path; defaults to the remote filename in the current directory.",
)
def fetch_latest_client_cmd(client_id: str | None, out_path: str | None) -> None:
    """Download the newest client state file."""

    async def _run_fetch_latest_client() -> None:
        try:
            async with OFocusStore.from_env() as store:
                filename, payload = await store.fetch_latest_client(client_id=client_id)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        destination = Path(out_path) if out_path is not None else Path(filename)
        destination.write_bytes(payload)
        click.echo(str(destination))

    _run(_run_fetch_latest_client())


@cli.command("decrypt-latest-delta")
@click.option("--client-id", default=None, metavar="ID")
def decrypt_latest_delta_cmd(client_id: str | None) -> None:
    """Decrypt and print the newest delta ZIP."""

    async def _run_decrypt_latest_delta() -> None:
        try:
            async with OFocusStore.from_env() as store:
                filename, contents_xml = await store.decrypt_latest_delta(client_id=client_id)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc
        except OFEncryptionError as exc:
            raise click.ClickException(f"Encryption error: {exc}") from exc
        except OFError as exc:
            raise click.ClickException(str(exc)) from exc
        click.echo(f"# {filename}")
        click.echo(contents_xml)

    _run(_run_decrypt_latest_delta())


# ---------------------------------------------------------------------------
# of tasks
# ---------------------------------------------------------------------------


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
    "--format", "fmt", type=click.Choice(["table", "json"]), default="table", help="Output format."
)
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks.")
def tasks_cmd(
    inbox: bool,
    today: bool,
    flagged: bool,
    due_only: bool,
    project_name: str | None,
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
    tag_ids: tuple[str, ...],
    clear_tags: bool,
) -> None:
    """Update an existing task."""

    async def _run_task_update() -> None:
        model = await _get_model()
        task = _match_task(model, query)
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
            parent_task_id=task.parent_task_id,
            project_id=task.project_id,
            inbox=task.inbox,
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
    "--format", "fmt", type=click.Choice(["tree", "json"]), default="tree", help="Output format."
)
def projects_cmd(status: str, fmt: str) -> None:
    """Show the folder/project hierarchy."""

    async def _run_projects() -> None:
        model = await _get_model()
        if fmt == "json":
            render_projects_json(model.projects)
        else:
            render_project_tree(model.folders, model.projects, status_filter=status)

    _run(_run_projects())


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
    status: str | None,
) -> None:
    """Update an existing project."""

    async def _run_project_update() -> None:
        model = await _get_model()
        project = _match_project(model, query)
        due_dt = None if clear_due else (_parse_due(due_str) if due_str else project.due)
        defer_dt = None if clear_defer else (_parse_due(defer_str) if defer_str else project.start)
        now = datetime.now(UTC)
        updated = Project(
            id=project.id,
            name=new_name or project.name,
            folder_id=project.folder_id,
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


@cli.command("decrypt-delta")
@click.argument("delta_file", type=click.Path(exists=True, dir_okay=False, path_type=str))
@click.option(
    "--encrypted-plist",
    "encrypted_plist_path",
    default="encrypted.plist",
    show_default=True,
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    help="Path to the bundle 'encrypted' plist file.",
)
def decrypt_delta_cmd(delta_file: str, encrypted_plist_path: str) -> None:
    """Decrypt a transaction ZIP and print its ``contents.xml``."""

    try:
        store = OFocusStore.from_env()
        contents_xml = store.decrypt_transaction_contents_xml(
            encrypted_plist_bytes=Path(encrypted_plist_path).read_bytes(),
            file_bytes=Path(delta_file).read_bytes(),
        )
    except OFWebDAVError as exc:
        raise click.ClickException(f"WebDAV error: {exc}") from exc
    except OFEncryptionError as exc:
        raise click.ClickException(f"Encryption error: {exc}") from exc
    except OFError as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(contents_xml)
