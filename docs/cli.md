# CLI Reference

`omnifocus-cli` exposes the `of` command for direct OmniFocus bundle operations over WebDAV.

The CLI runs without OmniFocus.app and without macOS-specific automation APIs.

## Global

```text
of --help
of --version
of sync
```

## Connection and Authentication

The CLI is configured entirely through environment variables. At minimum, set the WebDAV bundle
URL and credentials:

```text
OF_WEBDAV_URL    WebDAV bundle URL (credentials may be embedded as https://user:pass@host/path/)
OF_WEBDAV_USER   Explicit WebDAV username (overrides URL-embedded credentials)
OF_WEBDAV_PASS   Explicit WebDAV password (overrides URL-embedded credentials)
OF_WEBDAV_AUTH   Authentication scheme: basic (default) or digest
```

By default the client authenticates with HTTP Basic. If your WebDAV server requires Digest
authentication (a common cause of a `401` even when the username and password are correct)
set `OF_WEBDAV_AUTH=digest`. Any other value is rejected at startup.

See the [README environment-variable table](../README.md#environment-variables) for the full list,
including `OF_ENCRYPTION_PASSPHRASE` and `OF_CACHE_DIR`.

## Tasks

```text
of tasks [--inbox] [--today] [--flagged] [--due] [--project NAME] [--tag NAME] [--all] [--format table|json]
of add NAME [--project NAME] [--due DATE] [--flagged] [--note TEXT]
of done QUERY [-y]
of task-update QUERY [--name NAME] [--note TEXT] [--flagged|--unflagged]
                    [--due DATE|--clear-due] [--defer DATE|--clear-defer]
                    [--estimate MINUTES|--clear-estimate]
                    [--project-id PROJECT_ID|--clear-project|--inbox]
                    [--tag-id TAG_ID ...|--clear-tags]
of task-drop QUERY [-y]
```

Notes:
- `QUERY` uses the existing fuzzy/name substring workflow.
- `--tag-id` is for assigning existing tags to tasks.
- `tasks --tag` filters by tag name, not by id.

## Projects

```text
of projects [--status active|inactive|all] [--tag NAME] [--format tree|json]
of project-add NAME [--folder NAME] [--note TEXT] [--flagged]
                    [--due DATE] [--defer DATE]
                    [--status active|inactive]
of project-update QUERY [--name NAME] [--note TEXT] [--flagged|--unflagged]
                        [--due DATE|--clear-due] [--defer DATE|--clear-defer]
                        [--folder-id FOLDER_ID|--clear-folder]
                        [--tag-id TAG_ID ...|--clear-tags]
                        [--status active|inactive|done|dropped]
of project-done QUERY [-y]
```

Notes:
- `projects` is a project-centric grouped view, not a second copy of the folder tree.
- `--tag-id` assigns existing tags to projects.
- `projects --tag` filters by tag name.

## Folders

```text
of folders [--format tree|json]
of folder-add NAME [--parent-id FOLDER_ID]
of folder-update QUERY [--name NAME] [--parent-id FOLDER_ID|--clear-parent]
of folder-drop QUERY [-y]
```

## Tags

OmniFocus stores tags as `<context>` elements internally. Public CLI terminology stays `tag`.

```text
of tags [--format tree|json] [--all]
of tag-add NAME [--parent QUERY|--parent-id TAG_ID] [--note TEXT]
of tag-update QUERY [--name NAME]
                    [--parent QUERY|--parent-id TAG_ID|--clear-parent]
                    [--note TEXT]
of tag-drop QUERY [-y]
```

Notes:
- `tags` excludes dropped/hidden tags by default; use `--all` to include them.
- `tag-add` / `tag-update` manage tag entities themselves.
- `--tag-id` on task/project mutation commands assigns already-existing tags.

## Output Modes

- `tasks`: `table` or `json`
- `projects`: `tree` or `json`
- `folders`: `tree` or `json`
- `tags`: `tree` or `json`

## Not Yet Implemented

- perspectives
- statistics commands
- dedicated inbox subcommands
- dedicated `task view` and `project view` CLI workflows
- a dedicated `project-drop` command separate from `project-update --status dropped`
