# Containerfile for omnifocus-cli
#
# Build runtime image:
#   podman build --target runtime -t omnifocus-cli .
#
# Build test image and run tests:
#   podman build --target test -t omnifocus-cli-test .
#   podman run --rm omnifocus-cli-test
#
# Run CLI:
#   podman run --rm \
#     -e OF_WEBDAV_URL -e OF_WEBDAV_USER -e OF_WEBDAV_PASS \
#     -e OF_ENCRYPTION_PASSPHRASE \
#     omnifocus-cli of tasks --inbox
#
# MCP server (default ENTRYPOINT):
#   podman run --rm -i \
#     -e OF_WEBDAV_URL -e OF_WEBDAV_USER -e OF_WEBDAV_PASS \
#     -e OF_ENCRYPTION_PASSPHRASE \
#     omnifocus-cli

# ---------------------------------------------------------------------------
# Builder stage — install everything into /install
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install -e ".[mcp]"

# ---------------------------------------------------------------------------
# Runtime stage — minimal image, no build tools
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

# Security: run as non-root user
RUN useradd --uid 1001 --no-create-home --shell /bin/false appuser

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local
COPY src/ src/

# Install the editable package itself (needs the src directory)
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir --no-deps -e .

# Writable cache directory for the non-root user
RUN mkdir -p /tmp/of-cache && chown appuser /tmp/of-cache

USER appuser

ENV OF_CACHE_DIR=/tmp/of-cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default: MCP server mode (stdio transport for Claude integration)
ENTRYPOINT ["of-mcp"]

# CLI override example: podman run ... omnifocus-cli of tasks --inbox

# ---------------------------------------------------------------------------
# Test stage — includes dev dependencies and test suite
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS test

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY tests/ tests/

# Install package + dev dependencies
RUN pip install --no-cache-dir -e ".[dev]"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest", "--cov=src/omnifocus", "--cov-report=term-missing", "--cov-branch", "-v"]
