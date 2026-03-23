# omnifocus-cli

[![CI](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/ci.yml/badge.svg)](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/ci.yml)
[![Release](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/release.yml/badge.svg)](https://github.com/szymczag/OmniFocus-CLI/actions/workflows/release.yml)
[![Python](https://img.shields.io/badge/python-3.14-3776AB?logo=python&logoColor=white)](#development)
[![Coverage](https://img.shields.io/badge/coverage-100%25-2ea44f)](#quality-bar)
[![GHCR](https://img.shields.io/badge/ghcr-container%20release-0f172a?logo=github)](#packaging)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Independent OmniFocus 4 automation for people who want a real CLI and a real MCP server instead of AppleScript glue.

`omnifocus-cli` connects directly to an OmniFocus WebDAV sync target, parses and decrypts the `.ofocus` bundle, exposes a production CLI as `of`, and ships a container-first MCP server for Claude-compatible clients.

## Highlights

- Native-feeling CLI: `of sync`, `of tasks`, `of add`, `of done`
- MCP server over stdio for Claude and other MCP-compatible hosts
- Direct WebDAV sync support with encrypted OmniFocus bundles
- Strict quality bar: `ruff`, `black`, `mypy --strict`, `pytest`, 100% coverage
- Container-first distribution with a non-root runtime image
- GitHub Actions CI/CD for linting, typing, test coverage, artifact builds, and release publishing
- Dependabot updates for Python dependencies and GitHub Actions

## Quick Start

### Build the runtime image

```bash
podman build --target runtime -t of .
```

### Prepare a persistent cache

```bash
mkdir -p .of-cache
```

### Run CLI commands

```bash
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

### Discover the CLI surface

```bash
podman run --rm of --help
podman run --rm of --version
```

## Command Model

The project has two public entrypoints:

- Native Python CLI: `of`
- Native MCP server: `of-mcp`

The container image uses a launcher:

- `podman run --rm -i of`
  Starts MCP mode
- `podman run --rm -i of mcp`
  Starts MCP mode explicitly
- `podman run --rm of sync`
  Runs the CLI
- `podman run --rm of add "Task name"`
  Runs the CLI

Legacy container syntax like `podman run --rm of of sync` is intentionally rejected.

## CLI Usage

```text
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

## MCP Integration

### What the container does

The runtime image defaults to MCP mode when you start it without command arguments:

```bash
podman run --rm -i of
```

That is the correct shape for MCP hosts that expect a long-lived stdio process.

### Claude Desktop / Claude Code style configuration

Add an MCP server entry that launches the container in stdio mode and mounts a persistent cache directory:

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

If you prefer to keep credentials out of the URL, pass `OF_WEBDAV_USER`, `OF_WEBDAV_PASS`, and optionally `OF_ENCRYPTION_PASSPHRASE` as separate environment variables.

### Local MCP smoke test

Use this when you want to confirm the container launches cleanly in MCP mode before wiring it into a host:

```bash
podman run --rm -i \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://user:pass@dav.example.com/OmniFocus.ofocus/ \
  of
```

You should see the process stay attached on stdio rather than exiting immediately. That is the expected MCP server behavior.

### MCP operational notes

- Reuse the same mounted `.of-cache/` directory between requests for better performance
- Keep the MCP container long-lived when the host supports it
- Use `podman run --rm -i of mcp` only when you want to force explicit MCP mode
- Do not append `of` before CLI subcommands inside the container

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `OF_WEBDAV_URL` | Yes | WebDAV bundle URL. Credentials may be embedded as `https://user:pass@host/path/`. |
| `OF_WEBDAV_USER` | No | Explicit WebDAV username. Overrides URL-embedded credentials. |
| `OF_WEBDAV_PASS` | No | Explicit WebDAV password. Overrides URL-embedded credentials. |
| `OF_ENCRYPTION_PASSPHRASE` | No | Bundle decryption passphrase. Defaults to the WebDAV password. |
| `OF_CACHE_DIR` | No | Cache directory. Defaults to a repo-local `.of-cache/` when detectable, otherwise `/tmp/of-cache`. |

## Development

### Local Python workflow

```bash
python3.14 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
of --help
```

### Container workflow

```bash
podman build --target test -t omnifocus-cli-test .
podman run --rm omnifocus-cli-test
```

## Quality Bar

Every change is expected to satisfy the full gate:

```bash
ruff check src/ tests/
black --check src/ tests/
mypy src/
pytest --cov=src/omnifocus --cov-fail-under=100 -q
```

## CI/CD

The repository now includes GitHub Actions workflows under `.github/workflows/`:

- `ci.yml`
  Runs linting, formatting checks, `mypy`, full test coverage, and a container build on every push and pull request
- `release.yml`
  Builds wheel and sdist artifacts, publishes the runtime image to GHCR on version tags, and creates a GitHub Release with attached Python distributions
- `dependabot.yml`
  Keeps Python dependencies and GitHub Actions versions moving forward automatically

### Release flow

1. Bump the project version in `pyproject.toml` and `src/omnifocus/__init__.py`
2. Push a tag like `v1.0.1`
3. GitHub Actions builds:
   - Python sdist and wheel
   - runtime container image
   - GHCR image tags
   - GitHub Release assets

## Packaging

The project ships as:

- a Python package with console scripts:
  - `of`
  - `of-mcp`
- a container image optimized for:
  - CLI execution
  - MCP stdio hosting

The runtime image is based on `python:3.14-slim` and runs as a non-root user.

## Repository Layout

```text
src/omnifocus/          Core library, CLI, MCP server, sync and crypto modules
tests/                  Hermetic tests with 100% coverage enforcement
security_lab/           Defensive WebDAV harness for validation and experiments
Containerfile           Multi-stage container build for runtime and test images
.github/workflows/      CI and release automation
```

## Security Notes

- Never commit real credentials, passphrases, or bundle material
- Treat WebDAV responses as untrusted input
- Keep `.of-cache/` out of source control
- Use the isolated `security_lab/` harness only with test data and test accounts

## Author

Maciej Szymczak  
[maciej@szymczak.at](mailto:maciej@szymczak.at)
