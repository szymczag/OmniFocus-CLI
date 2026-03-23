"""Tests for :mod:`omnifocus.sync.client_state`."""

from __future__ import annotations

import plistlib
from datetime import UTC, datetime

import pytest

from omnifocus.errors import OFError
from omnifocus.sync.client_state import (
    ClientStateDocument,
    create_client_state_document,
    default_device_name,
    default_host_id,
    parse_client_state_plist,
    serialise_client_state_plist,
)

NOW = datetime(2026, 3, 23, 12, 0, 0, tzinfo=UTC)


def _make_payload() -> bytes:
    """Build a valid client-state plist payload."""
    return plistlib.dumps(
        {
            "clientIdentifier": "client123",
            "hostID": "ED325E58-F612-4653-BD34-7006A7D6DD52",
            "name": "air.local",
            "registrationDate": NOW,
            "lastSyncDate": NOW,
            "tailIdentifiers": ["tail123"],
            "HardwareCPUCount": "10",
            "HardwareCPUType": "16777228,0",
            "HardwareCPUTypeName": "arm64",
            "HardwareModel": "Mac16,12",
            "OSVersion": "25D2128",
            "OSVersionNumber": "26.3.1",
        },
        fmt=plistlib.FMT_XML,
        sort_keys=False,
    )


class TestParseClientStatePlist:
    def test_parses_valid_document(self) -> None:
        document = parse_client_state_plist(_make_payload())
        assert document.client_identifier == "client123"
        assert document.tail_identifiers == ("tail123",)
        assert document.hardware_model == "Mac16,12"
        assert document.hardware_cpu_count == "10"
        assert document.os_version_number == "26.3.1"

    def test_invalid_plist_raises(self) -> None:
        with pytest.raises(OFError, match="Failed to parse .client plist"):
            parse_client_state_plist(b"not plist")

    def test_non_dict_root_raises(self) -> None:
        payload = plistlib.dumps(["wrong"], fmt=plistlib.FMT_XML)
        with pytest.raises(OFError, match="dictionary root"):
            parse_client_state_plist(payload)

    @pytest.mark.parametrize(
        ("field", "value", "message"),
        [
            ("clientIdentifier", 123, "must be a string"),
            ("registrationDate", "bad", "must be a datetime"),
            ("tailIdentifiers", [123], "must be a list of strings"),
            ("OFMSyncClientSupportedCapabilities", [123], "must be a list of strings"),
            ("HardwareModel", 123, "must be a string"),
        ],
    )
    def test_invalid_fields_raise(self, field: str, value: object, message: str) -> None:
        payload = plistlib.loads(_make_payload())
        payload[field] = value
        with pytest.raises(OFError, match=message):
            parse_client_state_plist(plistlib.dumps(payload, fmt=plistlib.FMT_XML))


class TestCreateClientStateDocument:
    def test_creates_default_document(self) -> None:
        document = create_client_state_document(
            client_identifier="client123",
            tail_identifiers=("tail123",),
            now=NOW,
            device_name="air.local",
            host_id="host-123",
            registration_date=NOW,
        )
        assert document.client_identifier == "client123"
        assert document.host_id == "host-123"
        assert document.name == "air.local"
        assert document.last_sync_date == NOW
        assert document.host_id == "host-123"

    def test_clones_template(self) -> None:
        template = ClientStateDocument(
            client_identifier="template",
            host_id="host-template",
            name="template.local",
            registration_date=NOW,
            last_sync_date=NOW,
            tail_identifiers=("old-tail",),
            hardware_model="Mac16,12",
            os_version_number="26.3.1",
            extras={"ExtraField": "x"},
        )
        document = create_client_state_document(
            client_identifier="client123",
            tail_identifiers=("tail123",),
            template=template,
            now=NOW,
        )
        assert document.client_identifier == "client123"
        assert document.host_id == "host-template"
        assert document.name == "template.local"
        assert document.registration_date == NOW
        assert document.hardware_model == "Mac16,12"
        assert document.os_version_number == "26.3.1"
        assert document.extras == {"ExtraField": "x"}

    def test_default_host_id_uses_uuid_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("OF_DEVICE_HOST_ID", raising=False)
        document = create_client_state_document(
            client_identifier="client123",
            tail_identifiers=("tail123",),
            now=NOW,
            device_name="air.local",
            registration_date=NOW,
        )
        assert len(document.host_id) == 36
        assert document.host_id == document.host_id.upper()
        assert document.host_id.count("-") == 4

    def test_default_device_name_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_DEVICE_NAME", "cli-host.local")
        document = create_client_state_document(
            client_identifier="client123",
            tail_identifiers=("tail123",),
            now=NOW,
            registration_date=NOW,
        )
        assert document.name == "cli-host.local"

    def test_default_host_id_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_DEVICE_HOST_ID", "EXPLICIT-HOST-ID")
        document = create_client_state_document(
            client_identifier="client123",
            tail_identifiers=("tail123",),
            now=NOW,
            registration_date=NOW,
        )
        assert document.host_id == "EXPLICIT-HOST-ID"

    def test_default_device_name_helper_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_DEVICE_NAME", "cli-host.local")
        assert default_device_name() == "cli-host.local"

    def test_default_host_id_helper_uses_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("OF_DEVICE_HOST_ID", "EXPLICIT-HOST-ID")
        assert default_host_id() == "EXPLICIT-HOST-ID"


class TestSerialiseClientStatePlist:
    def test_round_trips_document(self) -> None:
        document = ClientStateDocument(
            client_identifier="client123",
            host_id="ED325E58-F612-4653-BD34-7006A7D6DD52",
            name="air.local",
            registration_date=NOW,
            last_sync_date=NOW,
            tail_identifiers=("tail123",),
            hardware_model="Mac16,12",
            hardware_cpu_count="10",
            hardware_cpu_type="16777228,0",
            hardware_cpu_type_name="arm64",
            os_version="25D2128",
            os_version_number="26.3.1",
            extras={"ExtraField": "x"},
        )
        payload = serialise_client_state_plist(document)
        parsed = parse_client_state_plist(payload)
        assert parsed.client_identifier == "client123"
        assert parsed.hardware_model == "Mac16,12"
        assert parsed.hardware_cpu_type_name == "arm64"
        assert parsed.os_version == "25D2128"
        assert parsed.extras["ExtraField"] == "x"

    def test_naive_datetimes_are_coerced_to_utc(self) -> None:
        document = ClientStateDocument(
            client_identifier="client123",
            host_id="host-123",
            name="air.local",
            registration_date=datetime(2026, 3, 23, 12, 0, 0),
            last_sync_date=datetime(2026, 3, 23, 12, 0, 0),
            tail_identifiers=("tail123",),
        )
        parsed = parse_client_state_plist(serialise_client_state_plist(document))
        assert parsed.registration_date.tzinfo == UTC
        assert parsed.last_sync_date.tzinfo == UTC
