# omnifocus-cli

[![CI](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/ci.yml/badge.svg)](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/ci.yml)
[![Release](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/release.yml/badge.svg)](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](#development)
[![Coverage](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/szymczag/OmniFocus-CLI/badges/coverage-badge.json)](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/ci.yml)
[![Container](https://img.shields.io/github/v/release/szymczag/OmniFocus-CLI?label=container&logo=github)](https://github.com/szymczag/OmniFocus-CLI/pkgs/container/omnifocus-cli)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Independent OmniFocus 4 automation with a production CLI, a secure HTTPS API, and a
container-first MCP server.

`omnifocus-cli` syncs directly with an OmniFocus WebDAV endpoint, reads encrypted `.ofocus`
bundles, exposes a clean `of` command-line interface, runs as an MCP server for
Claude-compatible hosts, and can serve a private HTTPS JSON API for n8n and other automation
clients.

It does **not** require OmniFocus.app, macOS, AppleScript, or Omni Automation. If a host can
reach your OmniFocus WebDAV bundle and provide the right credentials, `omnifocus-cli` can run
there headlessly.

## Why This Project

- Native-feeling CLI for day-to-day task management
- MCP server over stdio for LLM tooling and local assistants
- HTTPS JSON API for n8n-style automations, protected by mandatory TLS 1.3+ and Bearer auth
- Direct WebDAV sync without AppleScript or Omni Automation glue
- No dependency on OmniFocus.app or macOS at runtime
- Headless/container-friendly operation on servers, CI runners, and agent boxes
- Hermetic tests, strict typing, and 100% coverage enforcement
- Podman-friendly runtime image with a non-root default user

## No OmniFocus App Required

`omnifocus-cli` talks to the sync bundle directly.

- Required: WebDAV access to the `.ofocus` bundle and, when enabled, the encryption passphrase
- Not required: OmniFocus.app installed locally
- Not required: macOS
- Not required: System Automation permissions
- Supported deployment shape: local workstation, remote Linux box, CI job, or long-lived MCP host

## Quick Start

### Build the runtime image

```bash
podman build --target runtime -t of .
```

### Create a persistent cache

```bash
mkdir -p .of-cache
```

### Run CLI commands

```bash
# Runs anywhere with network access to the WebDAV bundle
podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  of sync

podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  of tasks --inbox
```

### Run as an MCP host process

```bash
podman run --rm -i \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  of
```

### Run as a private HTTPS API

```bash
podman run --rm \
  -p 127.0.0.1:8443:8443 \
  -v "$PWD/certs":/tls:ro \
  -e OF_HTTP_API_KEY=replace-me \
  -e OF_HTTP_TLS_CERT_FILE=/tls/cert.pem \
  -e OF_HTTP_TLS_KEY_FILE=/tls/key.pem \
  of http
```

### Inspect the CLI surface

```bash
podman run --rm --pull=always of --help
podman run --rm --pull=always of --version
```

## Documentation

- [CLI reference](docs/cli.md) for the exact `of` command surface
- [HTTPS API reference](docs/http.md) for operator guidance around the `/v1` API
- [ASVS 5.0 security mapping](docs/security/asvs-5.0.md) for implemented controls and known gaps
- [MCP reference](docs/mcp.md) for the stdio tool catalog and input contracts
- [Container and runtime notes](docs/container.md) for image behavior and deployment patterns

## Command Model

The project exposes three native Python entrypoints:

- `of` for CLI usage
- `of-mcp` for direct MCP usage
- `of-http` for direct HTTPS API usage

The container image ships with a launcher:

- `podman run --rm of sync`
  Runs the CLI
- `podman run --rm of add "Task name"`
  Runs the CLI
- `podman run --rm -i of`
  Starts the MCP server over stdio
- `podman run --rm -i of mcp`
  Starts the MCP server explicitly
- `podman run --rm of http`
  Starts the HTTPS API with mandatory TLS and Bearer auth

## CLI Usage

The full CLI surface lives in [docs/cli.md](docs/cli.md).

High-level command groups:

- tasks
- projects
- folders
- tags
- sync

## Current Surface

Today the project supports:

- direct WebDAV sync of OmniFocus bundles
- tasks, projects, folders, and tags
- project review workflow over MCP
- HTTPS JSON API for tasks, projects, folders, tags, review, and sync
- MCP over stdio
- headless/container-first deployment

Not yet implemented:

- perspectives
- statistics commands
- dedicated inbox subcommands

## MCP Integration

The detailed MCP tool catalog lives in [docs/mcp.md](docs/mcp.md).

### Container behavior

The runtime image starts the MCP server when you run it without a command:

```bash
podman run --rm -i of
```

That is the correct shape for MCP hosts that manage a long-lived stdio subprocess.
If you track `:latest`, prefer `podman pull` or `--pull=always` before running so a cached local
image does not mask a newer published release.

### Claude-compatible configuration

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
        "/absolute/path/to/repo/.of-cache:/cache",
        "-e",
        "OF_CACHE_DIR=/cache",
        "-e",
        "OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/",
        "of:latest"
      ]
    }
  }
}
```

If you prefer to keep credentials out of the URL, pass `OF_WEBDAV_USER`,
`OF_WEBDAV_PASS`, and optionally `OF_ENCRYPTION_PASSPHRASE` as separate variables.

### MCP deployment notes

- Mount a persistent cache directory for better warm-start performance
- Reuse a long-lived container when your MCP host supports it
- Use `podman run --rm -i of mcp` only if you want an explicit MCP selector

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `OF_WEBDAV_URL` | Yes | WebDAV bundle URL. Credentials may be embedded as `https://user:pass@host/path/`. |
| `OF_WEBDAV_USER` | No | Explicit WebDAV username. Overrides URL-embedded credentials. |
| `OF_WEBDAV_PASS` | No | Explicit WebDAV password. Overrides URL-embedded credentials. |
| `OF_WEBDAV_AUTH` | No | HTTP authentication scheme: `basic` (default) or `digest`. Set to `digest` if your server rejects Basic auth with `401`. |
| `OF_ENCRYPTION_PASSPHRASE` | No | Bundle decryption passphrase. Defaults to the WebDAV password. |
| `OF_CACHE_DIR` | No | Cache directory. Defaults to a repo-local `.of-cache/` when detectable, otherwise `/tmp/of-cache`. |

## Distribution

The project ships as:

- a Python package with `of` and `of-mcp`
- a container image for CLI and MCP usage
- a GitHub Release with wheel and sdist artifacts
- a GHCR runtime image published by the release workflow

The runtime container is based on `python:3.14-slim`, runs as a non-root user, and is published
for both `linux/amd64` and `linux/arm64`.
PyPI publishing is intentionally out of scope for the current release process.

## Development

For local setup, quality checks, and release workflow details, see
[CONTRIBUTING.md](CONTRIBUTING.md).

## Changelog

Release notes live in [CHANGELOG.md](CHANGELOG.md).

## Author

Maciej Szymczak  
[maciej@szymczak.at](mailto:maciej@szymczak.at)
