"""MCP server for OmniFocus CLI.

Exposes OmniFocus task management as MCP tools consumable by Claude.
Runs over stdio transport (suitable for ``podman run --rm -i``).

Tools
-----
``list_tasks``      Filter active tasks by inbox/today/flagged/project/due.
``search_tasks``    Fuzzy search tasks by name.
``get_task``        Retrieve a single task by id.
``add_task``        Create a new task.
``complete_task``   Mark a task as completed.
``update_task``     Update name, due date, flagged, or note on a task.
``add_project``     Create a new project.
``update_project``  Update a project.
``complete_project`` Mark a project completed.
``list_projects``   List projects (optionally filtered by status).
``list_folders``    List all folders.
``sync_now``        Trigger a full WebDAV sync.
``sync_status``     Report last sync time and cache state.

Usage::

    # Default: MCP server mode (stdin/stdout)
    of-mcp

    # In Claude MCP config (settings.json):
    {
      "mcpServers": {
        "omnifocus": {
          "command": "podman",
          "args": ["run", "--rm", "-i",
                   "-e", "OF_WEBDAV_URL",
                   "-e", "OF_WEBDAV_USER",
                   "-e", "OF_WEBDAV_PASS",
                   "-e", "OF_ENCRYPTION_PASSPHRASE",
                   "omnifocus-cli:latest"]
        }
      }
    }
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from datetime import datetime, timezone
from typing import Any

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from omnifocus.errors import OFError
from omnifocus.formatting import _json_default
from omnifocus.fuzzy import find_tasks
from omnifocus.models import OFModel, Project, Task
from omnifocus.store import OFocusStore

# ---------------------------------------------------------------------------
# Server instance
# ---------------------------------------------------------------------------

server: Server = Server("omnifocus")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _serialise(obj: Any) -> Any:
    """Recursively serialise an object to a JSON-safe form."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialise(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _serialise(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialise(item) for item in obj]
    return obj


def _text(data: Any) -> list[TextContent]:
    """Wrap any JSON-serialisable data as a list of MCP TextContent."""
    return [TextContent(type="text", text=json.dumps(_serialise(data), ensure_ascii=False, indent=2))]


async def _load_model(force: bool = False) -> OFModel:
    """Load the current OFModel via the store."""
    async with OFocusStore.from_env() as store:
        return await store.load(force_refresh=force)


