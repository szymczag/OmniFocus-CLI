# Containerfile for omnifocus-cli
# Author: Maciej Szymczak <maciej@szymczak.at>
#
# Build runtime image:
#   podman build --target runtime -t of .
#
# Build test image and run tests:
#   podman build --target test -t omnifocus-cli-test .
#   podman run --rm omnifocus-cli-test
#
# Run CLI:
#   podman run --rm \
#     -v "$PWD/.of-cache":/cache \
#     -e OF_CACHE_DIR=/cache \
#     -e OF_WEBDAV_URL -e OF_WEBDAV_USER -e OF_WEBDAV_PASS \
#     -e OF_ENCRYPTION_PASSPHRASE \
#     of tasks --inbox
#
# MCP server (default container command):
#   podman run --rm -i \
#     -v "$PWD/.of-cache":/cache \
#     -e OF_CACHE_DIR=/cache \
#     -e OF_WEBDAV_URL -e OF_WEBDAV_USER -e OF_WEBDAV_PASS \
#     -e OF_ENCRYPTION_PASSPHRASE \
#     of

# ---------------------------------------------------------------------------
# Builder stage — install everything into /install
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS builder

WORKDIR /build

COPY pyproject.toml README.md ./
COPY src/ src/

RUN pip install --no-cache-dir --prefix=/install -e .

# ---------------------------------------------------------------------------
# Runtime stage — minimal image, no build tools
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS runtime

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
RUN mkdir -p /app/.of-cache /tmp/of-cache && chown -R appuser /app/.of-cache /tmp/of-cache

USER appuser

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default launcher: no args -> MCP mode, CLI args -> of CLI.
# Using ENTRYPOINT so `podman run --rm of --help` passes `--help` to the
# launcher instead of trying to execute it as a binary inside the container.
ENTRYPOINT ["python", "-m", "omnifocus.launcher"]

# ---------------------------------------------------------------------------
# Test stage — includes dev dependencies and test suite
# ---------------------------------------------------------------------------
FROM python:3.14-slim AS test

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ src/
COPY tests/ tests/

# Install package + dev dependencies
RUN pip install --no-cache-dir -e ".[dev]"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

CMD ["pytest", "--cov=src/omnifocus", "--cov-report=term-missing", "--cov-branch", "-v"]
