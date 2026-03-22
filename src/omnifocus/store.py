"""OFocusStore — orchestrates sync, decryption, parsing, and caching.

This module is the main integration point between the WebDAV client, the
encryption layer, and the XML parser.  It produces an :class:`~omnifocus.models.OFModel`
which is then consumed by the CLI commands and the MCP server.

Cache strategy
--------------
The parsed model is serialised with :mod:`pickle` into ``OF_CACHE_DIR``.
The cache is invalidated whenever any ZIP file in the bundle is newer than
the cached file.  This avoids a 100-200 ms re-parse on every CLI invocation
while still reflecting changes made by OmniFocus on the sync server.

Security
--------
The cache stores fully-decrypted model data.  ``OF_CACHE_DIR`` should be
inside the container's ephemeral filesystem (default ``/tmp/of-cache``)
and **not** mounted to persistent host storage unless the host is trusted.

Usage::

    import asyncio
    from omnifocus.store import OFocusStore

    async def main() -> None:
        async with OFocusStore.from_env() as store:
            model = await store.load()
            print(len(model.active_tasks))

    asyncio.run(main())
"""

from __future__ import annotations

import logging
import os
import pickle
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

from omnifocus.crypto.discovery import is_encrypted
from omnifocus.crypto.encryption import decrypt
from omnifocus.errors import OFWebDAVError
from omnifocus.models import OFModel
from omnifocus.parser import build_model
from omnifocus.sync.protocol import classify_bundle_files
from omnifocus.sync.webdav import WebDAVClient

_CACHE_FILENAME = "of_model.pkl"


