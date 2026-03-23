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
When ``OF_CACHE_DIR`` is unset, the default cache location is the repo-local
``.of-cache/`` directory when a project root can be detected; otherwise the
store falls back to ``/tmp/of-cache``.

The cache is reused only when the current remote bundle listing matches the
cached bundle fingerprint exactly. This avoids a 100-200 ms re-parse on every
CLI invocation while still reflecting changes made by OmniFocus on the sync
server.

Security
--------
The cache stores fully-decrypted model data. A repo-local ``.of-cache/``
directory should only be used on a trusted host.

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

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import dataclasses
import json
import logging
import os
import pickle
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omnifocus.crypto.discovery import is_encrypted
from omnifocus.errors import OFEncryptionError, OFError, OFWebDAVError
from omnifocus.models import OFModel, Project, Task
from omnifocus.parser import build_model
from omnifocus.sync.client_state import (
    ClientStateDocument,
    create_client_state_document,
    default_device_name,
    default_host_id,
    parse_client_state_plist,
    serialise_client_state_plist,
)
from omnifocus.sync.protocol import (
    BundleState,
    ClientStateRef,
    build_bundle_state,
    classify_bundle_files,
)
from omnifocus.sync.webdav import WebDAVClient
from omnifocus.writer import (
    AddTaskPlan,
    TaskWriter,
    WritePlan,
    WriterContext,
    generate_id,
)

log = logging.getLogger(__name__)

_CACHE_FILENAME = "of_model.pkl"
_WRITER_STATE_FILENAME = "writer_state.json"
BundleFingerprint = tuple[str, tuple[str, ...], tuple[str, ...]]
DocumentKeys = dict[int, tuple[bytes, bytes]]
WriteStrategy = str
ChainShape = str


def _default_cache_dir() -> Path:
    """Return the default cache directory."""
    env_value = os.environ.get("OF_CACHE_DIR")
    if env_value:
        return Path(env_value)

    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "pyproject.toml").exists() and (source_root / "src" / "omnifocus").exists():
        return source_root / ".of-cache"

    return Path("/tmp/of-cache")  # noqa: S108


@dataclasses.dataclass(frozen=True)
class _CachePayload:
    """Serializable cache payload for a parsed model and bundle fingerprint."""

    model: OFModel
    bundle_fingerprint: BundleFingerprint | None


@dataclasses.dataclass(frozen=True)
class _WriterState:
    """Serializable local writer state used for sync interoperability."""

    client_id: str
    host_id: str
    device_name: str
    registration_date: datetime
    tail_identifiers: tuple[str, ...]
    hardware_cpu_count: str | None
    hardware_cpu_type: str | None
    hardware_cpu_type_name: str | None
    hardware_model: str | None
    os_version: str | None
    os_version_number: str | None
    encrypted: bool
    bundle_fingerprint: BundleFingerprint | None


