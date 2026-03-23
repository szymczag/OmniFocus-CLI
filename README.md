# omnifocus-cli

Independent CLI and MCP server for OmniFocus 4.

Author: Maciej Szymczak <maciej@szymczak.at>
Release: v1.0.0

Runs in a **Podman container** — no macOS dependencies, no AppleScript.
Syncs directly from a **custom WebDAV server**, decrypts the `.ofocus` bundle,
and exposes task management as a **Claude MCP server**.

## Quick start

```bash
# Build
podman build --target runtime -t omnifocus-cli .

# Create persistent cache
mkdir -p .of-cache

# Sync and list tasks (credentials embedded in URL)
podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  omnifocus-cli of sync

podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  omnifocus-cli of tasks --inbox
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OF_WEBDAV_URL` | Yes | WebDAV bundle URL — credentials may be embedded: `https://user:pass@host/path/` |
| `OF_WEBDAV_USER` | No | WebDAV username (overrides URL-embedded user) |
| `OF_WEBDAV_PASS` | No | WebDAV password (overrides URL-embedded password) |
| `OF_ENCRYPTION_PASSPHRASE` | No | Decryption passphrase — defaults to WebDAV password (linked-password mode) |
| `OF_CACHE_DIR` | No | Cache directory (default repo-local `.of-cache/` when detectable, otherwise `/tmp/of-cache`) |

## Commands

```
of sync
of tasks [--inbox] [--today] [--flagged] [--due] [--project NAME]
of add NAME [--project NAME] [--due DATE] [--flagged] [--note TEXT]
of done QUERY [-y]
of task-update QUERY [options]
of task-drop QUERY [-y]
of projects [--status active|all|inactive] [--format tree|json]
of project-add NAME [options]
of project-update QUERY [options]
of project-done QUERY [-y]
```

## MCP server (Claude integration)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "omnifocus": {
      "command": "podman",
        "args": ["run", "--rm", "-i",
               "-v", "/absolute/path/to/repo/.of-cache:/cache",
               "-e", "OF_CACHE_DIR=/cache",
               "-e", "OF_WEBDAV_URL",
               "omnifocus-cli:latest"]
    }
  }
}
```

The default container command is `of-mcp` (MCP server mode).
Pass `OF_WEBDAV_URL=https://user:pass@host/path/` to avoid separate user/pass vars.
For best performance, keep the MCP container long-lived and reuse the same mounted
`.of-cache/` directory between requests.
