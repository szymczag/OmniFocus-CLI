"""Tests for :mod:`omnifocus.store`."""

from __future__ import annotations

import json
import pickle
import zipfile
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from omnifocus.errors import OFEncryptionError, OFError, OFWebDAVError
from omnifocus.models import OFModel, Project, Task
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
NOW = datetime(2026, 3, 22, 12, 0, 0, tzinfo=UTC)


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
    client.list_bundle = AsyncMock(return_value=filenames or ["00000000000000=base.zip"])

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


def _read_contents_xml(data: bytes) -> str:
    """Return the XML payload from a transaction ZIP."""
    with zipfile.ZipFile(BytesIO(data)) as archive:
        return archive.read("contents.xml").decode("utf-8")


def _make_task() -> Task:
    """Build a stable test task."""
    return Task(
        id="t1",
        name="Write tests",
        parent_task_id="p1",
        project_id="p1",
        inbox=False,
        completed=None,
        flagged=False,
        due=None,
        start=None,
        hidden=None,
        note="",
        rank=100,
        repetition_rule=None,
        estimated_minutes=None,
        added=NOW,
        modified=NOW,
    )


def _make_project() -> Project:
    """Build a stable test project."""
    return Project(
        id="p1",
        name="Engineering",
        folder_id="f1",
        status="active",
        singleton=False,
        rank=100,
        added=NOW,
        modified=NOW,
        flagged=False,
        due=None,
        start=None,
        note="",
        completed=None,
    )


# ---------------------------------------------------------------------------
# from_env
# ---------------------------------------------------------------------------


