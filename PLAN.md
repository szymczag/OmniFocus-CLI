# omnifocus-cli: Containerized Independent CLI + MCP Server

## Context

Build a fully independent Python CLI for OmniFocus 4 that:
- Runs in a **Podman container** (no macOS/AppleScript dependencies)
- Syncs directly from a **custom WebDAV server** using the OmniFocus sync protocol
- Decrypts **AES-256 passphrase-encrypted** `.ofocus` bundles
- Exposes all functionality as an **MCP server** for Claude
- Achieves **100% test coverage**

Credentials passed via environment variables; no host filesystem access required.

---

## Data Format (confirmed from local exploration)

- `.ofocus` bundle = directory of ZIP files on WebDAV
- Baseline ZIP: `00000000000000=<id>.zip` → `contents.xml` (3.5MB XML, ~7,400 tasks)
- Transaction ZIPs: `<ISO8601timestamp>=<clientID>+<parentID>.zip` (incremental changes)
- XML namespace: `http://www.omnigroup.com/namespace/OmniFocus/v2`
- Local copy is decrypted by OmniFocus; **WebDAV copy is encrypted**
- **Encryption discovery step required**: connect to WebDAV, inspect magic bytes of encrypted files to confirm OF encryption header format before implementing crypto

---

## Architecture

```
omnifocus-cli/
├── Containerfile                   # Podman image (python:3.12-slim)
├── pyproject.toml                  # click, rich, httpx, cryptography, mcp
├── src/omnifocus/
│   ├── models.py                   # Task, Project, Folder, Tag dataclasses
│   ├── parser.py                   # XML parse + transaction merge
│   ├── writer.py                   # Create transaction ZIPs (write path)
│   ├── store.py                    # OFocusStore: sync → decrypt → parse → cache
│   ├── fuzzy.py                    # difflib-based task name matching
│   ├── formatting.py               # rich table/tree + JSON renderers
│   ├── cli.py                      # Click CLI entry point
│   ├── mcp_server.py               # MCP server (stdio transport)
│   ├── sync/
│   │   ├── webdav.py               # httpx WebDAV client (PROPFIND/GET/PUT)
│   │   └── protocol.py             # bundle discovery, transaction ordering
│   └── crypto/
│       ├── encryption.py           # OF decrypt/encrypt (PBKDF2 + AES-256)
│       └── discovery.py            # detect encryption format from magic bytes
└── tests/
    ├── conftest.py                  # shared fixtures (model, xml, mock webdav)
    ├── fixtures/
    │   ├── sample.xml               # synthetic <omnifocus> XML
    │   ├── sample_encrypted.bin     # synthetic encrypted file for crypto tests
    │   └── webdav_mock/             # canned PROPFIND/GET responses
    ├── test_models.py
    ├── test_parser.py
    ├── test_writer.py
    ├── test_store.py
    ├── test_fuzzy.py
    ├── test_formatting.py
    ├── test_cli.py                  # click.testing.CliRunner
    ├── test_mcp_server.py
    ├── test_webdav.py               # httpx mock transport
    ├── test_protocol.py
    └── test_encryption.py
```

---

## Key Dependencies

```toml
[project]
dependencies = [
    "click>=8.1",
    "rich>=13.0",
    "httpx>=0.27",           # async WebDAV client
    "cryptography>=42.0",    # AES-256, PBKDF2
    "mcp>=1.0",              # Anthropic MCP Python SDK
]
[project.optional-dependencies]
dev = ["pytest>=8", "pytest-cov", "pytest-asyncio", "pytest-httpx", "respx"]

[project.scripts]
of = "omnifocus.cli:cli"
of-mcp = "omnifocus.mcp_server:main"
```

---

## Environment Variables

```
OF_WEBDAV_URL=https://webdav.example.com/omnifocus/
OF_WEBDAV_USER=username
OF_WEBDAV_PASS=password
OF_ENCRYPTION_PASSPHRASE=my-passphrase   # if encrypted
OF_CACHE_DIR=/cache                      # recommended mounted cache directory
```

---

## Module Designs

### `sync/webdav.py`
- `WebDAVClient(url, user, password)` using `httpx.AsyncClient`
- `list_bundle()` → PROPFIND to enumerate ZIPs in the `.ofocus` path
- `get_file(path)` → GET → `bytes`
- `put_file(path, data)` → PUT for uploading new transactions
- Retry with exponential backoff (3 attempts) for transient errors

### `crypto/discovery.py` + `crypto/encryption.py`
- **Discovery first**: `detect_format(raw_bytes) -> EncryptionFormat` — inspect magic bytes of the first encrypted file on WebDAV to determine the exact format (OF has used multiple schemes across versions)
- `decrypt(data: bytes, passphrase: str) -> bytes` → returns raw ZIP bytes
- `encrypt(data: bytes, passphrase: str) -> bytes` → for write path
- Key derivation: PBKDF2-HMAC-SHA256 (confirmed from OF security model)
- Known header: magic identifier + version + salt (16 bytes) + HMAC + IV + ciphertext
- Test with synthetic round-trip: `decrypt(encrypt(data, p), p) == data`

### `parser.py`
- `load_xml_from_zip(zip_bytes: bytes) -> ET.Element`
- `merge_elements(base: dict, tx_root: ET.Element, ns: str) -> None`
  - Upsert by ID if `<name>` child present; delete if absent
