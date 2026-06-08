# MCP Reference

`omnifocus-cli` can run as an MCP server over stdio.

Default container behavior:

```bash
podman run --rm -i of
```

Explicit mode:

```bash
podman run --rm -i of mcp
```

## Host Configuration

```json
{
  "mcpServers": {
    "omnifocus": {
      "command": "podman",
      "args": [
        "run",
        "--rm",
        "-i",
        "-v",
        "/absolute/path/to/cache:/cache",
        "-e",
        "OF_CACHE_DIR=/cache",
        "-e",
        "OF_WEBDAV_URL=https://dav.example.com/OmniFocus.ofocus/",
        "-e",
        "OF_WEBDAV_USER=username",
        "-e",
        "OF_WEBDAV_PASS=password",
        "-e",
        "OF_ENCRYPTION_PASSPHRASE=passphrase",
        "of:latest"
      ]
    }
  }
}
```

If your WebDAV server requires Digest authentication rather than Basic, add
`-e OF_WEBDAV_AUTH=digest` to the `args` list (the default is `basic`).

## Tool Surface

### Tasks

#### `list_tasks`

- Required inputs: none
- Optional inputs: `inbox`, `today`, `flagged`, `due`, `project`, `tag`, `tag_id`, `limit`
- Response: list of task summaries

#### `search_tasks`

- Required inputs: `query`
- Optional inputs: `limit`
- Response: list of task summaries with a `score`

#### `get_task`

- Required inputs: `task_id`
- Optional inputs: none
- Response: one task summary

#### `add_task`

- Required inputs: `name`
- Optional inputs: `project`, `due`, `flagged`, `note`
- Semantics: `project` is a fuzzy folder/project-name convenience input, not a stable ID
- Response: mutation status plus created task ID

#### `complete_task`

- Required inputs: `query`
- Optional inputs: none
- Semantics: accepts a task ID or fuzzy task-name fragment
- Response: mutation status plus affected task ID

#### `update_task`

- Required inputs: `task_id`
- Optional inputs:
  - `name`
  - `project_id`
  - `clear_project`
  - `inbox`
  - `due`
  - `defer`
  - `flagged`
  - `note`
  - `estimate`
  - `tag_ids`
  - `clear_tags`
  - `dropped`
- Semantics:
  - `task_id` is stable-ID based
  - `project_id` and `clear_project` conflict
  - `project_id` and `inbox=true` conflict
  - `tag_ids` replaces assigned tags
  - `clear_tags` removes all tag assignments
  - `dropped=true` hides the task
- Response: mutation status plus affected task ID

Task responses include:
- `id`
- `name`
- `project`
- `inbox`
- `flagged`
- `due`
- `start`
- `completed`
- `note`
- `tag_ids`
- `tag_names`

### Projects

#### `list_projects`

- Required inputs: none
- Optional inputs: `status`, `tag`, `tag_id`
- Response: list of project summaries

#### `get_project`

- Required inputs: `project_id`
- Optional inputs: none
- Response: one project summary

#### `add_project`

- Required inputs: `name`
- Optional inputs: `folder`, `due`, `defer`, `flagged`, `note`, `status`
- Semantics:
  - `folder` is a fuzzy folder-name convenience input
  - `status` is limited to `active` or `inactive`
- Response: mutation status plus created project ID

#### `update_project`

- Required inputs: `project_id`
- Optional inputs:
  - `name`
  - `folder_id`
  - `clear_folder`
  - `due`
  - `defer`
  - `flagged`
  - `note`
  - `status`
  - `tag_ids`
  - `clear_tags`
- Semantics:
  - `project_id` is stable-ID based
  - `folder_id` and `clear_folder` conflict
  - `tag_ids` replaces assigned tags
  - `clear_tags` removes all tag assignments
  - `status=done` completes the project
  - `status=dropped` drops the project
- Response: mutation status plus affected project ID

#### `complete_project`

- Required inputs: `query`
- Optional inputs: none
- Semantics: accepts a project ID or fuzzy project-name fragment
- Response: mutation status plus affected project ID

Project responses include:
- core project fields from the parsed model
- `folder_name`
- `tag_names`
- `review_due`
- `review_basis`

### Project Review

#### `list_projects_for_review`

- Required inputs: none
- Optional inputs: `due_only`, `limit`
- Semantics:
  - defaults to active and inactive projects only
  - defaults to due-for-review projects only
  - returns most overdue projects first
- Response: list of project summaries with review metadata

#### `mark_project_reviewed`

- Required inputs: `project_id`
- Optional inputs: `reviewed_at`
- Semantics:
  - stamps `last_review`
  - recalculates `next_review` when the stored interval can be parsed
- Response: updated project summary plus `next_review_recalculated`

Review fields:
- `last_review`
- `next_review`
- `review_interval`
- `review_due`
- `review_basis`

### Folders

#### `list_folders`

- Required inputs: none
- Optional inputs: none
- Response: list of folder summaries

#### `get_folder`

- Required inputs: `folder_id`
- Optional inputs: none
- Response: one folder summary

#### `get_folder_tree`

- Required inputs: none
- Optional inputs: none
- Response: nested folder hierarchy with direct child projects

#### `add_folder`

- Required inputs: `name`
- Optional inputs: `parent_folder_id`
- Response: mutation status plus created folder ID

#### `update_folder`

- Required inputs: `folder_id`
- Optional inputs: `name`, `parent_folder_id`, `clear_parent`
- Semantics:
  - validates parent existence
  - rejects self-parenting and cycles
- Response: mutation status plus affected folder ID

#### `drop_folder`

- Required inputs: `folder_id`
- Optional inputs: none
- Response: mutation status plus affected folder ID

### Tags

#### `list_tags`

- Required inputs: none
- Optional inputs: `all`
- Semantics: hidden/dropped tags are excluded unless `all=true`
- Response: list of tag summaries

#### `get_tag`

- Required inputs: `tag_id`
- Optional inputs: none
- Response: one tag summary

#### `add_tag`

- Required inputs: `name`
- Optional inputs: `parent_tag_id`, `note`
- Response: mutation status plus created tag ID

#### `update_tag`

- Required inputs: `tag_id`
- Optional inputs: `name`, `parent_tag_id`, `clear_parent`, `note`
- Semantics:
  - validates parent existence
  - rejects self-parenting and cycles
- Response: mutation status plus affected tag ID

#### `drop_tag`

- Required inputs: `tag_id`
- Optional inputs: none
- Response: mutation status plus affected tag ID

Tag responses include:
- core tag fields from the parsed model
- `parent_name`
- `child_tag_ids`

### Sync

#### `sync_now`

- Required inputs: none
- Optional inputs: none
- Response: top-level object counts after a forced refresh

## Behavioral Notes

- MCP tools return structured dictionaries serialized as JSON text content.
- Mutation tools prefer stable IDs where the tool contract exposes them.
- MCP keeps some convenience flows that HTTP intentionally does not, especially fuzzy `query`-based completion and fuzzy `project` / `folder` name inputs during creation.
- Tags are first-class entities; assigning tags to tasks or projects is separate from creating/updating tag objects.

## Not Yet Implemented

- perspectives
- statistics tools
- dedicated inbox MCP tools
