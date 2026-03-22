"""Click CLI entry point for omnifocus-cli.

Provides the ``of`` command group with subcommands:

- ``of sync``      — pull the latest bundle from WebDAV
- ``of tasks``     — list tasks with filters
- ``of add``       — add a task
- ``of done``      — mark a task complete
- ``of projects``  — show the folder/project tree

All WebDAV credentials and the encryption passphrase are read from
environment variables (see :mod:`omnifocus.store`).
"""

from __future__ import annotations

import asyncio
import re
import sys
from datetime import datetime, timedelta
from typing import Optional

import click

from omnifocus.errors import (
    OFAmbiguousMatch,
    OFBundleNotFound,
    OFEncryptionError,
    OFError,
    OFTaskNotFound,
    OFWebDAVError,
)
from omnifocus.formatting import (
    render_project_tree,
    render_projects_json,
    render_tasks_json,
    render_tasks_table,
)
from omnifocus.fuzzy import find_tasks
from omnifocus.models import OFModel
from omnifocus.store import OFocusStore
from omnifocus.writer import TaskWriter


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


# ---------------------------------------------------------------------------
# CLI group
# ---------------------------------------------------------------------------


@click.group()
def cli() -> None:
    """OmniFocus 4 command-line interface.

    Reads credentials from environment variables:

    \b
    OF_WEBDAV_URL          WebDAV bundle URL (required)
    OF_WEBDAV_USER         WebDAV username (required)
    OF_WEBDAV_PASS         WebDAV password (required)
    OF_ENCRYPTION_PASSPHRASE  Database passphrase (if encrypted)
    OF_CACHE_DIR           Cache directory (default: /tmp/of-cache)
    """


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
                total = len(model.tasks) + len(model.projects)
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


# ---------------------------------------------------------------------------
# of tasks
# ---------------------------------------------------------------------------


@cli.command("tasks")
@click.option("--inbox", is_flag=True, help="Show only inbox tasks.")
@click.option("--today", is_flag=True, help="Show tasks due today or overdue.")
@click.option("--flagged", is_flag=True, help="Show only flagged tasks.")
@click.option("--due", "due_only", is_flag=True, help="Show only tasks with a due date.")
@click.option("--project", "project_name", default=None, metavar="NAME",
              help="Filter by project name (substring, case-insensitive).")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]),
              default="table", help="Output format.")
@click.option("--all", "show_all", is_flag=True, help="Include completed tasks.")
def tasks_cmd(
    inbox: bool,
    today: bool,
    flagged: bool,
    due_only: bool,
    project_name: Optional[str],
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
                pid for pid, p in model.projects.items()
                if needle in p.name.lower()
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
@click.option("--project", "project_name", default=None, metavar="NAME",
              help="Add to this project (substring match).")
@click.option("--due", "due_str", default=None, metavar="DATE",
              help="Due date: YYYY-MM-DD, today, tomorrow, mon-sun.")
@click.option("--flagged", is_flag=True, help="Mark as flagged.")
@click.option("--note", default=None, metavar="TEXT", help="Task note.")
def add_cmd(
    name: str,
    project_name: Optional[str],
    due_str: Optional[str],
    flagged: bool,
    note: Optional[str],
) -> None:
    """Add a task to inbox or a specific project.

    NAME is the task display name.
    """

    async def _run_add() -> None:
        due_dt: Optional[datetime] = None
        if due_str:
            due_dt = _parse_due(due_str)

        model = await _get_model()
        parent_task_id: Optional[str] = None
        inbox = True

        if project_name:
            needle = project_name.lower()
            matches = [
                p for p in model.projects.values()
                if needle in p.name.lower() and p.status == "active"
            ]
            if not matches:
                raise click.ClickException(
                    f"No active project matching {project_name!r}"
                )
            if len(matches) > 1:
                names = ", ".join(m.name for m in matches[:5])
                raise click.ClickException(
                    f"Multiple projects match {project_name!r}: {names}. "
                    "Be more specific."
                )
            parent_task_id = matches[0].id
            inbox = False

        writer = TaskWriter()
        fname, data, new_id = writer.add_task(
            name=name,
            parent_task_id=parent_task_id,
            inbox=inbox,
            flagged=flagged,
            due_dt=due_dt,
            note=note or "",
        )

        try:
            async with OFocusStore.from_env() as store:
                store.invalidate_cache()
                await store._client.put_file(fname, data)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc

        click.echo(f"Added task: {name!r} (id={new_id})")

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
        active = model.active_tasks

        results = find_tasks(query, active)
        if not results:
            raise click.ClickException(f"No active task matching {query!r}")

        if len(results) > 1 and results[0].score < 0.8:
            choices = "\n".join(
                f"  [{i + 1}] {r.task.id}  {r.task.name}"
                for i, r in enumerate(results[:5])
            )
            raise click.ClickException(
                f"Ambiguous match for {query!r}. Did you mean one of:\n{choices}"
            )

        task = results[0].task

        if not yes:
            click.confirm(f"Complete task: {task.name!r}?", abort=True)

        writer = TaskWriter()
        fname, data = writer.complete_task(task)

        try:
            async with OFocusStore.from_env() as store:
                store.invalidate_cache()
                await store._client.put_file(fname, data)
        except OFWebDAVError as exc:
            raise click.ClickException(f"WebDAV error: {exc}") from exc

        click.echo(f"Completed: {task.name!r}")

    _run(_run_done())


# ---------------------------------------------------------------------------
# of projects
# ---------------------------------------------------------------------------


@cli.command("projects")
@click.option("--status", type=click.Choice(["active", "all", "inactive"]),
              default="active", help="Filter by project status.")
@click.option("--format", "fmt", type=click.Choice(["tree", "json"]),
              default="tree", help="Output format.")
def projects_cmd(status: str, fmt: str) -> None:
    """Show the folder/project hierarchy."""

    async def _run_projects() -> None:
        model = await _get_model()
        if fmt == "json":
            render_projects_json(model.projects)
        else:
            render_project_tree(model.folders, model.projects, status_filter=status)

    _run(_run_projects())