class OFocusStore:
    """High-level store that syncs, decrypts, parses, and caches the OFModel.

    Args:
        client: A :class:`~omnifocus.sync.webdav.WebDAVClient` instance.
        passphrase: Decryption passphrase, or ``None`` for unencrypted bundles.
        cache_dir: Directory for the pickle cache.  Defaults to
            ``$OF_CACHE_DIR`` or ``/tmp/of-cache``.
    """

    def __init__(
        self,
        client: WebDAVClient,
        passphrase: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._passphrase = passphrase
        self._cache_dir = cache_dir or Path(
            os.environ.get("OF_CACHE_DIR", "/tmp/of-cache")
        )
        self._cache_path = self._cache_dir / _CACHE_FILENAME
        # Tracks the mtime of the newest ZIP seen during the last successful load
        self._last_bundle_mtime: float = 0.0

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    async def __aenter__(self) -> OFocusStore:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls) -> OFocusStore:
        """Construct a store from environment variables.

        Required:
            ``OF_WEBDAV_URL``, ``OF_WEBDAV_USER``, ``OF_WEBDAV_PASS``

        Optional:
            ``OF_ENCRYPTION_PASSPHRASE`` — decryption passphrase.  When not
            set, the WebDAV password is tried automatically (OmniFocus
            "linked password" mode uses the same credential for both).
            ``OF_CACHE_DIR`` — cache directory (default ``/tmp/of-cache``).

        Raises:
            OFWebDAVError: If required WebDAV env vars are missing.
        """
        client = WebDAVClient.from_env()
        passphrase = os.environ.get("OF_ENCRYPTION_PASSPHRASE") or _webdav_password_from_env()
        return cls(client=client, passphrase=passphrase or None)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def load(self, *, force_refresh: bool = False) -> OFModel:
        """Return the current :class:`OFModel`, using cache if available.

        Downloads and parses the bundle when the cache is stale or missing.

        Args:
            force_refresh: If ``True``, bypass the cache and re-sync.

        Returns:
            A fully-populated :class:`OFModel`.
        """
        if not force_refresh:
            cached = self._load_from_cache()
            if cached is not None:
                return cached

        return await self._sync_and_build()

    async def sync_status(self) -> dict[str, Any]:
        """Return metadata about the last sync.

        Returns:
            A dict with ``last_synced`` (ISO datetime string or ``null``),
            ``cached`` (bool), and ``cache_age_seconds`` (float or ``null``).
        """
        last_synced: str | None = None
        cache_age: float | None = None
        cached = False

        if self._cache_path.exists():
            cached = True
            age = time.time() - self._cache_path.stat().st_mtime
            cache_age = round(age, 1)
            ts = datetime.fromtimestamp(
                self._cache_path.stat().st_mtime, tz=timezone.utc
            )
            last_synced = ts.isoformat()

        return {
            "last_synced": last_synced,
            "cached": cached,
            "cache_age_seconds": cache_age,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _sync_and_build(self) -> OFModel:
        """Download the bundle, decrypt if needed, parse, cache, and return."""
        log.debug("Listing bundle files from WebDAV…")
        filenames = await self._client.list_bundle()
        baseline_name, tx_names = classify_bundle_files(filenames)
        log.debug("Bundle: baseline=%s  transactions=%d", baseline_name, len(tx_names))

        log.debug("Downloading baseline %s", baseline_name)
        baseline_raw = await self._client.get_file(baseline_name)
        baseline_bytes = self._maybe_decrypt(baseline_raw)

        tx_bytes_list: list[bytes] = []
        for name in tx_names:
            log.debug("Downloading transaction %s", name)
            raw = await self._client.get_file(name)
            tx_bytes_list.append(self._maybe_decrypt(raw))

        log.debug("Parsing model from %d file(s)…", 1 + len(tx_bytes_list))
        model = build_model(baseline_bytes, tx_bytes_list)
        log.debug(
            "Parsed: %d tasks, %d projects, %d folders",
            len(model.tasks), len(model.projects), len(model.folders),
        )
        self._save_to_cache(model)
        return model

    def _maybe_decrypt(self, data: bytes) -> bytes:
        """Decrypt *data* if it looks encrypted and a passphrase is configured."""
        if is_encrypted(data):
            if self._passphrase is None:
                from omnifocus.errors import OFEncryptionError
                raise OFEncryptionError(
                    "Bundle is encrypted but no passphrase is available. "
                    "Set OF_ENCRYPTION_PASSPHRASE (or use the WebDAV password "
                    "as a linked passphrase via OF_WEBDAV_PASS / URL-embedded credentials)."
                )
            log.debug("Decrypting %d-byte file", len(data))
            return decrypt(data, self._passphrase)
        return data

    def _load_from_cache(self) -> OFModel | None:
        """Return the cached model if it exists and is fresh, else ``None``."""
        if not self._cache_path.exists():
            log.debug("Cache miss (file not found)")
            return None
        try:
            data = self._cache_path.read_bytes()
            model = pickle.loads(data)  # noqa: S301 — trusted internal cache
            log.debug("Cache hit: %s", self._cache_path)
            return model
        except Exception:  # pragma: no cover — corrupted cache is skipped
            return None

    def _save_to_cache(self, model: OFModel) -> None:
        """Persist *model* to the pickle cache."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(pickle.dumps(model))
        log.debug("Model cached to %s", self._cache_path)

    def invalidate_cache(self) -> None:
        """Remove the on-disk cache, forcing a full re-sync on next :meth:`load`."""
        if self._cache_path.exists():
            self._cache_path.unlink()


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _webdav_password_from_env() -> str:
    """Return the effective WebDAV password from env vars or URL-embedded credentials.

    Mirrors the credential-resolution logic in
    :meth:`~omnifocus.sync.webdav.WebDAVClient.from_env` so that
    :class:`OFocusStore` can use the same password as the encryption
    passphrase when ``OF_ENCRYPTION_PASSPHRASE`` is not explicitly set
    (OmniFocus *linked password* mode).
    """
    explicit = os.environ.get("OF_WEBDAV_PASS", "")
    if explicit:
        return explicit
    raw_url = os.environ.get("OF_WEBDAV_URL", "")
    return urlsplit(raw_url).password or ""