- `build_model(baseline_bytes, tx_list: list[bytes]) -> OFModel`
- Project detection: `<project>` element with children = Project; empty `<project/>` = Task
- Parent chain resolution for `task.project_id` (memoized traversal)

### `writer.py`
- `create_transaction(changes: list[ET.Element], client_id: str) -> tuple[str, bytes]`
  - Returns `(filename, zip_bytes)` — filename = `<UTC_ISO8601>=<client_id>+<parent_id>.zip`
  - Wraps changes in `<omnifocus>` root with correct namespace and version attributes
- Used for: add task, complete task, update task
- `generate_id() -> str` — base64url random 8-byte ID matching OF format

### `store.py` — `OFocusStore`
```python
async def sync_and_load() -> OFModel:
    files = await webdav.list_bundle()
    baseline_raw = await webdav.get_file(baseline_path)
    if encrypted: baseline_raw = decrypt(baseline_raw, passphrase)
    tx_raws = [decrypt(await webdav.get_file(p), passphrase) for p in tx_paths]
    model = build_model(baseline_raw, tx_raws)
    cache_to_disk(model)
    return model

async def write_transaction(changes) -> None:
    fname, data = create_transaction(changes, CLIENT_ID)
    if encrypted: data = encrypt(data, passphrase)
    await webdav.put_file(fname, data)
    invalidate_cache()
```

### CLI commands (`cli.py`)

```
of sync                                    # pull latest from WebDAV
of tasks [--inbox] [--today] [--flagged] [--due] [--project NAME] [--format table|json]
of add NAME [--project NAME] [--due DATE] [--flagged] [--note TEXT]
of done QUERY [-y]                         # fuzzy match → confirm → write transaction
of projects [--status active|all] [--format tree|json]
```

### MCP Server (`mcp_server.py`)

Tools exposed via `mcp` SDK (stdio transport for Podman):

| Tool | Description |
|------|-------------|
| `list_tasks` | Filter by inbox/today/flagged/project/due, returns JSON |
| `search_tasks` | Fuzzy search by name |
| `get_task` | Single task by ID |
| `add_task` | Create task, returns new ID |
| `complete_task` | Mark done by ID or fuzzy name |
| `update_task` | Change due/flagged/name/note |
| `list_projects` | All projects with status |
| `list_folders` | Folder hierarchy |
| `sync_now` | Trigger WebDAV sync, returns summary |
| `sync_status` | Last sync time, pending count |

---

## Containerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml .
COPY src/ src/
RUN pip install -e ".[mcp]"
mkdir -p .of-cache

podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  ...
ENTRYPOINT ["of-mcp"]   # default: MCP server mode
# override with: podman run ... of tasks --inbox
```

Claude MCP config (`~/.claude/settings.json`):
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

---

## 100% Test Coverage Strategy

- **`test_parser.py`**: synthetic XML fixture covering all element types, deletion markers, transaction merging, parent-chain resolution
- **`test_encryption.py`**: round-trip test (encrypt→decrypt), bad passphrase raises, wrong magic bytes raises, format discovery from magic bytes
- **`test_webdav.py`**: mock httpx transport via `respx`; test PROPFIND parse, GET, PUT, retry on 503
- **`test_writer.py`**: transaction ZIP structure, filename format, ID generation, XML round-trip
- **`test_cli.py`**: all commands via `click.testing.CliRunner`, missing env vars, no-match error, ambiguous match flow
- **`test_mcp_server.py`**: each tool called with mock OFocusStore, assert JSON output schema
- **`pytest-cov` target**: `--cov=src/omnifocus --cov-fail-under=100`

---

## Implementation Order

1. `models.py` + `parser.py` + `test_parser.py` — core data layer, verify against local XML
2. `sync/webdav.py` + `test_webdav.py` — connect to WebDAV, list bundle files
3. `crypto/discovery.py` — inspect real encrypted files, confirm format
4. `crypto/encryption.py` + `test_encryption.py` — implement decrypt using discovered format
5. `store.py` — wire sync + decrypt + parse + cache
6. `writer.py` + `test_writer.py` — transaction creation
7. `cli.py` + `test_cli.py` — all 5 commands
8. `mcp_server.py` + `test_mcp_server.py` — MCP tools
9. `Containerfile` — build and test in Podman

---

## Verification (end-to-end)

```bash
# Run all tests with coverage
pytest --cov=src/omnifocus --cov-fail-under=100 -v

# Build container
podman build -t omnifocus-cli .

# Test sync (reads WebDAV, decrypts, parses)
podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL -e OF_WEBDAV_USER -e OF_WEBDAV_PASS \
  -e OF_ENCRYPTION_PASSPHRASE \
  omnifocus-cli of tasks --inbox

# Test MCP via Claude
# Add to ~/.claude/settings.json, then: /mcp
```

---

## Open Research Item

**OmniFocus encryption format (Phase 3 — discovery)**: The exact byte layout of the encrypted files on the WebDAV server must be confirmed by inspecting the first few bytes (magic number, version) of a real encrypted transaction file. The implementation will follow the structure discovered there. The crypto whitepaper for OmniFocus 2 documents PBKDF2-SHA1 with AES-128; OF4 likely upgraded to PBKDF2-SHA256 with AES-256. This discovery happens at the start of step 3 above.
