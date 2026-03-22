# omnifocus-cli

Independent CLI and MCP server for OmniFocus 4.

Runs in a **Podman container** — no macOS dependencies, no AppleScript.
Syncs directly from a **custom WebDAV server**, decrypts the `.ofocus` bundle,
and exposes task management as a **Claude MCP server**.

## Quick start

```bash
# Build
podman build --target runtime -t omnifocus-cli .

# Sync and list tasks (credentials embedded in URL)
podman run --rm \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  omnifocus-cli of --debug sync

podman run --rm \
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
| `OF_CACHE_DIR` | No | Cache directory (default `/tmp/of-cache`) |

## Commands

```
of [--debug] sync                   Pull latest bundle from WebDAV
of [--debug] tasks [--inbox] [--today] [--flagged] [--due] [--project NAME]
of [--debug] add NAME [--project NAME] [--due DATE] [--flagged] [--note TEXT]
of [--debug] done QUERY [-y]
of [--debug] projects [--status active|all] [--format tree|json]
```

`--debug` prints verbose logs to stderr (WebDAV requests, decryption, parsing).

## MCP server (Claude integration)

Add to `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "omnifocus": {
      "command": "podman",
      "args": ["run", "--rm", "-i",
               "-e", "OF_WEBDAV_URL",
               "omnifocus-cli:latest"]
    }
  }
}
```

The default container command is `of-mcp` (MCP server mode).
Pass `OF_WEBDAV_URL=https://user:pass@host/path/` to avoid separate user/pass vars.
