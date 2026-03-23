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
from omnifocus.store import OFocusStore, _default_cache_dir, _WriterState
from omnifocus.sync.webdav import WebDAVClient
from omnifocus.writer import WritePlan
from tests.conftest import make_zip

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
    """Build an ``OFocusStore`` with a mocked ``WebDAVClient``."""
    client = AsyncMock(spec=WebDAVClient)
    entries = filenames or ["00000000000000=base+tail.zip"]
    client.list_entries = AsyncMock(return_value=entries)
    client.list_bundle = AsyncMock(return_value=[name for name in entries if name.endswith(".zip")])

    baseline = baseline_bytes or make_zip(_EMPTY_XML)

    async def _get_file(name: str) -> bytes:
        if name == "encrypted":
            if encrypted_plist is not None:
                return encrypted_plist
            raise OFWebDAVError("Not found", status_code=404)
        return baseline

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


def _make_writer_state() -> _WriterState:
    """Build a stable writer state."""
    return _WriterState(
        client_id="client123",
        host_id="ED325E58-F612-4653-BD34-7006A7D6DD52",
        device_name="air.local",
        registration_date=NOW,
        tail_identifiers=("tail123",),
        hardware_cpu_count="10",
        hardware_cpu_type="16777228,0",
        hardware_cpu_type_name="arm64",
        hardware_model="Mac16,12",
        os_version="25D2128",
        os_version_number="26.3.1",
        encrypted=False,
        bundle_fingerprint=None,
    )


class TestFromEnv:
    def test_missing_webdav_vars(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("OF_WEBDAV_URL", "OF_WEBDAV_USER", "OF_WEBDAV_PASS"):
            monkeypatch.delenv(name, raising=False)
        with pytest.raises(OFWebDAVError):
            OFocusStore.from_env()

    def test_passphrase_falls_back_to_webdav_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "linked_pass")
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "linked_pass"  # noqa: S105

    def test_passphrase_falls_back_to_url_embedded_pass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://u:url_pass@dav.example.com/of/")
        monkeypatch.delenv("OF_WEBDAV_USER", raising=False)
        monkeypatch.delenv("OF_WEBDAV_PASS", raising=False)
        monkeypatch.delenv("OF_ENCRYPTION_PASSPHRASE", raising=False)
        store = OFocusStore.from_env()
        assert store._passphrase == "url_pass"  # noqa: S105

    def test_passphrase_set_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        monkeypatch.setenv("OF_ENCRYPTION_PASSPHRASE", "secret")
        store = OFocusStore.from_env()
        assert store._passphrase == "secret"  # noqa: S105

    def test_cache_dir_defaults_to_repo_local_dot_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        monkeypatch.delenv("OF_CACHE_DIR", raising=False)
        store = OFocusStore.from_env()
        assert store._cache_dir.name == ".of-cache"

    def test_cache_dir_respects_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_WEBDAV_URL", "https://dav.example.com/of/")
        monkeypatch.setenv("OF_WEBDAV_USER", "u")
        monkeypatch.setenv("OF_WEBDAV_PASS", "p")
        monkeypatch.setenv("OF_CACHE_DIR", "/custom-cache")
        store = OFocusStore.from_env()
        assert store._cache_dir == Path("/custom-cache")


