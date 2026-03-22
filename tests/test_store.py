"""Tests for :mod:`omnifocus.store`."""

from __future__ import annotations

import pickle
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from omnifocus.errors import OFEncryptionError, OFWebDAVError
from omnifocus.models import OFModel
from omnifocus.store import OFocusStore
from omnifocus.sync.webdav import WebDAVClient
from tests.conftest import make_zip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_store(
    tmp_path: Path,
    filenames: list[str] | None = None,
    baseline_bytes: bytes | None = None,
    passphrase: str | None = None,
) -> tuple[OFocusStore, AsyncMock]:
    """Build an OFocusStore with a mocked WebDAVClient."""
    client = AsyncMock(spec=WebDAVClient)
    client.list_bundle = AsyncMock(
        return_value=filenames or ["00000000000000=base.zip"]
    )
    client.get_file = AsyncMock(return_value=baseline_bytes or make_zip(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2"/>'
    ))
    store = OFocusStore(client=client, passphrase=passphrase, cache_dir=tmp_path)
    return store, client


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_missing_webdav_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("OF_WEBDAV_URL", "OF_WEBDAV_USER", "OF_WEBDAV_PASS"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(OFWebDAVError):
            OFocusStore.from_env()

    def test_passphrase_falls_back_to_webdav_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When OF_ENCRYPTION_PASSPHRASE is absent, the WebDAV password is used."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "linked_pass")
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "linked_pass"

    def test_passphrase_falls_back_to_url_embedded_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credentials in URL, no separate vars → passphrase taken from URL."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://u:url_pass@dav.example.com/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "url_pass"

    def test_passphrase_set_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit OF_ENCRYPTION_PASSPHRASE overrides the WebDAV password fallback."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        monkeypatch.setenv("OF_ENCRYPTION_PASSPHRASE", "secret")
        store = OFocusStore.from_env()
        assert store._passphrase == "secret"


# ---------------------------------------------------------------------------
# load (no cache)
# ---------------------------------------------------------------------------


class TestLoad:
    @pytest.mark.asyncio
    async def test_load_returns_model(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_load_calls_list_and_get(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        client.list_bundle.assert_called_once()
        client.get_file.assert_called_once_with("00000000000000=base.zip")

    @pytest.mark.asyncio
    async def test_load_with_transactions(self, tmp_path: Path) -> None:
        empty_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2"/>'
        )
        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base.zip", "20260322T154011=tx.zip"],
            baseline_bytes=make_zip(empty_xml),
        )
        # Second call returns a transaction ZIP
        client.get_file = AsyncMock(return_value=make_zip(empty_xml))
        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.get_file.call_count == 2

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        # Populate cache first
        await store.load()
        assert client.list_bundle.call_count == 1
        # Force refresh must call WebDAV again
        await store.load(force_refresh=True)
        assert client.list_bundle.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_cache_on_second_load(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        await store.load()
        # WebDAV should only be called once
        assert client.list_bundle.call_count == 1


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------


class TestCache:
    @pytest.mark.asyncio
    async def test_cache_file_created(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        await store.load()
        assert (tmp_path / "of_model.pkl").exists()

    @pytest.mark.asyncio
    async def test_invalidate_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        store.invalidate_cache()
        assert not (tmp_path / "of_model.pkl").exists()
        # Next load must re-sync
        await store.load()
        assert client.list_bundle.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_cache_noop_when_no_cache(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        store.invalidate_cache()  # must not raise

    @pytest.mark.asyncio
    async def test_cache_contains_valid_pickle(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        model = await store.load()
        cached = pickle.loads((tmp_path / "of_model.pkl").read_bytes())
        assert isinstance(cached, OFModel)
        assert cached.parsed_at == model.parsed_at


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestEncryption:
    @pytest.mark.asyncio
    async def test_encrypted_data_decrypted_before_parse(
        self, tmp_path: Path
    ) -> None:
        """Encrypted baseline must be decrypted before passing to the parser."""
        from omnifocus.crypto.encryption import encrypt

        plaintext = make_zip(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2"/>'
        )
        encrypted = encrypt(plaintext, "passphrase123")

        store, client = _make_store(
            tmp_path, baseline_bytes=encrypted, passphrase="passphrase123"
        )
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_encrypted_without_passphrase_raises(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.crypto.encryption import encrypt

        plaintext = make_zip(
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2"/>'
        )
        encrypted = encrypt(plaintext, "passphrase123")
        store, _ = _make_store(tmp_path, baseline_bytes=encrypted, passphrase=None)
        with pytest.raises(OFEncryptionError, match="no passphrase is available"):
            await store.load()

    @pytest.mark.asyncio
    async def test_unencrypted_data_passes_through(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path, passphrase=None)
        model = await store.load()
        assert isinstance(model, OFModel)


# ---------------------------------------------------------------------------
# sync_status
# ---------------------------------------------------------------------------


class TestSyncStatus:
    @pytest.mark.asyncio
    async def test_status_no_cache(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        status = await store.sync_status()
        assert status["cached"] is False
        assert status["last_synced"] is None
        assert status["cache_age_seconds"] is None

    @pytest.mark.asyncio
    async def test_status_after_load(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        await store.load()
        status = await store.sync_status()
        assert status["cached"] is True
        assert status["last_synced"] is not None
        assert isinstance(status["cache_age_seconds"], float)


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------


class TestContextManager:
    @pytest.mark.asyncio
    async def test_aclose_called_on_exit(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        async with store:
            pass
        client.aclose.assert_called_once()
