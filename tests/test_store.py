"""Tests for :mod:`omnifocus.store`."""

from __future__ import annotations

import pickle
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnifocus.errors import OFEncryptionError, OFWebDAVError
from omnifocus.models import OFModel
from omnifocus.store import OFocusStore
from omnifocus.sync.webdav import WebDAVClient
from tests.conftest import make_zip


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EMPTY_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    '<omnifocus xmlns="http://www.omnigroup.com/namespace/OmniFocus/v2"/>'
)


def _make_store(
    tmp_path: Path,
    filenames: list[str] | None = None,
    baseline_bytes: bytes | None = None,
    passphrase: str | None = None,
    encrypted_plist: bytes | None = None,
) -> tuple[OFocusStore, AsyncMock]:
    """Build an OFocusStore with a mocked WebDAVClient.

    *baseline_bytes* is returned for any ZIP file request.
    *encrypted_plist* is returned when ``get_file("encrypted")`` is called;
    if ``None``, a 404 WebDAVError is raised instead (unencrypted bundle).
    """
    client = AsyncMock(spec=WebDAVClient)
    client.list_bundle = AsyncMock(
        return_value=filenames or ["00000000000000=base.zip"]
    )

    _baseline = baseline_bytes or make_zip(_EMPTY_XML)

    async def _get_file(name: str) -> bytes:
        if name == "encrypted":
            if encrypted_plist is not None:
                return encrypted_plist
            raise OFWebDAVError("Not found", status_code=404)
        return _baseline

    client.get_file = AsyncMock(side_effect=_get_file)
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
        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base.zip", "20260322T154011=tx.zip"],
        )
        # Both the baseline and the transaction return the same plain XML ZIP
        client.get_file = AsyncMock(return_value=make_zip(_EMPTY_XML))
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
        # Bundle listing is checked again, but the ZIP payload is not re-downloaded.
        assert client.list_bundle.call_count == 2
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_changed_transaction_listing_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()

        client.list_bundle.return_value = [
            "00000000000000=base.zip",
            "20260322T154011=tx.zip",
        ]

        await store.load()
        assert client.list_bundle.call_count == 2
        assert client.get_file.call_count == 3

    @pytest.mark.asyncio
    async def test_changed_baseline_listing_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()

        client.list_bundle.return_value = ["00000000000000=base-v2.zip"]

        await store.load()
        assert client.list_bundle.call_count == 2
        assert client.get_file.call_count == 2


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
        assert cached.model.parsed_at == model.parsed_at
        assert cached.bundle_fingerprint == ("00000000000000=base.zip", ())

    @pytest.mark.asyncio
    async def test_corrupt_cache_is_ignored(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        (tmp_path / "of_model.pkl").write_bytes(b"not a pickle")
        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_legacy_model_only_cache_is_treated_as_stale(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        legacy_model = OFModel()
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(legacy_model))

        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.list_bundle.call_count == 1
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_unexpected_cache_payload_is_treated_as_stale(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps("unexpected"))

        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.list_bundle.call_count == 1
        assert client.get_file.call_count == 1


# ---------------------------------------------------------------------------
# Encryption
# ---------------------------------------------------------------------------


class TestEncryption:
    @pytest.mark.asyncio
    async def test_encrypted_data_decrypted_before_parse(
        self, tmp_path: Path
    ) -> None:
        """Encrypted baseline must be decrypted before passing to the parser."""
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(
            plaintext, "passphrase123"
        )
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_file,
            passphrase="passphrase123",
            encrypted_plist=encrypted_plist,
        )
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_encrypted_without_passphrase_raises(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(
            plaintext, "passphrase123"
        )
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_file,
            passphrase=None,
            encrypted_plist=encrypted_plist,
        )
        with pytest.raises(OFEncryptionError, match="no passphrase is available"):
            await store.load()

    @pytest.mark.asyncio
    async def test_wrong_passphrase_raises(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(
            plaintext, "correct-passphrase"
        )
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_file,
            passphrase="wrong-passphrase",
            encrypted_plist=encrypted_plist,
        )
        with pytest.raises(OFEncryptionError, match="HMAC verification failed"):
            await store.load()

    @pytest.mark.asyncio
    async def test_unencrypted_data_passes_through(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path, passphrase=None)
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_plaintext_transaction_in_encrypted_bundle(
        self, tmp_path: Path
    ) -> None:
        """A non-encrypted transaction ZIP in an otherwise encrypted bundle passes through."""
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(
            plaintext, "pw"
        )
        plain_tx = make_zip(_EMPTY_XML)  # transaction is not encrypted

        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base.zip", "20260322T154011=tx.zip"],
            passphrase="pw",
            encrypted_plist=encrypted_plist,
        )

        async def get_file(name: str) -> bytes:
            if name == "encrypted":
                return encrypted_plist
            if name == "00000000000000=base.zip":
                return encrypted_baseline
            return plain_tx  # transaction is plain ZIP

        client.get_file = AsyncMock(side_effect=get_file)
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_unknown_key_slot_raises(self, tmp_path: Path) -> None:
        """File references key slot not present in the document keys."""
        from omnifocus.crypto.encryption import create_encrypted_bundle, encrypt_file

        plaintext = make_zip(_EMPTY_XML)
        # Build plist with slot_id=1, but encrypt file with key_id=99
        encrypted_plist, _ = create_encrypted_bundle(plaintext, "pw", slot_id=1)
        aes_key, hmac_key = b"A" * 16, b"B" * 16
        bad_file = encrypt_file(plaintext, aes_key, hmac_key, key_id=99)

        store, _ = _make_store(
            tmp_path,
            baseline_bytes=bad_file,
            passphrase="pw",
            encrypted_plist=encrypted_plist,
        )
        with pytest.raises(OFEncryptionError, match="Key slot 99 not found"):
            await store.load()


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
        assert status["cache_valid"] is False

    @pytest.mark.asyncio
    async def test_status_after_load(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        status = await store.sync_status()
        assert status["cached"] is True
        assert status["last_synced"] is not None
        assert isinstance(status["cache_age_seconds"], float)
        assert status["cache_valid"] is True
        assert client.list_bundle.call_count == 2

    @pytest.mark.asyncio
    async def test_status_marks_cache_invalid_when_remote_listing_changes(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        client.list_bundle.return_value = [
            "00000000000000=base.zip",
            "20260322T154011=tx.zip",
        ]

        status = await store.sync_status()
        assert status["cached"] is True
        assert status["cache_valid"] is False

    @pytest.mark.asyncio
    async def test_status_marks_legacy_cache_invalid_without_hitting_remote(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(tmp_path)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(OFModel()))

        status = await store.sync_status()
        assert status["cached"] is True
        assert status["cache_valid"] is False
        assert client.list_bundle.call_count == 0


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