def _parse_optional_date(value: str | None) -> datetime | None:
    """Parse an ISO 8601 date string or return None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Tool: list_tasks
# ---------------------------------------------------------------------------


@server.list_tools()
async def list_tools() -> list[Tool]:
    """Return the list of all MCP tools provided by this server."""
    return [
        Tool(
            name="list_tasks",
            description=(
                "List active OmniFocus tasks. "
                "Optionally filter by inbox, today (due today or overdue), "
                "flagged, project name (substring), or tasks with a due date."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "inbox": {"type": "boolean", "description": "Inbox tasks only"},
                    "today": {"type": "boolean", "description": "Due today or overdue"},
                    "flagged": {"type": "boolean", "description": "Flagged tasks only"},
                    "due": {"type": "boolean", "description": "Tasks with any due date"},
                    "project": {"type": "string", "description": "Project name substring"},
                    "limit": {"type": "integer", "description": "Max tasks to return (default 50)"},
                },
            },
        ),
        Tool(
            name="search_tasks",
            description="Fuzzy search tasks by name or ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "integer", "description": "Max results (default 10)"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="get_task",
            description="Get a single task by its OmniFocus ID.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "Task ID"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="add_task",
            description="Create a new OmniFocus task.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Task name"},
                    "project": {"type": "string", "description": "Project name (substring)"},
                    "due": {"type": "string", "description": "Due date ISO 8601 or natural (today/tomorrow/mon-sun)"},
                    "flagged": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="complete_task",
            description="Mark a task as completed by ID or fuzzy name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Task ID or name fragment"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="update_task",
            description="Update a task's name, due date, flagged status, or note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "task_id": {"type": "string"},
                    "name": {"type": "string"},
                    "due": {"type": "string", "description": "ISO 8601 datetime or empty to clear"},
                    "flagged": {"type": "boolean"},
                    "note": {"type": "string"},
                },
                "required": ["task_id"],
            },
        ),
        Tool(
            name="add_project",
            description="Create a new OmniFocus project.",
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "folder": {"type": "string", "description": "Folder name substring"},
                    "due": {"type": "string", "description": "Due date ISO 8601 or natural"},
                    "defer": {"type": "string", "description": "Defer date ISO 8601 or natural"},
                    "flagged": {"type": "boolean"},
                    "note": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "inactive"]},
                },
                "required": ["name"],
            },
        ),
        Tool(
            name="update_project",
            description="Update a project's name, due date, defer date, flagged status, or note.",
            inputSchema={
                "type": "object",
                "properties": {
                    "project_id": {"type": "string"},
                    "name": {"type": "string"},
                    "due": {"type": "string", "description": "ISO 8601 datetime or empty to clear"},
                    "defer": {"type": "string", "description": "ISO 8601 datetime or empty to clear"},
                    "flagged": {"type": "boolean"},
                    "note": {"type": "string"},
                    "status": {"type": "string", "enum": ["active", "inactive", "done", "dropped"]},
                },
                "required": ["project_id"],
            },
        ),
        Tool(
            name="complete_project",
            description="Mark a project as completed by ID or fuzzy name.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Project ID or name fragment"},
                },
                "required": ["query"],
            },
        ),
        Tool(
            name="list_projects",
            description="List OmniFocus projects.",
            inputSchema={
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "all", "inactive", "done", "dropped"],
                        "description": "Filter by status (default: active)",
                    },
                },
            },
        ),
        Tool(
            name="list_folders",
            description="List all OmniFocus folders.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="sync_now",
            description="Trigger a full sync from the WebDAV server.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="sync_status",
            description="Report last sync time and cache state.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    """Dispatch incoming tool calls to the appropriate handler."""
    handlers: dict[str, Any] = {
        "list_tasks": _handle_list_tasks,
        "search_tasks": _handle_search_tasks,
        "get_task": _handle_get_task,
        "add_task": _handle_add_task,
        "complete_task": _handle_complete_task,
        "update_task": _handle_update_task,
        "add_project": _handle_add_project,
        "update_project": _handle_update_project,
        "complete_project": _handle_complete_project,
        "list_projects": _handle_list_projects,
        "list_folders": _handle_list_folders,
        "sync_now": _handle_sync_now,
        "sync_status": _handle_sync_status,
    }
    handler = handlers.get(name)
    if handler is None:
        return _text({"error": f"Unknown tool: {name}"})
    try:
        return await handler(arguments)
    except OFError as exc:
        return _text({"error": str(exc)})


async def _handle_list_tasks(args: dict[str, Any]) -> list[TextContent]:
    model = await _load_model()
    tasks = model.active_tasks
    limit = int(args.get("limit", 50))

    if args.get("inbox"):
        tasks = [t for t in tasks if t.inbox]
    if args.get("today"):
        now_date = datetime.today().date()
        tasks = [t for t in tasks if t.due is not None and t.due.date() <= now_date]
    if args.get("flagged"):
        tasks = [t for t in tasks if t.flagged]
    if args.get("due"):
        tasks = [t for t in tasks if t.due is not None]
    if args.get("project"):
        needle = args["project"].lower()
        matching = {
            pid for pid, p in model.projects.items()
            if needle in p.name.lower()
        }
        tasks = [t for t in tasks if t.project_id in matching]

    return _text([_task_summary(t, model) for t in tasks[:limit]])


async def _handle_search_tasks(args: dict[str, Any]) -> list[TextContent]:
    query = str(args.get("query", ""))
    limit = int(args.get("limit", 10))
    model = await _load_model()
    results = find_tasks(query, model.active_tasks, limit=limit)
    return _text([
        {"score": round(r.score, 3), **_task_summary(r.task, model)}
        for r in results
    ])


async def _handle_get_task(args: dict[str, Any]) -> list[TextContent]:
    task_id = str(args.get("task_id", ""))
    model = await _load_model()
    task = model.tasks.get(task_id)
    if task is None:
        return _text({"error": f"Task not found: {task_id}"})
    return _text(_task_summary(task, model))


async def _handle_add_task(args: dict[str, Any]) -> list[TextContent]:
    from omnifocus.cli import _parse_due
    import click

    name = str(args.get("name", ""))
    if not name:
        return _text({"error": "name is required"})

    model = await _load_model()
    parent_task_id: str | None = None
    inbox = True

    if args.get("project"):
        needle = str(args["project"]).lower()
        matches = [
            p for p in model.projects.values()
            if needle in p.name.lower() and p.status == "active"
        ]
        if not matches:
            return _text({"error": f"No active project matching {args['project']!r}"})
        parent_task_id = matches[0].id
        inbox = False

    due_dt: datetime | None = None
    if args.get("due"):
        try:
            due_dt = _parse_due(str(args["due"]))
        except click.BadParameter:
            try:
                due_dt = datetime.fromisoformat(str(args["due"]))
            except ValueError:
                return _text({"error": f"Invalid due date: {args['due']!r}"})

    async with OFocusStore.from_env() as store:
        result = await store.add_task(
            name=name,
            parent_task_id=parent_task_id,
            inbox=inbox,
            flagged=bool(args.get("flagged", False)),
            due_dt=due_dt,
            note=str(args.get("note", "")),
        )

    return _text(result)


async def _handle_complete_task(args: dict[str, Any]) -> list[TextContent]:
    query = str(args.get("query", ""))
    model = await _load_model()
    results = find_tasks(query, model.active_tasks, limit=5)
    if not results:
        return _text({"error": f"No active task matching {query!r}"})
    task = results[0].task

    async with OFocusStore.from_env() as store:
        result = await store.complete_task(task)

    return _text(result)


async def _handle_update_task(args: dict[str, Any]) -> list[TextContent]:
    task_id = str(args.get("task_id", ""))
    model = await _load_model()
    task = model.tasks.get(task_id)
    if task is None:
        return _text({"error": f"Task not found: {task_id}"})

    # Build updated task by replacing changed fields
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    updated = dataclasses.replace(
        task,
        name=str(args["name"]) if "name" in args else task.name,
        flagged=bool(args["flagged"]) if "flagged" in args else task.flagged,
        note=str(args["note"]) if "note" in args else task.note,
        due=_parse_optional_date(str(args["due"])) if "due" in args else task.due,
        modified=now,
    )

    async with OFocusStore.from_env() as store:
        result = await store.update_task(updated)

    return _text(result)


async def _handle_add_project(args: dict[str, Any]) -> list[TextContent]:
    from omnifocus.cli import _parse_due
    import click

    name = str(args.get("name", ""))
    if not name:
        return _text({"error": "name is required"})

    model = await _load_model()
    folder_id: str | None = None
    if args.get("folder"):
        needle = str(args["folder"]).lower()
        matches = [folder for folder in model.folders.values() if needle in folder.name.lower()]
        if not matches:
            return _text({"error": f"No folder matching {args['folder']!r}"})
        if len(matches) > 1:
            return _text({"error": f"Multiple folders match {args['folder']!r}"})
        folder_id = matches[0].id

    def _parse_natural(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return _parse_due(str(value))
        except click.BadParameter:
            try:
                return datetime.fromisoformat(str(value))
            except ValueError:
                return None

    due_dt = _parse_natural(args.get("due"))
    if args.get("due") and due_dt is None:
        return _text({"error": f"Invalid due date: {args['due']!r}"})
    defer_dt = _parse_natural(args.get("defer"))
    if args.get("defer") and defer_dt is None:
        return _text({"error": f"Invalid defer date: {args['defer']!r}"})

    async with OFocusStore.from_env() as store:
        result = await store.add_project(
            name=name,
            folder_id=folder_id,
            status=str(args.get("status", "active")),
            flagged=bool(args.get("flagged", False)),
            due_dt=due_dt,
            start_dt=defer_dt,
            note=str(args.get("note", "")),
        )

    return _text(result)


async def _handle_update_project(args: dict[str, Any]) -> list[TextContent]:
    project_id = str(args.get("project_id", ""))
    model = await _load_model()
    project = model.projects.get(project_id)
    if project is None:
        return _text({"error": f"Project not found: {project_id}"})

    updated = Project(
        id=project.id,
        name=str(args["name"]) if "name" in args else project.name,
        folder_id=project.folder_id,
        status=str(args["status"]) if "status" in args else project.status,
        singleton=project.singleton,
        rank=project.rank,
        added=project.added,
        modified=datetime.now(timezone.utc),
        flagged=bool(args["flagged"]) if "flagged" in args else project.flagged,
        due=_parse_optional_date(str(args["due"])) if "due" in args else project.due,
        start=_parse_optional_date(str(args["defer"])) if "defer" in args else project.start,
        note=str(args["note"]) if "note" in args else project.note,
        completed=project.completed,
        tag_ids=project.tag_ids,
    )
    if "status" in args and args["status"] == "done" and updated.completed is None:
        updated = dataclasses.replace(updated, completed=datetime.now(timezone.utc))

    async with OFocusStore.from_env() as store:
        result = await store.update_project(updated)

    return _text(result)


async def _handle_complete_project(args: dict[str, Any]) -> list[TextContent]:
    query = str(args.get("query", ""))
    model = await _load_model()
    project = model.projects.get(query)
    if project is None:
        needle = query.lower()
        matches = [candidate for candidate in model.projects.values() if needle in candidate.name.lower()]
        if not matches:
            return _text({"error": f"No project matching {query!r}"})
        if len(matches) > 1:
            return _text({"error": f"Multiple projects match {query!r}"})
        project = matches[0]

    async with OFocusStore.from_env() as store:
        result = await store.complete_project(project)

    return _text(result)


async def _handle_list_projects(args: dict[str, Any]) -> list[TextContent]:
    status = str(args.get("status", "active"))
    model = await _load_model()
    projects = [
        p for p in model.projects.values()
        if status == "all" or p.status == status
    ]
    return _text([dataclasses.asdict(p) for p in projects])


async def _handle_list_folders(args: dict[str, Any]) -> list[TextContent]:
    model = await _load_model()
    return _text([dataclasses.asdict(f) for f in model.folders.values()])


async def _handle_sync_now(args: dict[str, Any]) -> list[TextContent]:
    async with OFocusStore.from_env() as store:
        model = await store.load(force_refresh=True)
    return _text({
        "status": "synced",
        "tasks": len(model.tasks),
        "projects": len(model.projects),
        "folders": len(model.folders),
    })


async def _handle_sync_status(args: dict[str, Any]) -> list[TextContent]:
    async with OFocusStore.from_env() as store:
        status = await store.sync_status()
    return _text(status)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task_summary(task: Task, model: OFModel) -> dict[str, Any]:
    """Return a concise dict representation of a task."""
    proj = model.projects.get(task.project_id or "")
    return {
        "id": task.id,
        "name": task.name,
        "project": proj.name if proj else None,
        "inbox": task.inbox,
        "flagged": task.flagged,
        "due": task.due.isoformat() if task.due else None,
        "start": task.start.isoformat() if task.start else None,
        "completed": task.completed.isoformat() if task.completed else None,
        "note": task.note,
        "tag_ids": list(task.tag_ids),
    }


def _build_tx_for_task(task: Task) -> "TransactionBuilder":
    """Build a transaction that upserts the given task element."""
    from omnifocus.writer import TransactionBuilder
    builder = TransactionBuilder()
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
        hidden_dt=task.hidden,
    )
    return builder


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Start the MCP server over stdio.

    This is the entry point registered as ``of-mcp`` in ``pyproject.toml``.
    """

    async def _serve() -> None:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream,
                write_stream,
                server.create_initialization_options(),
            )

    asyncio.run(_serve())


if __name__ == "__main__":  # pragma: no cover
    main()