class TestFromEnv:
    def test_missing_webdav_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for v in ("OF_WEBDAV_URL", "OF_WEBDAV_USER", "OF_WEBDAV_PASS"):
            monkeypatch.delenv(v, raising=False)
        with pytest.raises(OFWebDAVError):
            OFocusStore.from_env()

    def test_passphrase_falls_back_to_webdav_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """When OF_ENCRYPTION_PASSPHRASE is absent, the WebDAV password is used."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "linked_pass")
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "linked_pass"  # noqa: S105

    def test_passphrase_falls_back_to_url_embedded_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credentials in URL, no separate vars → passphrase taken from URL."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://u:url_pass@dav.example.com/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "url_pass"  # noqa: S105

    def test_passphrase_set_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Explicit OF_ENCRYPTION_PASSPHRASE overrides the WebDAV password fallback."""
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        monkeypatch.setenv("OF_ENCRYPTION_PASSPHRASE", "secret")
        store = OFocusStore.from_env()
        assert store._passphrase == "secret"  # noqa: S105


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
        cached = pickle.loads((tmp_path / "of_model.pkl").read_bytes())  # noqa: S301
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
    async def test_encrypted_data_decrypted_before_parse(self, tmp_path: Path) -> None:
        """Encrypted baseline must be decrypted before passing to the parser."""
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(plaintext, "passphrase123")
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_file,
            passphrase="passphrase123",  # noqa: S106
            encrypted_plist=encrypted_plist,
        )
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_encrypted_without_passphrase_raises(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(plaintext, "passphrase123")
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
        encrypted_plist, encrypted_file = create_encrypted_bundle(plaintext, "correct-passphrase")
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_file,
            passphrase="wrong-passphrase",  # noqa: S106
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
    async def test_plaintext_transaction_in_encrypted_bundle(self, tmp_path: Path) -> None:
        """A non-encrypted transaction ZIP in an otherwise encrypted bundle passes through."""
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(plaintext, "pw")
        plain_tx = make_zip(_EMPTY_XML)  # transaction is not encrypted

        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base.zip", "20260322T154011=tx.zip"],
            passphrase="pw",  # noqa: S106
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
            passphrase="pw",  # noqa: S106
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
# Write path
# ---------------------------------------------------------------------------


class TestWritePath:
    @pytest.mark.asyncio
    async def test_add_task_creates_writer_state_file(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)

        result = await store.add_task(name="New task")

        assert result["status"] == "created"
        state_path = tmp_path / "writer_state.json"
        assert state_path.exists()
        saved = json.loads(state_path.read_text())
        assert isinstance(saved["client_id"], str)
        assert saved["predecessor_id"] == saved["client_id"]
        assert saved["encrypted"] is False

    @pytest.mark.asyncio
    async def test_reuses_same_client_id_across_writes(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        await store.add_task(name="First task")
        first_state = json.loads((tmp_path / "writer_state.json").read_text())

        await store.add_task(name="Second task")
        second_state = json.loads((tmp_path / "writer_state.json").read_text())

        assert second_state["client_id"] == first_state["client_id"]

    @pytest.mark.asyncio
    async def test_refreshes_predecessor_when_remote_listing_changes(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="First task")
        client.list_bundle.return_value = [
            "00000000000000=base.zip",
            "20260322T154011=remote123+parent123.zip",
        ]

        await store.add_task(name="Second task")

        uploaded_name = client.put_file.await_args_list[-1].args[0]
        assert "+remote123.zip" in uploaded_name
        payload = json.loads((tmp_path / "writer_state.json").read_text())
        assert payload["predecessor_id"] == uploaded_name.split("=", 1)[1].split("+", 1)[0]

    @pytest.mark.asyncio
    async def test_no_transactions_uses_client_id_as_predecessor(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)

        await store.add_task(name="First task")

        payload = json.loads((tmp_path / "writer_state.json").read_text())
        uploaded_name = client.put_file.await_args_list[-1].args[0]
        assert f"={payload['client_id']}+{payload['client_id']}.zip" in uploaded_name

    @pytest.mark.asyncio
    async def test_plaintext_bundle_uploads_plain_zip(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)

        await store.add_task(name="Plain task")

        uploaded = client.put_file.await_args.args[1]
        assert uploaded[:2] == b"PK"
        assert "Plain task" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_encrypted_bundle_uploads_ciphertext(self, tmp_path: Path) -> None:
        from omnifocus.crypto.discovery import MAGIC
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(plaintext, "secret")
        store, client = _make_store(
            tmp_path,
            baseline_bytes=encrypted_baseline,
            passphrase="secret",  # noqa: S106
            encrypted_plist=encrypted_plist,
        )

        await store.add_task(name="Encrypted task")

        uploaded = client.put_file.await_args.args[1]
        assert uploaded.startswith(MAGIC)

    @pytest.mark.asyncio
    async def test_encrypted_bundle_without_passphrase_raises_on_write(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(plaintext, "secret")
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_baseline,
            passphrase=None,
            encrypted_plist=encrypted_plist,
        )

        with pytest.raises(OFEncryptionError, match="no passphrase is available"):
            await store.add_task(name="Encrypted task")

    @pytest.mark.asyncio
    async def test_missing_writable_key_slot_raises(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(plaintext, "secret")
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=encrypted_baseline,
            passphrase="secret",  # noqa: S106
            encrypted_plist=encrypted_plist,
        )
        store._load_writable_key_slot = lambda _: (_ for _ in ()).throw(  # type: ignore[method-assign]
            OFEncryptionError("No active writable encryption key slot found in bundle")
        )

        with pytest.raises(OFEncryptionError, match="No active writable encryption key slot"):
            await store.add_task(name="Encrypted task")

    @pytest.mark.asyncio
    async def test_successful_upload_invalidates_cache(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        await store.load()
        assert (tmp_path / "of_model.pkl").exists()

        await store.add_task(name="Invalidate cache")

        assert not (tmp_path / "of_model.pkl").exists()

    @pytest.mark.asyncio
    async def test_writer_state_updates_after_upload(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)

        await store.add_task(name="Track state")

        payload = json.loads((tmp_path / "writer_state.json").read_text())
        uploaded_name = client.put_file.await_args.args[0]
        uploaded_client_id = uploaded_name.split("=", 1)[1].split("+", 1)[0]
        assert payload["predecessor_id"] == uploaded_client_id

    @pytest.mark.asyncio
    async def test_update_task_uploads_task_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        task = _make_task()

        result = await store.update_task(task)

        assert result == {"status": "updated", "task_id": "t1", "name": "Write tests"}
        uploaded = client.put_file.await_args.args[1]
        assert "Write tests" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_complete_task_uploads_task_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        task = _make_task()

        result = await store.complete_task(task)

        assert result == {"status": "completed", "task_id": "t1", "name": "Write tests"}
        uploaded = client.put_file.await_args.args[1]
        assert "<completed>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_complete_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        project = _make_project()

        result = await store.complete_project(project)

        assert result == {"status": "completed", "project_id": "p1", "name": "Engineering"}
        uploaded = client.put_file.await_args.args[1]
        xml = _read_contents_xml(uploaded)
        assert "<status>done</status>" in xml
        assert "<name>Engineering</name>" in xml

    @pytest.mark.asyncio
    async def test_add_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)

        result = await store.add_project(name="Launch", folder_id="f1", status="inactive")

        assert result == {"status": "created", "project_id": result["project_id"], "name": "Launch"}
        uploaded = client.put_file.await_args.args[1]
        xml = _read_contents_xml(uploaded)
        assert "<name>Launch</name>" in xml
        assert "<status>inactive</status>" in xml

    @pytest.mark.asyncio
    async def test_update_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        project = _make_project()

        result = await store.update_project(project)

        assert result == {"status": "updated", "project_id": "p1", "name": "Engineering"}
        uploaded = client.put_file.await_args.args[1]
        assert "<name>Engineering</name>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_upload_transaction_rejects_missing_writable_key_slot(
        self, tmp_path: Path
    ) -> None:
        store, _ = _make_store(tmp_path)

        with pytest.raises(OFEncryptionError, match="Encrypted bundle has no writable key slot"):
            await store._upload_transaction(  # noqa: SLF001
                "20260322T154011=client+parent.zip",
                b"payload",
                encrypted_plist=b"plist",
                key_slot=None,
            )

    def test_load_writer_state_invalid_json_returns_none(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        (tmp_path / "writer_state.json").write_text("{not json")
        assert store._load_writer_state() is None  # noqa: SLF001

    def test_load_writer_state_invalid_shape_returns_none(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        (tmp_path / "writer_state.json").write_text(json.dumps({"client_id": 1}))
        assert store._load_writer_state() is None  # noqa: SLF001

    def test_load_writer_state_invalid_encrypted_flag_returns_none(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        (tmp_path / "writer_state.json").write_text(
            json.dumps(
                {
                    "client_id": "abc",
                    "predecessor_id": "abc",
                    "encrypted": "yes",
                    "bundle_fingerprint": None,
                }
            )
        )
        assert store._load_writer_state() is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_prepare_writer_rejects_malformed_latest_transaction(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnifocus.sync.protocol import TransactionRef

        store, _ = _make_store(tmp_path)
        monkeypatch.setattr(
            "omnifocus.store.latest_transaction_ref",
            lambda _: TransactionRef(
                filename="20260322T154011=remote123+.zip",
                client_id="remote123",
                parent_id="",
            ),
        )

        with pytest.raises(OFError, match="Malformed latest transaction filename"):
            await store._prepare_writer()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_upload_transaction_without_saved_state_returns_cleanly(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(tmp_path)

        await store._upload_transaction(  # noqa: SLF001
            "20260322T154011=client+parent.zip",
            make_zip(_EMPTY_XML),
            encrypted_plist=None,
            key_slot=None,
        )

        client.put_file.assert_awaited_once()


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
