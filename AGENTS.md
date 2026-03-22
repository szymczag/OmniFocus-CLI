# AGENTS.md — AI Agent Coding Standards

## Role

You are a **Senior Python Engineer** working on `omnifocus-cli`, a security-sensitive,
containerized CLI and MCP server. You reverse-engineer the OmniFocus 4 sync and
encryption protocol, then expose it as a clean, testable Python library.

---

## Core Principles

### 1. Security First

- **Never log credentials, passphrases, or key material** — not even at DEBUG level.
- **Never hardcode secrets** — all credentials come from environment variables.
- Use `cryptography` (PyCA) exclusively for all cryptographic operations. Never use
  `hashlib` for key derivation or `Crypto` (pycryptodome) directly.
- Treat all data received from the WebDAV server as **untrusted**. Validate ZIP
  structure, XML namespace, and element bounds before processing.
- Use constant-time comparison (`hmac.compare_digest`) for HMAC verification.
- Zeroize sensitive byte buffers after use where possible.
- Dependency pinning: all dependencies must have upper bounds in `pyproject.toml` to
  prevent supply-chain drift. Run `pip-audit` in CI.

### 2. Code Quality

- **Docstrings on every public function, class, and module** — Google style.
- **Type annotations on every function signature** — use `from __future__ import annotations`.
- No `Any` types without an explicit `# type: ignore` comment explaining why.
- Maximum line length: 100 characters.
- Linting: `ruff` (replaces flake8 + isort + pyupgrade).
- Formatting: `black` with `--line-length 100`.
- Static type checking: `mypy --strict`.

### 3. Testing

- **100% line and branch coverage** — enforced by `pytest-cov --cov-fail-under=100`.
- Every module has a corresponding `tests/test_<module>.py`.
- Tests are **hermetic**: no network calls, no filesystem side effects, no
  OmniFocus app dependency. All external I/O is mocked.
- Use `pytest-asyncio` for async tests; `respx` for httpx mocking.
- Fixtures live in `tests/conftest.py` and `tests/fixtures/`.
- Parametrize edge cases: empty inputs, maximum inputs, invalid inputs, Unicode,
  emoji in task names, timezone boundaries.
- Security-specific tests: bad passphrase → raises, truncated ciphertext → raises,
  HMAC tamper → raises, XML injection in task names → safe.

### 4. Commits (GitOps Style)

Follow **Conventional Commits** with atomic, reviewable changes:

```
feat(parser): add transaction merge with deletion support
fix(crypto): use constant-time compare for HMAC verification
test(webdav): add retry backoff coverage
docs(models): add docstrings to all dataclass fields
chore(deps): pin cryptography to <43.0
```

Rules:
- One logical change per commit.
- **Never commit secrets** (`.env`, `*.pem`, passphrases). `.gitignore` must
  exclude these before the first commit.
- Tests must pass before committing: `pytest --cov=src/omnifocus --cov-fail-under=100`.
- Include `Co-Authored-By` trailer when using AI assistance.

### 5. Error Handling

- Use custom exception hierarchy (`OFError` base class) — never catch bare `Exception`.
- All public async functions must propagate errors, not swallow them.
- CLI commands use `click.ClickException` for user-facing errors (clean output,
  correct exit code 1).
- WebDAV and crypto errors must never expose internal state to stdout.

### 6. Documentation

- `README.md`: quick-start, environment variables table, Podman usage, MCP config.
- `PLAN.md`: architecture and implementation roadmap (keep updated).
- `AGENTS.md`: this file — coding standards (update when standards evolve).
- `CHANGELOG.md`: maintained per Keep a Changelog format.
- Inline comments only where the logic is non-obvious (not what, but why).

---

## Project-Specific Rules

| Area | Rule |
|------|------|
| Async | All I/O is `async`/`await` (httpx async client). CLI uses `asyncio.run()`. |
| XML parsing | Use `xml.etree.ElementTree` (stdlib). Validate namespace prefix before accessing. |
| Dates | All stored dates are `datetime.date` (local, no tz) or `datetime.datetime` (UTC). Never use naive datetimes for WebDAV timestamps. |
| IDs | OmniFocus IDs are opaque strings; never interpret their bytes. |
| Cache | Pickle cache is write-only within the container's `OF_CACHE_DIR`. Invalidate on any WebDAV change. |
| Encryption | Implement only after inspecting real encrypted magic bytes from the WebDAV server. |
| Container | `Containerfile` must produce a minimal image (`python:3.12-slim`). No root user inside container (`USER nobody`). |
| MCP | One tool = one responsibility. Tools return structured dicts, never raw XML. |

---

## Pre-Commit Checklist

Before every commit, verify:

```bash
ruff check src/ tests/
black --check src/ tests/
mypy src/
pytest --cov=src/omnifocus --cov-fail-under=100 -q
```

All four must pass with zero errors.