class OFocusStore:
    """High-level store that syncs, decrypts, parses, and caches the OFModel.

    Args:
        client: A :class:`~omnifocus.sync.webdav.WebDAVClient` instance.
        passphrase: Decryption passphrase, or ``None`` for unencrypted bundles.
        cache_dir: Directory for the pickle cache.  Defaults to
            ``$OF_CACHE_DIR`` or a repo-local ``.of-cache`` directory.
    """

    def __init__(
        self,
        client: WebDAVClient,
        passphrase: str | None = None,
        cache_dir: Path | None = None,
    ) -> None:
        self._client = client
        self._passphrase = passphrase
        self._cache_dir = cache_dir or _default_cache_dir()
        self._cache_path = self._cache_dir / _CACHE_FILENAME
        self._writer_state_path = self._cache_dir / _WRITER_STATE_FILENAME

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
            ``OF_CACHE_DIR`` — cache directory (default repo-local ``.of-cache``).

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
        entries = await self._client.list_entries()
        bundle_fingerprint = _bundle_fingerprint(entries)

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
            filenames=entries,
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
        remote_entries: list[str] | None = None

        if self._cache_path.exists():
            cached = True
            age = time.time() - self._cache_path.stat().st_mtime
            cache_age = round(age, 1)
            ts = datetime.fromtimestamp(self._cache_path.stat().st_mtime, tz=UTC)
            last_synced = ts.isoformat()
            cached_payload = self._load_from_cache()
            if cached_payload is not None and cached_payload.bundle_fingerprint is not None:
                remote_entries = await self._client.list_entries()
                cache_valid = cached_payload.bundle_fingerprint == _bundle_fingerprint(
                    remote_entries
                )

        state = self._load_writer_state()
        current_tail_identifier = None
        if cached and cache_valid:
            try:
                entries = remote_entries or await self._client.list_entries()
                current_tail_identifier = self._current_tail_id(
                    build_bundle_state(entries),
                    await self._load_remote_client_documents(build_bundle_state(entries)),
                )
            except OFError:
                current_tail_identifier = None
        return {
            "last_synced": last_synced,
            "cached": cached,
            "cache_age_seconds": cache_age,
            "cache_valid": cache_valid,
            "bundle_state_version": 2,
            "registered_client": state is not None,
            "tail_identifiers": list(state.tail_identifiers) if state is not None else [],
            "advertised_tail_identifiers": (
                list(state.tail_identifiers) if state is not None else []
            ),
            "client_id": state.client_id if state is not None else None,
            "host_id": state.host_id if state is not None else None,
            "current_tail_identifier": current_tail_identifier,
        }

    async def bundle_state(self) -> dict[str, Any]:
        """Return a parsed debug view of the remote bundle state."""
        state = build_bundle_state(await self._client.list_entries())
        remote_clients = await self._load_remote_client_documents(state)
        return {
            "baseline": {
                "filename": state.baseline.filename,
                "snapshot_id": state.baseline.snapshot_id,
                "tail_id": state.baseline.tail_id,
            },
            "deltas": [
                {
                    "filename": delta.filename,
                    "timestamp": delta.timestamp.isoformat(),
                    "head_id": delta.head_id,
                    "parent_tail_id": delta.parent_tail_id,
                }
                for delta in state.deltas
            ],
            "clients": [
                {
                    "filename": ref.filename,
                    "client_id": ref.client_id,
                    "timestamp": ref.timestamp.isoformat(),
                    "tail_identifiers": (
                        list(remote_clients[ref.client_id].tail_identifiers)
                        if ref.client_id in remote_clients
                        else []
                    ),
                }
                for ref in state.clients
            ],
            "capabilities": list(state.capabilities),
            "other_entries": list(state.other_entries),
        }

    async def fetch_file(self, name: str) -> bytes:
        """Return a raw file from the remote bundle."""
        return await self._client.get_file(name)

    async def fetch_latest_deltas(
        self,
        *,
        count: int = 1,
        client_id: str | None = None,
    ) -> list[tuple[str, bytes]]:
        """Return the newest delta ZIPs, optionally scoped to one client tail."""
        state = build_bundle_state(await self._client.list_entries())
        remote_clients = await self._load_remote_client_documents(state)
        deltas = list(state.deltas)
        if client_id is not None:
            ref = self._latest_client_ref(state, client_id)
            if ref is None:
                raise OFError(f"No client state found for client {client_id!r}")
            document = remote_clients.get(client_id)
            if document is None or not document.tail_identifiers:
                raise OFError(f"Client {client_id!r} has no advertised tail identifier")
            target_tail = document.tail_identifiers[0]
            deltas = [delta for delta in deltas if delta.head_id == target_tail]
            if not deltas:
                raise OFError(
                    f"No delta ZIP found for client {client_id!r} and tail {target_tail!r}"
                )
        selected = deltas[-count:] if count > 0 else []
        return [(delta.filename, await self._client.get_file(delta.filename)) for delta in selected]

    async def fetch_latest_client(
        self,
        *,
        client_id: str | None = None,
    ) -> tuple[str, bytes]:
        """Return the newest client state file, optionally for a specific client."""
        state = build_bundle_state(await self._client.list_entries())
        ref = self._latest_client_ref(state, client_id)
        if ref is None:
            if client_id is None:
                raise OFError("No client state files found in bundle")
            raise OFError(f"No client state found for client {client_id!r}")
        return ref.filename, await self._client.get_file(ref.filename)

    async def decrypt_latest_delta(
        self,
        *,
        client_id: str | None = None,
    ) -> tuple[str, str]:
        """Download, decrypt, and return the newest delta ``contents.xml``."""
        latest = await self.fetch_latest_deltas(count=1, client_id=client_id)
        if not latest:
            raise OFError("No delta ZIPs found in bundle")
        filename, payload = latest[0]
        try:
            encrypted_plist = await self._client.get_file("encrypted")
        except OFWebDAVError as exc:
            if exc.status_code == 404:
                encrypted_plist = b""
            else:
                raise
        return filename, self.decrypt_transaction_contents_xml(
            encrypted_plist_bytes=encrypted_plist,
            file_bytes=payload,
        )

    async def add_task(
        self,
        *,
        name: str,
        parent_task_id: str | None = None,
        inbox: bool = True,
        flagged: bool = False,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        note: str = "",
        estimated_minutes: int | None = None,
        rank: int | None = None,
    ) -> dict[str, str]:
        """Create and upload a task transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        write_strategy = _configured_write_strategy()
        chain_shape = _configured_chain_shape()
        plan = writer.add_task(
            name=name,
            parent_task_id=parent_task_id,
            inbox=inbox,
            flagged=flagged,
            due_dt=due_dt,
            start_dt=start_dt,
            note=note,
            estimated_minutes=estimated_minutes,
            rank=rank,
            write_strategy=write_strategy,
            chain_shape=chain_shape,
        )
        await self._upload_task_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "created", "task_id": plan.task_id, "name": name}

    async def update_task(self, task: Task) -> dict[str, str]:
        """Upload a task upsert transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.update_task(
            task,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "updated", "task_id": task.id, "name": task.name}

    async def complete_task(self, task: Task) -> dict[str, str]:
        """Upload a task completion transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.complete_task(
            task,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "completed", "task_id": task.id, "name": task.name}

    async def drop_task(self, task: Task) -> dict[str, str]:
        """Upload a task drop transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.drop_task(
            task,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "dropped", "task_id": task.id, "name": task.name}

    async def add_project(
        self,
        *,
        name: str,
        folder_id: str | None = None,
        status: str = "active",
        flagged: bool = False,
        due_dt: datetime | None = None,
        start_dt: datetime | None = None,
        note: str = "",
        singleton: bool = False,
        rank: int | None = None,
    ) -> dict[str, str]:
        """Create and upload a project transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.add_project(
            name=name,
            folder_id=folder_id,
            status=status,
            flagged=flagged,
            due_dt=due_dt,
            start_dt=start_dt,
            note=note,
            singleton=singleton,
            rank=rank,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "created", "project_id": plan.project_id, "name": name}

    async def update_project(self, project: Project) -> dict[str, str]:
        """Upload a project upsert transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.update_project(
            project,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "updated", "project_id": project.id, "name": project.name}

    async def complete_project(self, project: Project) -> dict[str, str]:
        """Upload a project completion transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.complete_project(
            project,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "completed", "project_id": project.id, "name": project.name}

    async def drop_project(self, project: Project) -> dict[str, str]:
        """Upload a project drop transaction."""
        writer, encrypted_plist, key_slot, writer_state = await self._prepare_writer()
        plan = writer.drop_project(
            project,
            write_strategy=_configured_write_strategy(),
            chain_shape=_configured_chain_shape(),
        )
        await self._upload_write_plan(
            plan,
            encrypted_plist=encrypted_plist,
            key_slot=key_slot,
            writer_state=writer_state,
        )
        return {"status": "dropped", "project_id": project.id, "name": project.name}

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
            len(model.tasks),
            len(model.projects),
            len(model.folders),
        )
        self._save_to_cache(_CachePayload(model=model, bundle_fingerprint=bundle_fingerprint))
        return model

    async def _prepare_writer(
        self,
    ) -> tuple[TaskWriter, bytes | None, tuple[int, bytes, bytes] | None, _WriterState]:
        """Resolve writer state, bundle tail, and outbound encryption settings."""
        entries = await self._client.list_entries()
        bundle_state = build_bundle_state(entries)
        bundle_fingerprint = _bundle_fingerprint(entries)
        encrypted_plist: bytes | None = None
        key_slot: tuple[int, bytes, bytes] | None = None

        baseline_raw = await self._client.get_file(bundle_state.baseline.filename)
        encrypted = is_encrypted(baseline_raw)

        state = self._load_writer_state()
        remote_clients = await self._load_remote_client_documents(bundle_state)
        current_accepted_tail = self._current_tail_id(bundle_state, remote_clients)
        if current_accepted_tail is None:
            raise OFError("Bundle has no known tail identifier")
        next_tail_id = generate_id()
        template = self._select_client_template(bundle_state, remote_clients)

        if encrypted:
            encrypted_plist = await self._client.get_file("encrypted")
            key_slot = self._load_writable_key_slot(encrypted_plist)

        writer_state = self._build_writer_state(
            previous_state=state,
            template=template,
            accepted_tail=current_accepted_tail,
            encrypted=encrypted,
            bundle_fingerprint=bundle_fingerprint,
        )
        self._save_writer_state(writer_state)
        writer_context = WriterContext(
            os_name=_writer_context_os_name(),
            os_version=writer_state.os_version_number,
            machine_model=writer_state.hardware_model,
        )
        return (
            TaskWriter(
                head_id=next_tail_id,
                parent_tail_id=current_accepted_tail,
                context=writer_context,
            ),
            encrypted_plist,
            key_slot,
            writer_state,
        )

    async def _upload_task_plan(
        self,
        plan: AddTaskPlan,
        *,
        encrypted_plist: bytes | None,
        key_slot: tuple[int, bytes, bytes] | None,
        writer_state: _WriterState,
    ) -> None:
        """Upload a multi-delta task creation plan."""
        if not plan.deltas:
            raise OFError("Task creation plan produced no deltas")

        for delta in plan.deltas:
            await self._upload_transaction(
                delta.filename,
                delta.data,
                encrypted_plist=encrypted_plist,
                key_slot=key_slot,
                writer_state=writer_state,
                upload_client_state=delta.refresh_client_after,
            )

    async def _upload_write_plan(
        self,
        plan: WritePlan,
        *,
        encrypted_plist: bytes | None,
        key_slot: tuple[int, bytes, bytes] | None,
        writer_state: _WriterState,
    ) -> None:
        """Upload a generic write plan."""
        if not plan.deltas:
            raise OFError("Write plan produced no deltas")
        for delta in plan.deltas:
            await self._upload_transaction(
                delta.filename,
                delta.data,
                encrypted_plist=encrypted_plist,
                key_slot=key_slot,
                writer_state=writer_state,
                upload_client_state=delta.refresh_client_after,
            )

    async def _upload_transaction(
        self,
        filename: str,
        payload: bytes,
        *,
        encrypted_plist: bytes | None,
        key_slot: tuple[int, bytes, bytes] | None,
        writer_state: _WriterState,
        upload_client_state: bool = True,
    ) -> None:
        """Upload a delta ZIP and matching ``.client`` state update."""
        upload_bytes = payload
        if encrypted_plist is not None:
            if key_slot is None:
                raise OFEncryptionError("Encrypted bundle has no writable key slot")
            from omnifocus.crypto.encryption import encrypt_file

            slot_id, aes_key, hmac_key = key_slot
            upload_bytes = encrypt_file(payload, aes_key, hmac_key, key_id=slot_id)

        await self._client.put_file(filename, upload_bytes)
        if upload_client_state:
            client_filename, client_payload = self._build_client_state_payload(
                writer_state, filename
            )
            await self._client.put_file(client_filename, client_payload)
        self.invalidate_cache()

        self._save_writer_state(
            _WriterState(
                client_id=writer_state.client_id,
                host_id=writer_state.host_id,
                device_name=writer_state.device_name,
                registration_date=writer_state.registration_date,
                tail_identifiers=writer_state.tail_identifiers,
                hardware_cpu_count=writer_state.hardware_cpu_count,
                hardware_cpu_type=writer_state.hardware_cpu_type,
                hardware_cpu_type_name=writer_state.hardware_cpu_type_name,
                hardware_model=writer_state.hardware_model,
                os_version=writer_state.os_version,
                os_version_number=writer_state.os_version_number,
                encrypted=encrypted_plist is not None,
                bundle_fingerprint=None,
            )
        )

    async def _load_remote_client_documents(
        self, state: BundleState
    ) -> dict[str, ClientStateDocument]:
        """Download and parse remote ``.client`` plist documents."""
        documents: dict[str, ClientStateDocument] = {}
        for ref in state.clients:
            data = await self._client.get_file(ref.filename)
            documents[ref.client_id] = parse_client_state_plist(data)
        return documents

    def _select_client_template(
        self,
        state: BundleState,
        remote_clients: dict[str, ClientStateDocument],
    ) -> ClientStateDocument | None:
        """Return the latest remote client document as a template, if available."""
        for ref in reversed(state.clients):
            if ref.client_id in remote_clients:
                return remote_clients[ref.client_id]
        return None

    def _latest_client_ref(
        self,
        state: BundleState,
        client_id: str | None,
    ) -> ClientStateRef | None:
        """Return the newest client state ref, optionally scoped by client id."""
        refs = list(state.clients)
        if client_id is not None:
            refs = [ref for ref in refs if ref.client_id == client_id]
        return refs[-1] if refs else None

    def _current_tail_id(
        self,
        state: BundleState,
        remote_clients: dict[str, ClientStateDocument],
    ) -> str | None:
        """Return the best-known current tail identifier."""
        for ref in reversed(state.clients):
            document = remote_clients.get(ref.client_id)
            if document is not None and document.tail_identifiers:
                return document.tail_identifiers[0]
        if state.deltas:
            return state.deltas[-1].head_id
        if state.baseline.tail_id:
            return state.baseline.tail_id
        return None

    def _build_writer_state(
        self,
        *,
        previous_state: _WriterState | None,
        template: ClientStateDocument | None,
        accepted_tail: str,
        encrypted: bool,
        bundle_fingerprint: BundleFingerprint,
    ) -> _WriterState:
        """Build a stable local writer state from local state or a remote template."""
        if previous_state is not None:
            return dataclasses.replace(
                previous_state,
                client_id=_configured_client_id(previous_state.client_id),
                host_id=_configured_host_id(previous_state.host_id),
                device_name=_configured_device_name(previous_state.device_name),
                tail_identifiers=(accepted_tail,),
                hardware_cpu_count=_configured_optional_env(
                    "OF_DEVICE_HARDWARE_CPU_COUNT", previous_state.hardware_cpu_count
                ),
                hardware_cpu_type=_configured_optional_env(
                    "OF_DEVICE_HARDWARE_CPU_TYPE", previous_state.hardware_cpu_type
                ),
                hardware_cpu_type_name=_configured_optional_env(
                    "OF_DEVICE_HARDWARE_CPU_TYPE_NAME", previous_state.hardware_cpu_type_name
                ),
                hardware_model=_configured_optional_env(
                    "OF_DEVICE_HARDWARE_MODEL", previous_state.hardware_model
                ),
                os_version=_configured_optional_env(
                    "OF_DEVICE_OS_VERSION", previous_state.os_version
                ),
                os_version_number=_configured_optional_env(
                    "OF_DEVICE_OS_VERSION_NUMBER", previous_state.os_version_number
                ),
                encrypted=encrypted,
                bundle_fingerprint=bundle_fingerprint,
            )

        client_id = _configured_client_id()
        current = datetime.now(UTC)
        device_name = _configured_device_name()
        host_id = _configured_host_id()
        document = create_client_state_document(
            client_identifier=client_id,
            tail_identifiers=(accepted_tail,),
            template=template,
            now=current,
            device_name=device_name,
            host_id=host_id,
            registration_date=current,
        )
        document = dataclasses.replace(
            document,
            host_id=host_id,
            name=device_name,
            hardware_cpu_count=os.environ.get("OF_DEVICE_HARDWARE_CPU_COUNT"),
            hardware_cpu_type=os.environ.get("OF_DEVICE_HARDWARE_CPU_TYPE"),
            hardware_cpu_type_name=os.environ.get("OF_DEVICE_HARDWARE_CPU_TYPE_NAME"),
            hardware_model=os.environ.get("OF_DEVICE_HARDWARE_MODEL"),
            os_version=os.environ.get("OF_DEVICE_OS_VERSION"),
            os_version_number=os.environ.get("OF_DEVICE_OS_VERSION_NUMBER"),
        )
        return _WriterState(
            client_id=document.client_identifier,
            host_id=document.host_id,
            device_name=document.name,
            registration_date=document.registration_date,
            tail_identifiers=document.tail_identifiers,
            hardware_cpu_count=document.hardware_cpu_count,
            hardware_cpu_type=document.hardware_cpu_type,
            hardware_cpu_type_name=document.hardware_cpu_type_name,
            hardware_model=document.hardware_model,
            os_version=document.os_version,
            os_version_number=document.os_version_number,
            encrypted=encrypted,
            bundle_fingerprint=bundle_fingerprint,
        )

    def _build_client_state_payload(
        self,
        writer_state: _WriterState,
        transaction_filename: str,
    ) -> tuple[str, bytes]:
        """Return the updated ``.client`` filename and plist payload."""
        timestamp_raw = transaction_filename.split("=", 1)[0]
        current = datetime.strptime(timestamp_raw, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        document = create_client_state_document(
            client_identifier=writer_state.client_id,
            tail_identifiers=writer_state.tail_identifiers,
            now=current,
            device_name=writer_state.device_name,
            host_id=writer_state.host_id,
            registration_date=writer_state.registration_date,
        )
        document = dataclasses.replace(
            document,
            hardware_cpu_count=writer_state.hardware_cpu_count,
            hardware_cpu_type=writer_state.hardware_cpu_type,
            hardware_cpu_type_name=writer_state.hardware_cpu_type_name,
            hardware_model=writer_state.hardware_model,
            os_version=writer_state.os_version,
            os_version_number=writer_state.os_version_number,
        )
        return (
            f"{timestamp_raw}={writer_state.client_id}.client",
            serialise_client_state_plist(document),
        )

    def decrypt_transaction_contents_xml(
        self,
        *,
        encrypted_plist_bytes: bytes,
        file_bytes: bytes,
    ) -> str:
        """Return ``contents.xml`` from a plaintext or encrypted transaction ZIP."""
        from io import BytesIO
        from zipfile import ZipFile

        if not is_encrypted(file_bytes):
            plaintext = file_bytes
        else:
            if self._passphrase is None:
                raise OFEncryptionError(
                    "Bundle is encrypted but no passphrase is available. "
                    "Set OF_ENCRYPTION_PASSPHRASE (or use the WebDAV password "
                    "as a linked passphrase via OF_WEBDAV_PASS / URL-embedded credentials)."
                )
            from omnifocus.crypto.discovery import parse_file_header
            from omnifocus.crypto.encryption import decrypt_file, load_document_keys

            keys = load_document_keys(self._passphrase, encrypted_plist_bytes)
            key_id, _ = parse_file_header(file_bytes)
            if key_id not in keys:
                raise OFEncryptionError(
                    f"Key slot {key_id} not found in document keys "
                    f"(available slots: {sorted(keys.keys())})"
                )
            plaintext = decrypt_file(file_bytes, *keys[key_id])

        with ZipFile(BytesIO(plaintext)) as archive:
            return archive.read("contents.xml").decode("utf-8")

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

    def _load_writable_key_slot(self, encrypted_plist: bytes) -> tuple[int, bytes, bytes]:
        """Return the active writable key slot for outbound encryption."""
        if self._passphrase is None:
            raise OFEncryptionError(
                "Bundle is encrypted but no passphrase is available. "
                "Set OF_ENCRYPTION_PASSPHRASE (or use the WebDAV password "
                "as a linked passphrase via OF_WEBDAV_PASS / URL-embedded credentials)."
            )
        from omnifocus.crypto.encryption import load_writable_document_key

        return load_writable_document_key(self._passphrase, encrypted_plist)

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

    def _load_writer_state(self) -> _WriterState | None:
        """Return the cached writer state if present and valid."""
        if not self._writer_state_path.exists():
            return None
        try:
            raw = json.loads(self._writer_state_path.read_text())
        except (OSError, ValueError):
            return None
        fingerprint_raw = raw.get("bundle_fingerprint")
        bundle_fingerprint: BundleFingerprint | None = None
        if (
            isinstance(fingerprint_raw, list)
            and len(fingerprint_raw) == 3
            and isinstance(fingerprint_raw[0], str)
            and isinstance(fingerprint_raw[1], list)
            and isinstance(fingerprint_raw[2], list)
        ):
            bundle_fingerprint = (
                fingerprint_raw[0],
                tuple(str(name) for name in fingerprint_raw[1]),
                tuple(str(name) for name in fingerprint_raw[2]),
            )
        client_id = raw.get("client_id")
        host_id = raw.get("host_id")
        device_name = raw.get("device_name")
        registration_date_raw = raw.get("registration_date")
        tail_identifiers_raw = raw.get("tail_identifiers")
        hardware_cpu_count = raw.get("hardware_cpu_count")
        hardware_cpu_type = raw.get("hardware_cpu_type")
        hardware_cpu_type_name = raw.get("hardware_cpu_type_name")
        hardware_model = raw.get("hardware_model")
        os_version = raw.get("os_version")
        os_version_number = raw.get("os_version_number")
        encrypted = raw.get("encrypted")
        if (
            not isinstance(client_id, str)
            or not isinstance(host_id, str)
            or not isinstance(device_name, str)
            or not isinstance(registration_date_raw, str)
        ):
            return None
        for value in (
            hardware_cpu_count,
            hardware_cpu_type,
            hardware_cpu_type_name,
            hardware_model,
            os_version,
            os_version_number,
        ):
            if value is not None and not isinstance(value, str):
                return None
        if not isinstance(tail_identifiers_raw, list) or not all(
            isinstance(item, str) for item in tail_identifiers_raw
        ):
            return None
        if not isinstance(encrypted, bool):
            return None
        try:
            registration_date = datetime.fromisoformat(registration_date_raw)
        except ValueError:
            return None
        return _WriterState(
            client_id=client_id,
            host_id=host_id,
            device_name=device_name,
            registration_date=registration_date,
            tail_identifiers=tuple(tail_identifiers_raw),
            hardware_cpu_count=hardware_cpu_count,
            hardware_cpu_type=hardware_cpu_type,
            hardware_cpu_type_name=hardware_cpu_type_name,
            hardware_model=hardware_model,
            os_version=os_version,
            os_version_number=os_version_number,
            encrypted=encrypted,
            bundle_fingerprint=bundle_fingerprint,
        )

    def _save_writer_state(self, state: _WriterState) -> None:
        """Persist writer state for future sync writes."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "client_id": state.client_id,
            "host_id": state.host_id,
            "device_name": state.device_name,
            "registration_date": state.registration_date.isoformat(),
            "tail_identifiers": list(state.tail_identifiers),
            "hardware_cpu_count": state.hardware_cpu_count,
            "hardware_cpu_type": state.hardware_cpu_type,
            "hardware_cpu_type_name": state.hardware_cpu_type_name,
            "hardware_model": state.hardware_model,
            "os_version": state.os_version,
            "os_version_number": state.os_version_number,
            "encrypted": state.encrypted,
            "bundle_fingerprint": (
                [
                    state.bundle_fingerprint[0],
                    list(state.bundle_fingerprint[1]),
                    list(state.bundle_fingerprint[2]),
                ]
                if state.bundle_fingerprint is not None
                else None
            ),
        }
        self._writer_state_path.write_text(json.dumps(payload))


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
    state = build_bundle_state(filenames)
    return (
        state.baseline.filename,
        tuple(delta.filename for delta in state.deltas),
        tuple(client.filename for client in state.clients),
    )


