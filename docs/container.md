# Container and Runtime Notes

`omnifocus-cli` is designed for headless operation.

You do not need:
- OmniFocus.app
- macOS
- AppleScript
- Omni Automation permissions

You do need:
- network access to the OmniFocus WebDAV bundle
- valid WebDAV credentials
- the encryption passphrase when the bundle is encrypted

## Build

```bash
podman build --target runtime -t of .
```

## Persistent Cache

Mount a writable cache directory for faster warm starts:

```bash
mkdir -p .of-cache

podman run --rm \
  -v "$PWD/.of-cache":/cache \
  -e OF_CACHE_DIR=/cache \
  -e OF_WEBDAV_URL=https://dav.example.com/OmniFocus.ofocus/ \
  -e OF_WEBDAV_USER=username \
  -e OF_WEBDAV_PASS=password \
  of sync
```

## CLI vs MCP

- `podman run --rm of sync`
  Runs a CLI command.
- `podman run --rm -i of`
  Starts the MCP server over stdio.
- `podman run --rm -i of mcp`
  Starts the MCP server explicitly.
- `podman run --rm of http`
  Starts the HTTPS API.

## HTTPS API

The HTTP transport is private-by-default:

- bind host defaults to `127.0.0.1`
- TLS is mandatory and enforced at TLS 1.3 minimum
- Bearer auth is mandatory
- the process refuses to start without:
  - `OF_HTTP_API_KEY`
  - `OF_HTTP_TLS_CERT_FILE`
  - `OF_HTTP_TLS_KEY_FILE`
- the authenticated OpenAPI spec lives at `/v1/openapi.json`

Example:

```bash
mkdir -p certs

podman run --rm \
  -p 127.0.0.1:8443:8443 \
  -v "$PWD/certs":/tls:ro \
  -e OF_HTTP_API_KEY=replace-me \
  -e OF_HTTP_TLS_CERT_FILE=/tls/cert.pem \
  -e OF_HTTP_TLS_KEY_FILE=/tls/key.pem \
  ghcr.io/szymczag/omnifocus-cli:latest http
```

For detailed API semantics, OpenAPI usage, and n8n examples, see [docs/http.md](http.md).

## Image Freshness

If you track `latest`, refresh the local image before debugging behavior:

```bash
podman pull ghcr.io/szymczag/omnifocus-cli:latest
```

or:

```bash
podman run --rm --pull=always ghcr.io/szymczag/omnifocus-cli:latest --version
```

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `OF_WEBDAV_URL` | Yes | WebDAV bundle URL |
| `OF_WEBDAV_USER` | No | Explicit WebDAV username |
| `OF_WEBDAV_PASS` | No | Explicit WebDAV password |
| `OF_WEBDAV_AUTH` | No | WebDAV authentication scheme: `basic` (default) or `digest` |
| `OF_ENCRYPTION_PASSPHRASE` | No | Bundle decryption passphrase |
| `OF_CACHE_DIR` | No | Writable cache directory |
| `OF_HTTP_HOST` | No | HTTPS API bind host (default `127.0.0.1`) |
| `OF_HTTP_PORT` | No | HTTPS API bind port (default `8443`) |
| `OF_HTTP_API_KEY` | HTTPS only | Required Bearer token for the HTTPS API |
| `OF_HTTP_TLS_CERT_FILE` | HTTPS only | TLS certificate PEM path |
| `OF_HTTP_TLS_KEY_FILE` | HTTPS only | TLS private key PEM path |
| `OF_HTTP_ALLOWED_HOSTS` | HTTPS only | Comma-separated trusted host allowlist |

## Operational Notes

- The runtime image is intended to stay non-root.
- Cache invalidation happens automatically after write operations.
- The CLI and MCP server both operate on the same underlying WebDAV sync and parse layer.
- The HTTPS API uses the same business logic as MCP, but over a REST JSON transport.
- The HTTPS API adds request size limits, security headers, trusted-host validation, and auth rate limiting.
- Do not expose the HTTPS API externally without reviewing your firewall, certificate posture, and allowed-host configuration.
