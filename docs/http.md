# HTTPS API Reference

`omnifocus-cli` can run a private HTTPS JSON API for n8n and other automation clients.

This transport is separate from MCP. It is designed for machine-to-machine access and uses
authenticated OpenAPI as its canonical contract.

## Security Model

The HTTPS API is intentionally strict:

- TLS is mandatory and enforced at **TLS 1.3 minimum**
- Bearer authentication is mandatory on **every** endpoint
- the server refuses to start without:
  - `OF_HTTP_API_KEY`
  - `OF_HTTP_TLS_CERT_FILE`
  - `OF_HTTP_TLS_KEY_FILE`
- default bind address is `127.0.0.1`
- default port is `8443`
- no Swagger UI or Redoc is exposed in production mode
- the OpenAPI spec at `/v1/openapi.json` is also protected by Bearer auth
- request bodies are capped at 1 MiB
- invalid Bearer attempts are rate-limited in-memory
- trusted hosts are allowlisted

## Environment Variables

| Variable | Required | Description |
| --- | --- | --- |
| `OF_HTTP_HOST` | No | Bind host. Defaults to `127.0.0.1`. |
| `OF_HTTP_PORT` | No | Bind port. Defaults to `8443`. |
| `OF_HTTP_API_KEY` | Yes | Required Bearer token value. |
| `OF_HTTP_TLS_CERT_FILE` | Yes | Path to the TLS certificate PEM file. |
| `OF_HTTP_TLS_KEY_FILE` | Yes | Path to the TLS private key PEM file. |
| `OF_HTTP_ALLOWED_HOSTS` | No | Comma-separated trusted host allowlist. Defaults to `127.0.0.1,localhost`. |
| `OF_WEBDAV_URL` | Usually | Required for endpoints that need bundle access. |
| `OF_WEBDAV_USER` | No | Explicit WebDAV username override. |
| `OF_WEBDAV_PASS` | No | Explicit WebDAV password override. |
| `OF_WEBDAV_AUTH` | No | WebDAV authentication scheme: `basic` (default) or `digest`. |
| `OF_ENCRYPTION_PASSPHRASE` | No | Bundle decryption passphrase. |
| `OF_CACHE_DIR` | No | Writable cache directory. |

## Start the Server

### Native Python entrypoint

```bash
OF_HTTP_API_KEY=replace-me \
OF_HTTP_TLS_CERT_FILE=/absolute/path/cert.pem \
OF_HTTP_TLS_KEY_FILE=/absolute/path/key.pem \
of-http
```

### Container launcher mode

```bash
podman run --rm \
  -p 127.0.0.1:8443:8443 \
  -v "$PWD/certs":/tls:ro \
  -e OF_HTTP_API_KEY=replace-me \
  -e OF_HTTP_TLS_CERT_FILE=/tls/cert.pem \
  -e OF_HTTP_TLS_KEY_FILE=/tls/key.pem \
  ghcr.io/szymczag/omnifocus-cli:latest http
```

## OpenAPI

The canonical machine-readable schema lives at:

- `GET /v1/openapi.json`

It is protected by the same Bearer auth path as the rest of the API.
That OpenAPI document is the normative HTTP contract; this Markdown page is an operator guide
and implementation summary.

Example:

```bash
curl --silent --show-error --insecure \
  -H "Authorization: Bearer replace-me" \
  https://127.0.0.1:8443/v1/openapi.json
```

## n8n Usage

Use the standard HTTP Request node:

- Method: `GET`, `POST`, or `PATCH`
- URL: `https://127.0.0.1:8443/v1/...`
- Authentication: Header auth
- Header: `Authorization: Bearer <your-token>`
- TLS: trust your certificate or disable verification only in a controlled local setup

Minimal example:

```bash
curl --silent --show-error --insecure \
  -H "Authorization: Bearer replace-me" \
  https://127.0.0.1:8443/v1/health
```

## Response Envelope

Success:

```json
{
  "ok": true,
  "data": {}
}
```

Error:

```json
{
  "ok": false,
  "error": {
    "code": "validation_error",
    "message": "..."
  }
}
```

Status code policy:

- `200` success
- `201` created
- `400` malformed request
- `401` missing or invalid bearer token
- `404` resource not found
- `409` semantic conflict
- `422` validation failure
- `429` too many authentication failures
- `500` internal unexpected failure
- `504` request timeout

## Security Headers

All success and error responses carry the same security baseline:

- `Strict-Transport-Security`
- `X-Content-Type-Options: nosniff`
- `Cache-Control: no-store`
- `Pragma: no-cache`

## ASVS Coverage Notes

The companion mapping in `docs/security/asvs-5.0.md` is a traceability matrix, not a
certification statement.

It is intended to show:

- which ASVS 5.0 controls are implemented directly in the HTTPS API
- which controls are only partially implemented
- which controls remain open or intentionally out of scope for this transport

It is not intended to guarantee full ASVS conformance for the repository or the deployment
environment around it.

## Request Logging

The server emits structured JSON logs aligned to the OpenTelemetry log data model:

- request lifecycle logs with method, route, status, duration, client IP, request id, and trace id
- security event logs for auth failures, invalid hosts, startup misconfiguration, and timeouts
- no `Authorization` values, WebDAV credentials, passphrases, or request bodies in normal logs

`traceparent` is accepted and propagated into the structured log fields when present.

## Endpoint Catalog

All endpoints live under `/v1`.

### Health and sync

- `GET /v1/health`
- `POST /v1/sync`

### Tasks

- `GET /v1/tasks`
- `GET /v1/tasks/search`
- `GET /v1/tasks/{task_id}`
- `POST /v1/tasks`
- `PATCH /v1/tasks/{task_id}`
- `POST /v1/tasks/{task_id}/complete`
- `POST /v1/tasks/{task_id}/drop`

Task filters:

- `inbox`
- `today`
- `flagged`
- `due`
- `project`
- `tag`
- `tag_id`
- `limit`

### Projects

- `GET /v1/projects`
- `GET /v1/projects/{project_id}`
- `POST /v1/projects`
- `PATCH /v1/projects/{project_id}`
- `POST /v1/projects/{project_id}/complete`

Project filters:

- `status`
- `tag`
- `tag_id`
- `limit`

### Project review

- `GET /v1/projects/review`
- `POST /v1/projects/{project_id}/review`

Review filters:

- `due_only`
- `limit`

### Folders

- `GET /v1/folders`
- `GET /v1/folders/tree`
- `GET /v1/folders/{folder_id}`
- `POST /v1/folders`
- `PATCH /v1/folders/{folder_id}`
- `POST /v1/folders/{folder_id}/drop`

### Tags

- `GET /v1/tags`
- `GET /v1/tags/{tag_id}`
- `POST /v1/tags`
- `PATCH /v1/tags/{tag_id}`
- `POST /v1/tags/{tag_id}/drop`

## Write Semantics

- reads may filter by human-readable names where the API supports that
- all write endpoints mutate by stable OmniFocus IDs in the path
- task and project tag assignment uses explicit `tag_ids`
- folder and tag moves validate parent existence, self-parenting, and cycles
- request bodies use Pydantic schema validation before reaching the service layer

## Operational Notes

- keep the API bound to localhost unless you have a reviewed network exposure plan
- prefer mounted PEM files over in-image cert material
- do not terminate TLS in plaintext in front of the app in this deployment shape
- do not expose the API externally without reviewing firewall rules, certificate posture, and host allowlists
- the default per-request timeout is 30 seconds