def _configured_client_id(current: str | None = None) -> str:
    """Return the configured or current sync client identifier."""
    return os.environ.get("OF_CLIENT_ID") or current or generate_id()


def _configured_device_name(current: str | None = None) -> str:
    """Return the configured or current device name."""
    return os.environ.get("OF_DEVICE_NAME") or current or default_device_name()


def _configured_host_id(current: str | None = None) -> str:
    """Return the configured or current device host identifier."""
    return os.environ.get("OF_DEVICE_HOST_ID") or current or default_host_id()


def _configured_optional_env(name: str, current: str | None) -> str | None:
    """Return an optional environment override or keep the current value."""
    return os.environ.get(name) or current


def _writer_context_os_name() -> str | None:
    """Return the OS name to embed in transaction XML."""
    return os.environ.get("OF_DEVICE_OS_NAME", "macOS")


def _configured_write_strategy() -> WriteStrategy:
    """Return the experimental write strategy for task creation chains."""
    value = os.environ.get("OF_WRITE_STRATEGY", "client_after_each_delta")
    if value not in {"chain_then_client", "client_after_each_delta"}:
        raise OFError(
            "Invalid OF_WRITE_STRATEGY. Use 'chain_then_client' or 'client_after_each_delta'."
        )
    return value


def _configured_chain_shape() -> ChainShape:
    """Return the experimental chain shape for task creation deltas."""
    value = os.environ.get("OF_CHAIN_SHAPE", "app_rebase")
    if value not in {"linear", "app_rebase"}:
        raise OFError("Invalid OF_CHAIN_SHAPE. Use 'linear' or 'app_rebase'.")
    return value
