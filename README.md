# omnifocus-cli

Independent CLI and MCP server for OmniFocus 4.

Runs in a **Podman container** — no macOS dependencies, no AppleScript.
Syncs directly from a **custom WebDAV server**, decrypts the `.ofocus` bundle,
and exposes task management as a **Claude MCP server**.

## Quick start

```bash
# Build
podman build --target test -t omnifocus-cli-test .
podman run --rm omnifocus-cli-test

podman build --target runtime -t omnifocus-cli .

# Use CLI
podman run --rm \
  -e OF_WEBDAV_URL=https://dav.example.com/omnifocus/OmniFocus.ofocus/ \
  -e OF_WEBDAV_USER=user \
  -e OF_WEBDAV_PASS=pass \
  -e OF_ENCRYPTION_PASSPHRASE=secret \
  omnifocus-cli of tasks --inbox
```

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `OF_WEBDAV_URL` | Yes | WebDAV bundle URL (must end with `/`) |
| `OF_WEBDAV_USER` | Yes | WebDAV username |
| `OF_WEBDAV_PASS` | Yes | WebDAV password |
| `OF_ENCRYPTION_PASSPHRASE` | If encrypted | Database passphrase |
| `OF_CACHE_DIR` | No | Cache directory (default `/tmp/of-cache`) |

## Commands

```
of sync                         Pull latest bundle from WebDAV
of tasks [--inbox] [--today] [--flagged] [--due] [--project NAME]
of add NAME [--project NAME] [--due DATE] [--flagged] [--note TEXT]
of done QUERY [-y]
of projects [--status active|all] [--format tree|json]
```

## MCP server (Claude integration)

Add to `~/.claude/settings.json`:

```json
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
```
