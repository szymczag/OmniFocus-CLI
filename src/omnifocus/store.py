"""OFocusStore — orchestrates sync, decryption, parsing, and caching.

This module is the main integration point between the WebDAV client, the
encryption layer, and the XML parser.  It produces an :class:`~omnifocus.models.OFModel`
which is then consumed by the CLI commands and the MCP server.

Encryption
----------
When a bundle is encrypted the ``.ofocus`` directory contains an ``encrypted``
plist file that stores the PBKDF2 parameters and the AES-128-wrapped document
key slots.  :class:`OFocusStore` detects this automatically:

1. Downloads the baseline ZIP.
2. If the baseline starts with the ``OmniFileEncryption`` magic, downloads
   the ``encrypted`` plist and derives AES + HMAC keys from the passphrase.
3. Decrypts every ZIP in the bundle using those keys before parsing.

OmniFocus *linked-password* mode means the same credential is used for both
WebDAV authentication and bundle decryption.  When ``OF_ENCRYPTION_PASSPHRASE``
is not set, the WebDAV password is used automatically.

Cache strategy
--------------
The parsed model is serialised with :mod:`pickle` into ``OF_CACHE_DIR``.
The cache is reused only when the current remote bundle listing matches the
cached bundle fingerprint exactly. This avoids a 100-200 ms re-parse on every
CLI invocation while still reflecting changes made by OmniFocus on the sync
server.

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

import dataclasses
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
from omnifocus.errors import OFEncryptionError
from omnifocus.models import OFModel
from omnifocus.parser import build_model
from omnifocus.sync.protocol import classify_bundle_files
from omnifocus.sync.webdav import WebDAVClient

_CACHE_FILENAME = "of_model.pkl"
BundleFingerprint = tuple[str, tuple[str, ...]]
DocumentKeys = dict[int, tuple[bytes, bytes]]


@dataclasses.dataclass(frozen=True)
class _CachePayload:
    """Serializable cache payload for a parsed model and bundle fingerprint."""

    model: OFModel
    bundle_fingerprint: BundleFingerprint | None


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
        filenames = await self._client.list_bundle()
        bundle_fingerprint = _bundle_fingerprint(filenames)

        if not force_refresh:
            cached = self._load_from_cache()
            if (
                cached is not None
                and cached.bundle_fingerprint is not None
                and cached.bundle_fingerprint == bundle_fingerprint
            ):
                log.debug("Cache hit for bundle fingerprint %s", bundle_fingerprint)
                return cached.model

        return await self._sync_and_build(
            filenames=filenames,
            bundle_fingerprint=bundle_fingerprint,
        )

    async def sync_status(self) -> dict[str, Any]:
        """Return metadata about the last sync.

        Returns:
            A dict with ``last_synced`` (ISO datetime string or ``null``),
            ``cached`` (bool), ``cache_age_seconds`` (float or ``null``), and
            ``cache_valid`` (bool).
        """
        last_synced: str | None = None
        cache_age: float | None = None
        cached = False
        cache_valid = False

        if self._cache_path.exists():
            cached = True
            age = time.time() - self._cache_path.stat().st_mtime
            cache_age = round(age, 1)
            ts = datetime.fromtimestamp(
                self._cache_path.stat().st_mtime, tz=timezone.utc
            )
            last_synced = ts.isoformat()
            cached_payload = self._load_from_cache()
            if (
                cached_payload is not None
                and cached_payload.bundle_fingerprint is not None
            ):
                filenames = await self._client.list_bundle()
                cache_valid = (
                    cached_payload.bundle_fingerprint == _bundle_fingerprint(filenames)
                )

        return {
            "last_synced": last_synced,
            "cached": cached,
            "cache_age_seconds": cache_age,
            "cache_valid": cache_valid,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _sync_and_build(
        self,
        *,
        filenames: list[str],
        bundle_fingerprint: BundleFingerprint,
    ) -> OFModel:
        """Download the bundle, decrypt if needed, parse, cache, and return."""
        baseline_name, tx_names = classify_bundle_files(filenames)
        log.debug("Bundle: baseline=%s  transactions=%d", baseline_name, len(tx_names))

        log.debug("Downloading baseline %s", baseline_name)
        baseline_raw = await self._client.get_file(baseline_name)
        log.debug(
            "Baseline header (hex): %s  ascii: %r",
            baseline_raw[:32].hex(),
            baseline_raw[:32],
        )

        # Detect encryption by inspecting the baseline magic bytes
        doc_keys: DocumentKeys | None = None
        if is_encrypted(baseline_raw):
            log.debug("Encrypted bundle detected — fetching document keys")
            doc_keys = await self._load_document_keys()

        baseline_bytes = self._maybe_decrypt(baseline_raw, doc_keys)

        tx_bytes_list: list[bytes] = []
        for name in tx_names:
            log.debug("Downloading transaction %s", name)
            raw = await self._client.get_file(name)
            tx_bytes_list.append(self._maybe_decrypt(raw, doc_keys))

        log.debug("Parsing model from %d file(s)…", 1 + len(tx_bytes_list))
        model = build_model(baseline_bytes, tx_bytes_list)
        log.debug(
            "Parsed: %d tasks, %d projects, %d folders",
            len(model.tasks), len(model.projects), len(model.folders),
        )
        self._save_to_cache(
            _CachePayload(model=model, bundle_fingerprint=bundle_fingerprint)
        )
        return model

    async def _load_document_keys(self) -> DocumentKeys:
        """Download the ``encrypted`` plist and derive document key slots.

        Returns:
            Mapping ``{slot_id: (aes_key, hmac_key)}``.

        Raises:
            OFEncryptionError: If no passphrase is configured.
            OFWebDAVError: On non-404 WebDAV errors.
        """
        if self._passphrase is None:
            raise OFEncryptionError(
                "Bundle is encrypted but no passphrase is available. "
                "Set OF_ENCRYPTION_PASSPHRASE (or use the WebDAV password "
                "as a linked passphrase via OF_WEBDAV_PASS / URL-embedded credentials)."
            )

        log.debug("Downloading 'encrypted' plist")
        plist_bytes = await self._client.get_file("encrypted")

        from omnifocus.crypto.encryption import load_document_keys
        keys = load_document_keys(self._passphrase, plist_bytes)
        log.debug("Loaded %d key slot(s) from 'encrypted' plist", len(keys))
        return keys

    def _maybe_decrypt(self, data: bytes, doc_keys: DocumentKeys | None) -> bytes:
        """Decrypt *data* using *doc_keys*, or return it unchanged if unencrypted."""
        if doc_keys is None:
            log.debug("File does not match known encryption magic — treating as plaintext")
            return data

        if not is_encrypted(data):
            log.debug("File in encrypted bundle is not encrypted — treating as plaintext")
            return data

        from omnifocus.crypto.discovery import parse_file_header
        from omnifocus.crypto.encryption import decrypt_file

        key_id, _ = parse_file_header(data)
        if key_id not in doc_keys:
            raise OFEncryptionError(
                f"Key slot {key_id} not found in document keys "
                f"(available slots: {sorted(doc_keys.keys())})"
            )

        log.debug("Decrypting %d-byte file using key slot %d", len(data), key_id)
        return decrypt_file(data, *doc_keys[key_id])

    def _load_from_cache(self) -> _CachePayload | None:
        """Return the cached payload if it can be decoded, else ``None``."""
        if not self._cache_path.exists():
            log.debug("Cache miss (file not found)")
            return None
        try:
            data = self._cache_path.read_bytes()
            cached = pickle.loads(data)  # noqa: S301 — trusted internal cache
            log.debug("Cache hit: %s", self._cache_path)
            if isinstance(cached, _CachePayload):
                return cached
            if isinstance(cached, OFModel):
                return _CachePayload(model=cached, bundle_fingerprint=None)
            return None
        except Exception:  # pragma: no cover — corrupted cache is skipped
            return None

    def _save_to_cache(self, payload: _CachePayload) -> None:
        """Persist *payload* to the pickle cache."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_bytes(pickle.dumps(payload))
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


def _bundle_fingerprint(filenames: list[str]) -> BundleFingerprint:
    """Return a deterministic fingerprint for the current remote bundle listing."""
    baseline_name, tx_names = classify_bundle_files(filenames)
    return baseline_name, tuple(tx_names)