class TestDefaultCacheDir:
    def test_prefers_env_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_CACHE_DIR", "/custom-cache")
        assert _default_cache_dir() == Path("/custom-cache")

    def test_defaults_to_repo_local_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OF_CACHE_DIR", raising=False)
        assert _default_cache_dir().name == ".of-cache"

    def test_falls_back_to_tmp_when_repo_root_is_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("OF_CACHE_DIR", raising=False)
        monkeypatch.setattr(Path, "exists", lambda self: False)
        assert _default_cache_dir() == Path("/tmp/of-cache")  # noqa: S108


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
        client.list_entries.assert_called_once()
        client.get_file.assert_called_once_with("00000000000000=base+tail.zip")

    @pytest.mark.asyncio
    async def test_load_with_transactions(self, tmp_path: Path) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base+tail0.zip", "20260322154011=tail1+tail0.zip"],
        )
        client.get_file = AsyncMock(return_value=make_zip(_EMPTY_XML))
        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.get_file.call_count == 2

    @pytest.mark.asyncio
    async def test_force_refresh_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        assert client.list_entries.call_count == 1
        await store.load(force_refresh=True)
        assert client.list_entries.call_count == 2

    @pytest.mark.asyncio
    async def test_uses_cache_on_second_load(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        await store.load()
        assert client.list_entries.call_count == 2
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_changed_transaction_listing_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        client.list_entries.return_value = [
            "00000000000000=base+tail0.zip",
            "20260322154011=tail1+tail0.zip",
        ]
        await store.load()
        assert client.list_entries.call_count == 2
        assert client.get_file.call_count == 3

    @pytest.mark.asyncio
    async def test_changed_baseline_listing_bypasses_cache(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        client.list_entries.return_value = ["00000000000000=base-v2+tail1.zip"]
        await store.load()
        assert client.list_entries.call_count == 2
        assert client.get_file.call_count == 2


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
        await store.load()
        assert client.list_entries.call_count == 2

    @pytest.mark.asyncio
    async def test_invalidate_cache_noop_when_no_cache(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        store.invalidate_cache()

    @pytest.mark.asyncio
    async def test_cache_contains_valid_pickle(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        model = await store.load()
        cached = pickle.loads((tmp_path / "of_model.pkl").read_bytes())  # noqa: S301
        assert cached.model.parsed_at == model.parsed_at
        assert cached.bundle_fingerprint == ("00000000000000=base+tail.zip", (), ())

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
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps(OFModel()))
        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.list_entries.call_count == 1
        assert client.get_file.call_count == 1

    @pytest.mark.asyncio
    async def test_unexpected_cache_payload_is_treated_as_stale(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        (tmp_path / "of_model.pkl").write_bytes(pickle.dumps("unexpected"))
        model = await store.load()
        assert isinstance(model, OFModel)
        assert client.list_entries.call_count == 1
        assert client.get_file.call_count == 1


class TestEncryption:
    @pytest.mark.asyncio
    async def test_encrypted_data_decrypted_before_parse(self, tmp_path: Path) -> None:
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
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_baseline = create_encrypted_bundle(plaintext, "pw")
        plain_tx = make_zip(_EMPTY_XML)
        store, client = _make_store(
            tmp_path,
            filenames=["00000000000000=base+tail0.zip", "20260322154011=tail1+tail0.zip"],
            passphrase="pw",  # noqa: S106
            encrypted_plist=encrypted_plist,
        )

        async def get_file(name: str) -> bytes:
            if name == "encrypted":
                return encrypted_plist
            if name == "00000000000000=base+tail0.zip":
                return encrypted_baseline
            return plain_tx

        client.get_file = AsyncMock(side_effect=get_file)
        model = await store.load()
        assert isinstance(model, OFModel)

    @pytest.mark.asyncio
    async def test_unknown_key_slot_raises(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle, encrypt_file

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, _ = create_encrypted_bundle(plaintext, "pw", slot_id=1)
        bad_file = encrypt_file(plaintext, b"A" * 16, b"B" * 16, key_id=99)
        store, _ = _make_store(
            tmp_path,
            baseline_bytes=bad_file,
            passphrase="pw",  # noqa: S106
            encrypted_plist=encrypted_plist,
        )
        with pytest.raises(OFEncryptionError, match="Key slot 99 not found"):
            await store.load()


class TestSyncStatus:
    @pytest.mark.asyncio
    async def test_status_no_cache(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        status = await store.sync_status()
        assert status == {
            "last_synced": None,
            "cached": False,
            "cache_age_seconds": None,
            "cache_valid": False,
            "bundle_state_version": 2,
            "registered_client": False,
            "tail_identifiers": [],
            "advertised_tail_identifiers": [],
            "client_id": None,
            "host_id": None,
            "current_tail_identifier": None,
        }

    @pytest.mark.asyncio
    async def test_status_after_load(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        status = await store.sync_status()
        assert status["cached"] is True
        assert status["last_synced"] is not None
        assert isinstance(status["cache_age_seconds"], float)
        assert status["cache_valid"] is True
        assert status["bundle_state_version"] == 2
        assert status["registered_client"] is False
        assert status["tail_identifiers"] == []
        assert status["advertised_tail_identifiers"] == []
        assert status["client_id"] is None
        assert status["host_id"] is None
        assert status["current_tail_identifier"] == "tail"
        assert client.list_entries.call_count == 2

    @pytest.mark.asyncio
    async def test_status_marks_cache_invalid_when_remote_listing_changes(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(tmp_path)
        await store.load()
        client.list_entries.return_value = [
            "00000000000000=base+tail0.zip",
            "20260322154011=tail1+tail0.zip",
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
        assert client.list_entries.call_count == 0

    @pytest.mark.asyncio
    async def test_status_exposes_registered_client_tail_identifiers(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        store._save_writer_state(_make_writer_state())  # noqa: SLF001
        status = await store.sync_status()
        assert status["registered_client"] is True
        assert status["tail_identifiers"] == ["tail123"]
        assert status["client_id"] == "client123"
        assert status["host_id"] == "ED325E58-F612-4653-BD34-7006A7D6DD52"

    @pytest.mark.asyncio
    async def test_status_current_tail_uses_remote_client_documents(self, tmp_path: Path) -> None:
        client_doc = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>clientIdentifier</key><string>client123</string>
<key>hostID</key><string>host-123</string>
<key>name</key><string>air.local</string>
<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>
<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>
<key>tailIdentifiers</key><array><string>tail123</string></array>
</dict></plist>"""
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base.zip",
                "20260322154011=client123.client",
            ],
        )
        await store.load()

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return client_doc
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        status = await store.sync_status()
        assert status["current_tail_identifier"] == "tail123"

    @pytest.mark.asyncio
    async def test_status_current_tail_swallow_remote_client_parse_errors(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base.zip",
                "20260322154011=client123.client",
            ],
        )
        await store.load()

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return b"not plist"
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        status = await store.sync_status()
        assert status["current_tail_identifier"] is None


class TestWritePath:
    @pytest.mark.asyncio
    async def test_add_task_creates_writer_state_file(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        result = await store.add_task(name="New task")
        assert result["status"] == "created"
        saved = json.loads((tmp_path / "writer_state.json").read_text())
        assert isinstance(saved["client_id"], str)
        assert isinstance(saved["host_id"], str)
        assert len(saved["host_id"]) == 36
        assert saved["device_name"]
        assert isinstance(saved["registration_date"], str)
        assert isinstance(saved["tail_identifiers"], list)
        assert len(saved["tail_identifiers"]) == 1
        assert saved["hardware_model"] is None
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
    async def test_uses_explicit_env_identity_without_saved_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_CLIENT_ID", "STATICCLIENT01")
        monkeypatch.setenv("OF_DEVICE_NAME", "air.local")
        monkeypatch.setenv("OF_DEVICE_HOST_ID", "ED325E58-F612-4653-BD34-7006A7D6DD52")

        store, _ = _make_store(tmp_path)
        await store.add_task(name="First task")
        first_state = json.loads((tmp_path / "writer_state.json").read_text())

        fresh_tmp = tmp_path / "fresh"
        fresh_tmp.mkdir()
        fresh_store, _ = _make_store(fresh_tmp)
        await fresh_store.add_task(name="Second task")
        second_state = json.loads((fresh_tmp / "writer_state.json").read_text())

        assert first_state["client_id"] == "STATICCLIENT01"
        assert second_state["client_id"] == "STATICCLIENT01"
        assert first_state["device_name"] == "air.local"
        assert second_state["device_name"] == "air.local"
        assert first_state["host_id"] == "ED325E58-F612-4653-BD34-7006A7D6DD52"
        assert second_state["host_id"] == "ED325E58-F612-4653-BD34-7006A7D6DD52"

    @pytest.mark.asyncio
    async def test_refreshes_tail_when_remote_listing_changes(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="First task")
        client.list_entries.return_value = [
            "00000000000000=base+tail0.zip",
            "20260322154011=remote123+tail0.zip",
        ]
        await store.add_task(name="Second task")
        zip_uploads = [
            call.args[0]
            for call in client.put_file.await_args_list
            if call.args[0].endswith(".zip")
        ]
        delta_upload = zip_uploads[-2]
        assert delta_upload.startswith("202")
        assert "=remote123+" in delta_upload
        payload = json.loads((tmp_path / "writer_state.json").read_text())
        assert payload["tail_identifiers"] == ["remote123"]

    @pytest.mark.asyncio
    async def test_no_remote_deltas_uses_baseline_tail_as_parent(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="First task")
        zip_uploads = [
            call.args[0]
            for call in client.put_file.await_args_list
            if call.args[0].endswith(".zip")
        ]
        delta_upload = zip_uploads[0]
        assert "=tail+" in delta_upload
        payload = json.loads((tmp_path / "writer_state.json").read_text())
        assert payload["tail_identifiers"] == ["tail"]

    @pytest.mark.asyncio
    async def test_plaintext_bundle_uploads_plain_zip(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="Plain task")
        uploaded = client.put_file.await_args_list[0].args[1]
        assert uploaded[:2] == b"PK"
        assert "<name/>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_write_also_uploads_client_state(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        zip_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".zip")
        ]
        client_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".client")
        ]
        delta_name = zip_uploads[-1].args[0]
        client_name = client_uploads[-1].args[0]
        client_payload = client_uploads[-1].args[1]
        assert delta_name.endswith(".zip")
        assert client_name.endswith(".client")
        assert b"<plist" in client_payload

    @pytest.mark.asyncio
    async def test_add_task_uploads_multiple_deltas(self, tmp_path: Path) -> None:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("OF_CHAIN_SHAPE", "linear")
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        monkeypatch.undo()
        zip_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".zip")
        ]
        client_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".client")
        ]
        assert len(zip_uploads) >= 2
        assert len(client_uploads) == len(zip_uploads)
        first_name = zip_uploads[0].args[0]
        second_name = zip_uploads[1].args[0]
        first_head = first_name.split("=", 1)[1].split("+", 1)[0]
        second_parent = second_name.split("+", 1)[1].removesuffix(".zip")
        assert second_parent == first_head

    @pytest.mark.asyncio
    async def test_add_task_second_delta_updates_name(self, tmp_path: Path) -> None:
        monkeypatch = pytest.MonkeyPatch()
        monkeypatch.setenv("OF_CHAIN_SHAPE", "linear")
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        monkeypatch.undo()
        zip_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".zip")
        ]
        second_xml = _read_contents_xml(zip_uploads[1].args[1])
        assert 'op="update"' in second_xml
        assert "<name>Track state</name>" in second_xml

    @pytest.mark.asyncio
    async def test_add_task_chain_then_client_uploads_client_once(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_WRITE_STRATEGY", "chain_then_client")
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        client_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".client")
        ]
        assert len(client_uploads) == 1

    @pytest.mark.asyncio
    async def test_add_task_client_after_each_delta_uploads_client_each_time(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_WRITE_STRATEGY", "client_after_each_delta")
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        zip_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".zip")
        ]
        client_uploads = [
            call for call in client.put_file.await_args_list if call.args[0].endswith(".client")
        ]
        assert len(client_uploads) == len(zip_uploads)

    @pytest.mark.asyncio
    async def test_default_chain_shape_is_app_rebase(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        zip_uploads = [
            call.args[0]
            for call in client.put_file.await_args_list
            if call.args[0].endswith(".zip")
        ]
        first = zip_uploads[0].split("=", 1)[1].removesuffix(".zip")
        second = zip_uploads[1].split("=", 1)[1].removesuffix(".zip")
        first_head, first_parent = first.split("+", 1)
        second_head, second_parent = second.split("+", 1)
        assert first_head == "tail"
        assert second_head == first_parent
        assert second_parent != first_head

    @pytest.mark.asyncio
    async def test_invalid_write_strategy_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_WRITE_STRATEGY", "bogus")
        store, _ = _make_store(tmp_path)
        with pytest.raises(OFError, match="Invalid OF_WRITE_STRATEGY"):
            await store.add_task(name="Track state")

    @pytest.mark.asyncio
    async def test_invalid_chain_shape_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_CHAIN_SHAPE", "bogus")
        store, _ = _make_store(tmp_path)
        with pytest.raises(OFError, match="Invalid OF_CHAIN_SHAPE"):
            await store.add_task(name="Track state")

    @pytest.mark.asyncio
    async def test_app_rebase_chain_shape_changes_parent_model(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_CHAIN_SHAPE", "app_rebase")
        store, client = _make_store(tmp_path)
        await store.add_task(name="Track state")
        zip_uploads = [
            call.args[0]
            for call in client.put_file.await_args_list
            if call.args[0].endswith(".zip")
        ]
        first = zip_uploads[0].split("=", 1)[1].removesuffix(".zip")
        second = zip_uploads[1].split("=", 1)[1].removesuffix(".zip")
        first_head, first_parent = first.split("+", 1)
        second_head, second_parent = second.split("+", 1)
        assert first_head == "tail"
        assert second_head == first_parent
        assert second_parent != first_head

    @pytest.mark.asyncio
    async def test_initial_writer_state_metadata_comes_from_env_not_remote_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("OF_DEVICE_HARDWARE_MODEL", "Mac16,12")
        monkeypatch.setenv("OF_DEVICE_OS_VERSION", "25D2128")
        monkeypatch.setenv("OF_DEVICE_OS_VERSION_NUMBER", "26.3.1")
        store, _ = _make_store(tmp_path)
        await store.add_task(name="Track state")
        payload = json.loads((tmp_path / "writer_state.json").read_text())
        assert payload["hardware_model"] == "Mac16,12"
        assert payload["os_version"] == "25D2128"
        assert payload["os_version_number"] == "26.3.1"

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
        uploaded = client.put_file.await_args_list[0].args[1]
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
        assert payload["tail_identifiers"] == ["tail"]

    @pytest.mark.asyncio
    async def test_latest_remote_client_tail_is_preferred_over_latest_delta(
        self, tmp_path: Path
    ) -> None:
        store, _ = _make_store(tmp_path)
        from omnifocus.sync.client_state import ClientStateDocument
        from omnifocus.sync.protocol import build_bundle_state

        state = build_bundle_state(
            [
                "00000000000000=base+tail0.zip",
                "20260322154011=delta999+tail0.zip",
                "20260322154012=appclient.client",
            ]
        )
        remote_clients = {
            "appclient": ClientStateDocument(
                client_identifier="appclient",
                tail_identifiers=("client-tail",),
                registration_date=NOW,
                last_sync_date=NOW,
                name="air.local",
                host_id="ED325E58-F612-4653-BD34-7006A7D6DD52",
            )
        }
        assert store._current_tail_id(state, remote_clients) == "client-tail"  # type: ignore[attr-defined]

    def test_current_tail_skips_client_without_tail_and_uses_next_latest(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.sync.client_state import ClientStateDocument
        from omnifocus.sync.protocol import build_bundle_state

        store, _ = _make_store(tmp_path)
        state = build_bundle_state(
            [
                "00000000000000=snapshot+baseline-tail.zip",
                "20260322154011=clientA.client",
                "20260322154012=clientB.client",
            ]
        )
        remote_clients = {
            "clientA": ClientStateDocument(
                client_identifier="clientA",
                host_id="hostA",
                name="a.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("tail-a",),
            ),
            "clientB": ClientStateDocument(
                client_identifier="clientB",
                host_id="hostB",
                name="b.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=(),
            ),
        }
        assert store._current_tail_id(state, remote_clients) == "tail-a"  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_empty_add_task_plan_raises(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        writer_state = _make_writer_state()
        from omnifocus.writer import AddTaskPlan

        with pytest.raises(OFError, match="Task creation plan produced no deltas"):
            await store._upload_task_plan(  # type: ignore[attr-defined]
                AddTaskPlan(task_id="task-1", deltas=()),
                encrypted_plist=None,
                key_slot=None,
                writer_state=writer_state,
            )

    @pytest.mark.asyncio
    async def test_fetch_latest_deltas_returns_newest_files(self, tmp_path: Path) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=head1+tail0.zip",
                "20260322154012=head2+head1.zip",
            ],
        )
        client.get_file = AsyncMock(side_effect=lambda name: name.encode("utf-8"))
        files = await store.fetch_latest_deltas(count=2)
        assert [name for name, _ in files] == [
            "20260322154011=head1+tail0.zip",
            "20260322154012=head2+head1.zip",
        ]

    @pytest.mark.asyncio
    async def test_fetch_file_returns_named_payload(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        client.get_file = AsyncMock(return_value=b"payload")
        assert await store.fetch_file("encrypted") == b"payload"

    @pytest.mark.asyncio
    async def test_fetch_latest_deltas_for_client_uses_client_tail(self, tmp_path: Path) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=head1+tail0.zip",
                "20260322154012=client-a.client",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return (
                    b'<?xml version="1.0" encoding="UTF-8"?>'
                    b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
                    b'<plist version="1.0"><dict>'
                    b"<key>clientIdentifier</key><string>client-a</string>"
                    b"<key>hostID</key><string>host-a</string>"
                    b"<key>name</key><string>a.local</string>"
                    b"<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>"
                    b"<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>"
                    b"<key>tailIdentifiers</key><array><string>head1</string></array>"
                    b"</dict></plist>"
                )
            return name.encode("utf-8")

        client.get_file = AsyncMock(side_effect=get_file)
        files = await store.fetch_latest_deltas(client_id="client-a")
        assert [name for name, _ in files] == ["20260322154011=head1+tail0.zip"]

    @pytest.mark.asyncio
    async def test_fetch_latest_deltas_unknown_client_raises(self, tmp_path: Path) -> None:
        store, _ = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=head1+tail0.zip",
            ],
        )
        with pytest.raises(OFError, match="No client state found"):
            await store.fetch_latest_deltas(client_id="client-a")

    @pytest.mark.asyncio
    async def test_fetch_latest_deltas_client_without_tail_raises(self, tmp_path: Path) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=client-a.client",
            ],
        )

        async def get_file(name: str) -> bytes:
            return (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
                b'<plist version="1.0"><dict>'
                b"<key>clientIdentifier</key><string>client-a</string>"
                b"<key>hostID</key><string>host-a</string>"
                b"<key>name</key><string>a.local</string>"
                b"<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>"
                b"<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>"
                b"<key>tailIdentifiers</key><array></array>"
                b"</dict></plist>"
            )

        client.get_file = AsyncMock(side_effect=get_file)
        with pytest.raises(OFError, match="has no advertised tail identifier"):
            await store.fetch_latest_deltas(client_id="client-a")

    @pytest.mark.asyncio
    async def test_fetch_latest_deltas_client_without_matching_delta_raises(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=client-a.client",
            ],
        )

        async def get_file(name: str) -> bytes:
            return (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                b'"http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
                b'<plist version="1.0"><dict>'
                b"<key>clientIdentifier</key><string>client-a</string>"
                b"<key>hostID</key><string>host-a</string>"
                b"<key>name</key><string>a.local</string>"
                b"<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>"
                b"<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>"
                b"<key>tailIdentifiers</key><array><string>missing-head</string></array>"
                b"</dict></plist>"
            )

        client.get_file = AsyncMock(side_effect=get_file)
        with pytest.raises(OFError, match="No delta ZIP found"):
            await store.fetch_latest_deltas(client_id="client-a")

    @pytest.mark.asyncio
    async def test_fetch_latest_client_returns_requested_client(self, tmp_path: Path) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=client-a.client",
                "20260322154012=client-b.client",
            ],
        )
        client.get_file = AsyncMock(side_effect=lambda name: name.encode("utf-8"))
        name, payload = await store.fetch_latest_client(client_id="client-b")
        assert name == "20260322154012=client-b.client"
        assert payload == b"20260322154012=client-b.client"

    @pytest.mark.asyncio
    async def test_fetch_latest_client_without_any_clients_raises(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path, filenames=["00000000000000=base+tail0.zip"])
        with pytest.raises(OFError, match="No client state files found"):
            await store.fetch_latest_client()

    @pytest.mark.asyncio
    async def test_fetch_latest_client_unknown_client_raises(self, tmp_path: Path) -> None:
        store, _ = _make_store(
            tmp_path,
            filenames=["00000000000000=base+tail0.zip", "20260322154012=client-b.client"],
        )
        with pytest.raises(OFError, match="No client state found for client"):
            await store.fetch_latest_client(client_id="client-a")

    @pytest.mark.asyncio
    async def test_decrypt_latest_delta_plaintext_without_encrypted_plist(
        self, tmp_path: Path
    ) -> None:
        xml = '<?xml version="1.0" encoding="UTF-8"?><omnifocus xmlns="x"/>'
        zip_bytes = make_zip(xml)
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=head1+tail0.zip",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name == "encrypted":
                raise OFWebDAVError("Not found", status_code=404)
            return zip_bytes

        client.get_file = AsyncMock(side_effect=get_file)
        filename, contents_xml = await store.decrypt_latest_delta()
        assert filename == "20260322154011=head1+tail0.zip"
        assert contents_xml == xml

    @pytest.mark.asyncio
    async def test_decrypt_latest_delta_with_no_deltas_raises(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path, filenames=["00000000000000=base+tail0.zip"])
        with pytest.raises(OFError, match="No delta ZIPs found"):
            await store.decrypt_latest_delta()

    @pytest.mark.asyncio
    async def test_decrypt_latest_delta_re_raises_non_404_encrypted_fetch_error(
        self, tmp_path: Path
    ) -> None:
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=head1+tail0.zip",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name == "encrypted":
                raise OFWebDAVError("Forbidden", status_code=403)
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        with pytest.raises(OFWebDAVError, match="Forbidden"):
            await store.decrypt_latest_delta()

    @pytest.mark.asyncio
    async def test_update_task_uploads_task_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.update_task(_make_task())
        assert result == {"status": "updated", "task_id": "t1", "name": "Write tests"}
        uploaded = client.put_file.await_args_list[0].args[1]
        assert "Write tests" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_complete_task_uploads_task_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.complete_task(_make_task())
        assert result == {"status": "completed", "task_id": "t1", "name": "Write tests"}
        uploaded = client.put_file.await_args_list[0].args[1]
        assert "<completed>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_complete_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.complete_project(_make_project())
        assert result == {"status": "completed", "project_id": "p1", "name": "Engineering"}
        uploaded = client.put_file.await_args_list[0].args[1]
        xml = _read_contents_xml(uploaded)
        assert "<status>done</status>" in xml
        assert "<name>Engineering</name>" in xml

    @pytest.mark.asyncio
    async def test_drop_task_uploads_hidden_task_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.drop_task(_make_task())
        assert result == {"status": "dropped", "task_id": "t1", "name": "Write tests"}
        uploaded = client.put_file.await_args_list[0].args[1]
        assert "<hidden>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_drop_project_uploads_dropped_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.drop_project(_make_project())
        assert result == {"status": "dropped", "project_id": "p1", "name": "Engineering"}
        uploaded = client.put_file.await_args_list[0].args[1]
        xml = _read_contents_xml(uploaded)
        assert "<status>dropped</status>" in xml

    @pytest.mark.asyncio
    async def test_add_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.add_project(name="Launch", folder_id="f1", status="inactive")
        assert result == {"status": "created", "project_id": result["project_id"], "name": "Launch"}
        uploaded = client.put_file.await_args_list[0].args[1]
        xml = _read_contents_xml(uploaded)
        assert "<name>Launch</name>" in xml
        assert "<status>inactive</status>" in xml

    @pytest.mark.asyncio
    async def test_update_project_uploads_project_transaction(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        result = await store.update_project(_make_project())
        assert result == {"status": "updated", "project_id": "p1", "name": "Engineering"}
        uploaded = client.put_file.await_args_list[0].args[1]
        assert "<name>Engineering</name>" in _read_contents_xml(uploaded)

    @pytest.mark.asyncio
    async def test_upload_transaction_rejects_missing_writable_key_slot(
        self, tmp_path: Path
    ) -> None:
        store, _ = _make_store(tmp_path)
        with pytest.raises(OFEncryptionError, match="Encrypted bundle has no writable key slot"):
            await store._upload_transaction(  # noqa: SLF001
                "20260322154011=client+parent.zip",
                b"payload",
                encrypted_plist=b"plist",
                key_slot=None,
                writer_state=_make_writer_state(),
            )

    @pytest.mark.asyncio
    async def test_upload_write_plan_rejects_empty_plan(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        with pytest.raises(OFError, match="Write plan produced no deltas"):
            await store._upload_write_plan(  # noqa: SLF001
                WritePlan(deltas=()),
                encrypted_plist=None,
                key_slot=None,
                writer_state=_make_writer_state(),
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
                    "host_id": "host",
                    "device_name": "air.local",
                    "registration_date": NOW.isoformat(),
                    "tail_identifiers": ["tail"],
                    "hardware_cpu_count": None,
                    "hardware_cpu_type": None,
                    "hardware_cpu_type_name": None,
                    "hardware_model": None,
                    "os_version": None,
                    "os_version_number": None,
                    "encrypted": "yes",
                    "bundle_fingerprint": None,
                }
            )
        )
        assert store._load_writer_state() is None  # noqa: SLF001

    def test_load_writer_state_with_valid_fingerprint_returns_state(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        payload = {
            "client_id": "abc",
            "host_id": "host",
            "device_name": "air.local",
            "registration_date": NOW.isoformat(),
            "tail_identifiers": ["tail"],
            "hardware_cpu_count": "10",
            "hardware_cpu_type": "16777228,0",
            "hardware_cpu_type_name": "arm64",
            "hardware_model": "Mac16,12",
            "os_version": "25D2128",
            "os_version_number": "26.3.1",
            "encrypted": False,
            "bundle_fingerprint": [
                "00000000000000=base+tail.zip",
                ["20260322154011=head+tail.zip"],
                ["20260322154012=client.client"],
            ],
        }
        (tmp_path / "writer_state.json").write_text(json.dumps(payload))
        state = store._load_writer_state()  # noqa: SLF001
        assert state is not None
        assert state.bundle_fingerprint == (
            "00000000000000=base+tail.zip",
            ("20260322154011=head+tail.zip",),
            ("20260322154012=client.client",),
        )

    def test_load_writer_state_invalid_tail_identifiers_returns_none(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        payload = {
            "client_id": "abc",
            "host_id": "host",
            "device_name": "air.local",
            "registration_date": NOW.isoformat(),
            "tail_identifiers": [123],
            "hardware_cpu_count": None,
            "hardware_cpu_type": None,
            "hardware_cpu_type_name": None,
            "hardware_model": None,
            "os_version": None,
            "os_version_number": None,
            "encrypted": False,
            "bundle_fingerprint": None,
        }
        (tmp_path / "writer_state.json").write_text(json.dumps(payload))
        assert store._load_writer_state() is None  # noqa: SLF001

    def test_load_writer_state_invalid_registration_date_returns_none(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        payload = {
            "client_id": "abc",
            "host_id": "host",
            "device_name": "air.local",
            "registration_date": "not-a-date",
            "tail_identifiers": ["tail"],
            "hardware_cpu_count": None,
            "hardware_cpu_type": None,
            "hardware_cpu_type_name": None,
            "hardware_model": None,
            "os_version": None,
            "os_version_number": None,
            "encrypted": False,
            "bundle_fingerprint": None,
        }
        (tmp_path / "writer_state.json").write_text(json.dumps(payload))
        assert store._load_writer_state() is None  # noqa: SLF001

    def test_load_writer_state_invalid_optional_system_field_returns_none(
        self, tmp_path: Path
    ) -> None:
        store, _ = _make_store(tmp_path)
        payload = {
            "client_id": "abc",
            "host_id": "host",
            "device_name": "air.local",
            "registration_date": NOW.isoformat(),
            "tail_identifiers": ["tail"],
            "hardware_cpu_count": 10,
            "hardware_cpu_type": None,
            "hardware_cpu_type_name": None,
            "hardware_model": None,
            "os_version": None,
            "os_version_number": None,
            "encrypted": False,
            "bundle_fingerprint": None,
        }
        (tmp_path / "writer_state.json").write_text(json.dumps(payload))
        assert store._load_writer_state() is None  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_prepare_writer_rejects_missing_tail_identifier(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path, filenames=["00000000000000=base.zip"])
        client.get_file = AsyncMock(return_value=make_zip(_EMPTY_XML))
        with pytest.raises(OFError, match="Bundle has no known tail identifier"):
            await store._prepare_writer()  # noqa: SLF001

    @pytest.mark.asyncio
    async def test_prepare_writer_uses_remote_template_only_for_tail_not_device_profile(
        self, tmp_path: Path
    ) -> None:
        client_doc = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>clientIdentifier</key><string>remote-client</string>
<key>hostID</key><string>ED325E58-F612-4653-BD34-7006A7D6DD52</string>
<key>name</key><string>air.local</string>
<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>
<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>
<key>tailIdentifiers</key><array><string>tail0</string></array>
<key>HardwareCPUCount</key><string>10</string>
<key>HardwareCPUType</key><string>16777228,0</string>
<key>HardwareCPUTypeName</key><string>arm64</string>
<key>HardwareModel</key><string>Mac16,12</string>
<key>OSVersion</key><string>25D2128</string>
<key>OSVersionNumber</key><string>26.3.1</string>
</dict></plist>"""
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=remote-client.client",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return client_doc
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        _, _, _, writer_state = await store._prepare_writer()  # noqa: SLF001
        assert writer_state.device_name.endswith(".local")
        assert len(writer_state.host_id) == 36
        assert writer_state.hardware_model is None
        assert writer_state.os_version is None
        assert writer_state.os_version_number is None
        assert writer_state.tail_identifiers == ("tail0",)

    @pytest.mark.asyncio
    async def test_prepare_writer_prefers_explicit_env_identity_over_template(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client_doc = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>clientIdentifier</key><string>remote-client</string>
<key>hostID</key><string>REMOTE-HOST</string>
<key>name</key><string>remote.local</string>
<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>
<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>
<key>tailIdentifiers</key><array><string>tail0</string></array>
</dict></plist>"""
        monkeypatch.setenv("OF_CLIENT_ID", "STATICCLIENT01")
        monkeypatch.setenv("OF_DEVICE_NAME", "air.local")
        monkeypatch.setenv("OF_DEVICE_HOST_ID", "ED325E58-F612-4653-BD34-7006A7D6DD52")
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=remote-client.client",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return client_doc
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        _, _, _, writer_state = await store._prepare_writer()  # noqa: SLF001
        assert writer_state.client_id == "STATICCLIENT01"
        assert writer_state.device_name == "air.local"
        assert writer_state.host_id == "ED325E58-F612-4653-BD34-7006A7D6DD52"

    def test_current_tail_prefers_shared_client_tail(self, tmp_path: Path) -> None:
        from omnifocus.sync.client_state import ClientStateDocument
        from omnifocus.sync.protocol import build_bundle_state

        store, _ = _make_store(tmp_path)
        state = build_bundle_state(
            [
                "00000000000000=snapshot.zip",
                "20260322154011=clientA.client",
                "20260322154012=clientB.client",
            ]
        )
        remote_clients = {
            "clientA": ClientStateDocument(
                client_identifier="clientA",
                host_id="hostA",
                name="a.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("shared-tail",),
            ),
            "clientB": ClientStateDocument(
                client_identifier="clientB",
                host_id="hostB",
                name="b.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("shared-tail",),
            ),
        }
        assert store._current_tail_id(state, remote_clients) == "shared-tail"  # noqa: SLF001

    def test_current_tail_prefers_latest_client_when_clients_disagree(self, tmp_path: Path) -> None:
        from omnifocus.sync.client_state import ClientStateDocument
        from omnifocus.sync.protocol import build_bundle_state

        store, _ = _make_store(tmp_path)
        state = build_bundle_state(
            [
                "00000000000000=snapshot+baseline-tail.zip",
                "20260322154011=clientA.client",
                "20260322154012=clientB.client",
            ]
        )
        remote_clients = {
            "clientA": ClientStateDocument(
                client_identifier="clientA",
                host_id="hostA",
                name="a.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("tail-a",),
            ),
            "clientB": ClientStateDocument(
                client_identifier="clientB",
                host_id="hostB",
                name="b.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("tail-b",),
            ),
        }
        assert store._current_tail_id(state, remote_clients) == "tail-b"  # noqa: SLF001

    def test_select_client_template_returns_none_without_remote_clients(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.sync.protocol import build_bundle_state

        store, _ = _make_store(tmp_path)
        state = build_bundle_state(["00000000000000=snapshot+tail.zip"])
        assert store._select_client_template(state, {}) is None  # noqa: SLF001

    def test_select_client_template_skips_missing_latest_document(self, tmp_path: Path) -> None:
        from omnifocus.sync.client_state import ClientStateDocument
        from omnifocus.sync.protocol import build_bundle_state

        store, _ = _make_store(tmp_path)
        state = build_bundle_state(
            [
                "00000000000000=snapshot+tail.zip",
                "20260322154011=clientA.client",
                "20260322154012=clientB.client",
            ]
        )
        remote_clients = {
            "clientA": ClientStateDocument(
                client_identifier="clientA",
                host_id="hostA",
                name="a.local",
                registration_date=NOW,
                last_sync_date=NOW,
                tail_identifiers=("tail-a",),
            )
        }
        template = store._select_client_template(state, remote_clients)  # noqa: SLF001
        assert template is not None
        assert template.client_identifier == "clientA"

    @pytest.mark.asyncio
    async def test_upload_transaction_writes_delta_and_client_state(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        await store._upload_transaction(  # noqa: SLF001
            "20260322154011=client+parent.zip",
            make_zip(_EMPTY_XML),
            encrypted_plist=None,
            key_slot=None,
            writer_state=_make_writer_state(),
        )
        assert client.put_file.await_count == 2
        assert client.put_file.await_args_list[0].args[0].endswith(".zip")
        assert client.put_file.await_args_list[1].args[0].endswith(".client")
        payload = client.put_file.await_args_list[1].args[1]
        assert b"<string>tail123</string>" in payload

    def test_decrypt_transaction_contents_xml_plain_zip(self, tmp_path: Path) -> None:
        store, _ = _make_store(tmp_path)
        xml = store.decrypt_transaction_contents_xml(  # noqa: SLF001
            encrypted_plist_bytes=b"ignored",
            file_bytes=make_zip(_EMPTY_XML),
        )
        assert "<omnifocus" in xml

    def test_decrypt_transaction_contents_xml_encrypted_zip(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        passphrase = "pw"  # noqa: S105
        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(plaintext, passphrase)
        store, _ = _make_store(tmp_path, passphrase=passphrase)
        xml = store.decrypt_transaction_contents_xml(  # noqa: SLF001
            encrypted_plist_bytes=encrypted_plist,
            file_bytes=encrypted_file,
        )
        assert "<omnifocus" in xml

    def test_decrypt_transaction_contents_xml_encrypted_without_passphrase_raises(
        self, tmp_path: Path
    ) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, encrypted_file = create_encrypted_bundle(plaintext, "pw")
        store, _ = _make_store(tmp_path, passphrase=None)
        with pytest.raises(OFEncryptionError, match="no passphrase is available"):
            store.decrypt_transaction_contents_xml(  # noqa: SLF001
                encrypted_plist_bytes=encrypted_plist,
                file_bytes=encrypted_file,
            )

    def test_decrypt_transaction_contents_xml_unknown_key_slot_raises(self, tmp_path: Path) -> None:
        from omnifocus.crypto.encryption import create_encrypted_bundle, encrypt_file

        plaintext = make_zip(_EMPTY_XML)
        encrypted_plist, _ = create_encrypted_bundle(plaintext, "pw", slot_id=1)
        bad_file = encrypt_file(plaintext, b"A" * 16, b"B" * 16, key_id=99)
        store, _ = _make_store(tmp_path, passphrase="pw")  # noqa: S106
        with pytest.raises(OFEncryptionError, match="Key slot 99 not found"):
            store.decrypt_transaction_contents_xml(  # noqa: SLF001
                encrypted_plist_bytes=encrypted_plist,
                file_bytes=bad_file,
            )

    @pytest.mark.asyncio
    async def test_bundle_state_exposes_remote_clients(self, tmp_path: Path) -> None:
        client_doc = b"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
"http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>clientIdentifier</key><string>client123</string>
<key>hostID</key><string>host-123</string>
<key>name</key><string>air.local</string>
<key>registrationDate</key><date>2026-03-22T12:00:00Z</date>
<key>lastSyncDate</key><date>2026-03-22T12:00:00Z</date>
<key>tailIdentifiers</key><array><string>tail123</string></array>
</dict></plist>"""
        store, client = _make_store(
            tmp_path,
            filenames=[
                "00000000000000=base+tail0.zip",
                "20260322154011=client123.client",
                "delta_transactions.capability",
            ],
        )

        async def get_file(name: str) -> bytes:
            if name.endswith(".client"):
                return client_doc
            return make_zip(_EMPTY_XML)

        client.get_file = AsyncMock(side_effect=get_file)
        result = await store.bundle_state()
        assert result["baseline"]["tail_id"] == "tail0"
        assert result["clients"][0]["client_id"] == "client123"
        assert result["clients"][0]["tail_identifiers"] == ["tail123"]
        assert result["capabilities"] == ["delta_transactions"]


class TestContextManager:
    @pytest.mark.asyncio
    async def test_aclose_called_on_exit(self, tmp_path: Path) -> None:
        store, client = _make_store(tmp_path)
        async with store:
            pass
        client.aclose.assert_called_once()
