"""Parsing and serializing OmniFocus ``.client`` state files."""

from __future__ import annotations

import os
import plistlib
import socket
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from omnifocus.errors import OFError

_DEFAULT_XML_CAPABILITIES = (
    "stable_repeats",
    "external_attachments",
    "v4_7_features",
    "floating_time_zones",
    "unknown_element_import",
    "versioned_perspectives",
    "delta_transactions",
    "active_object_hidden_dates",
)
_DEFAULT_OFM_CAPABILITIES = ("delta_transactions",)


@dataclass(frozen=True)
class ClientStateDocument:
    """Structured representation of an OmniFocus ``.client`` plist."""

    client_identifier: str
    host_id: str
    name: str
    registration_date: datetime
    last_sync_date: datetime
    tail_identifiers: tuple[str, ...]
    bundle_identifier: str = "com.omnigroup.OmniFocus4"
    bundle_version: str = "185.9.1"
    application_marketing_version: str = "4.8.8"
    current_framework_version: str = "2"
    ofm_sync_client_model_version: str = "6.0.18"
    ofm_sync_client_supported_capabilities: tuple[str, ...] = _DEFAULT_OFM_CAPABILITIES
    xml_sync_client_supported_capabilities: tuple[str, ...] = _DEFAULT_XML_CAPABILITIES
    hardware_cpu_count: str | None = None
    hardware_cpu_type: str | None = None
    hardware_cpu_type_name: str | None = None
    hardware_model: str | None = None
    os_version: str | None = None
    os_version_number: str | None = None
    extras: dict[str, object] = field(default_factory=dict)


def parse_client_state_plist(data: bytes) -> ClientStateDocument:
    """Parse a remote ``.client`` plist into a structured document."""
    try:
        payload = plistlib.loads(data)
    except Exception as exc:
        raise OFError(f"Failed to parse .client plist: {exc}") from exc
    if not isinstance(payload, dict):
        raise OFError("Client state plist must contain a dictionary root")

    client_identifier = _require_str(payload, "clientIdentifier")
    host_id = _require_str(payload, "hostID")
    name = _require_str(payload, "name")
    registration_date = _require_datetime(payload, "registrationDate")
    last_sync_date = _require_datetime(payload, "lastSyncDate")
    tail_identifiers = _require_str_tuple(payload, "tailIdentifiers")

    extras = {
        key: value
        for key, value in payload.items()
        if key
        not in {
            "clientIdentifier",
            "hostID",
            "name",
            "registrationDate",
            "lastSyncDate",
            "tailIdentifiers",
            "bundleIdentifier",
            "bundleVersion",
            "ApplicationMarketingVersion",
            "CurrentFrameworkVersion",
            "OFMSyncClientModelVersion",
            "OFMSyncClientSupportedCapabilities",
            "XMLSyncClientSupportedCapabilities",
            "HardwareCPUCount",
            "HardwareCPUType",
            "HardwareCPUTypeName",
            "HardwareModel",
            "OSVersion",
            "OSVersionNumber",
        }
    }

    return ClientStateDocument(
        client_identifier=client_identifier,
        host_id=host_id,
        name=name,
        registration_date=registration_date,
        last_sync_date=last_sync_date,
        tail_identifiers=tail_identifiers,
        bundle_identifier=str(payload.get("bundleIdentifier", "com.omnigroup.OmniFocus4")),
        bundle_version=str(payload.get("bundleVersion", "185.9.1")),
        application_marketing_version=str(payload.get("ApplicationMarketingVersion", "4.8.8")),
        current_framework_version=str(payload.get("CurrentFrameworkVersion", "2")),
        ofm_sync_client_model_version=str(payload.get("OFMSyncClientModelVersion", "6.0.18")),
        ofm_sync_client_supported_capabilities=_optional_str_tuple(
            payload,
            "OFMSyncClientSupportedCapabilities",
            _DEFAULT_OFM_CAPABILITIES,
        ),
        xml_sync_client_supported_capabilities=_optional_str_tuple(
            payload,
            "XMLSyncClientSupportedCapabilities",
            _DEFAULT_XML_CAPABILITIES,
        ),
        hardware_cpu_count=_optional_str(payload, "HardwareCPUCount"),
        hardware_cpu_type=_optional_str(payload, "HardwareCPUType"),
        hardware_cpu_type_name=_optional_str(payload, "HardwareCPUTypeName"),
        hardware_model=_optional_str(payload, "HardwareModel"),
        os_version=_optional_str(payload, "OSVersion"),
        os_version_number=_optional_str(payload, "OSVersionNumber"),
        extras=extras,
    )


def create_client_state_document(
    *,
    client_identifier: str,
    tail_identifiers: tuple[str, ...],
    template: ClientStateDocument | None = None,
    now: datetime | None = None,
    device_name: str | None = None,
    host_id: str | None = None,
    registration_date: datetime | None = None,
) -> ClientStateDocument:
    """Create a local ``.client`` document, optionally cloning a template."""
    current = now or datetime.now(UTC)
    if template is not None:
        return ClientStateDocument(
            client_identifier=client_identifier,
            host_id=host_id or template.host_id,
            name=device_name or template.name,
            registration_date=registration_date or template.registration_date,
            last_sync_date=current,
            tail_identifiers=tail_identifiers,
            bundle_identifier=template.bundle_identifier,
            bundle_version=template.bundle_version,
            application_marketing_version=template.application_marketing_version,
            current_framework_version=template.current_framework_version,
            ofm_sync_client_model_version=template.ofm_sync_client_model_version,
            ofm_sync_client_supported_capabilities=template.ofm_sync_client_supported_capabilities,
            xml_sync_client_supported_capabilities=template.xml_sync_client_supported_capabilities,
            hardware_cpu_count=template.hardware_cpu_count,
            hardware_cpu_type=template.hardware_cpu_type,
            hardware_cpu_type_name=template.hardware_cpu_type_name,
            hardware_model=template.hardware_model,
            os_version=template.os_version,
            os_version_number=template.os_version_number,
            extras=dict(template.extras),
        )

    return ClientStateDocument(
        client_identifier=client_identifier,
        host_id=host_id or default_host_id(),
        name=device_name or default_device_name(),
        registration_date=registration_date or current,
        last_sync_date=current,
        tail_identifiers=tail_identifiers,
        hardware_cpu_count=os.environ.get("OF_DEVICE_HARDWARE_CPU_COUNT"),
        hardware_cpu_type=os.environ.get("OF_DEVICE_HARDWARE_CPU_TYPE"),
        hardware_cpu_type_name=os.environ.get("OF_DEVICE_HARDWARE_CPU_TYPE_NAME"),
        hardware_model=os.environ.get("OF_DEVICE_HARDWARE_MODEL"),
        os_version=os.environ.get("OF_DEVICE_OS_VERSION"),
        os_version_number=os.environ.get("OF_DEVICE_OS_VERSION_NUMBER"),
    )


def serialise_client_state_plist(document: ClientStateDocument) -> bytes:
    """Serialise a client state document to XML plist bytes."""
    payload: dict[str, Any] = dict(document.extras)
    payload.update(
        {
            "ApplicationMarketingVersion": document.application_marketing_version,
            "bundleIdentifier": document.bundle_identifier,
            "bundleVersion": document.bundle_version,
            "clientIdentifier": document.client_identifier,
            "CurrentFrameworkVersion": document.current_framework_version,
            "hostID": document.host_id,
            "lastSyncDate": _coerce_utc(document.last_sync_date),
            "name": document.name,
            "OFMSyncClientModelVersion": document.ofm_sync_client_model_version,
            "OFMSyncClientSupportedCapabilities": list(
                document.ofm_sync_client_supported_capabilities
            ),
            "registrationDate": _coerce_utc(document.registration_date),
            "tailIdentifiers": list(document.tail_identifiers),
            "XMLSyncClientSupportedCapabilities": list(
                document.xml_sync_client_supported_capabilities
            ),
        }
    )
    if document.hardware_cpu_count is not None:
        payload["HardwareCPUCount"] = document.hardware_cpu_count
    if document.hardware_cpu_type is not None:
        payload["HardwareCPUType"] = document.hardware_cpu_type
    if document.hardware_cpu_type_name is not None:
        payload["HardwareCPUTypeName"] = document.hardware_cpu_type_name
    if document.hardware_model is not None:
        payload["HardwareModel"] = document.hardware_model
    if document.os_version is not None:
        payload["OSVersion"] = document.os_version
    if document.os_version_number is not None:
        payload["OSVersionNumber"] = document.os_version_number
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False)


def _coerce_utc(value: datetime) -> datetime:
    """Return a UTC-aware datetime."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _require_str(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise OFError(f"Client state field {key!r} must be a string")
    return value


def _require_datetime(payload: dict[str, object], key: str) -> datetime:
    value = payload.get(key)
    if not isinstance(value, datetime):
        raise OFError(f"Client state field {key!r} must be a datetime")
    return _coerce_utc(value)


def _require_str_tuple(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OFError(f"Client state field {key!r} must be a list of strings")
    return tuple(value)


def _optional_str_tuple(
    payload: dict[str, object],
    key: str,
    default: tuple[str, ...],
) -> tuple[str, ...]:
    value = payload.get(key)
    if value is None:
        return default
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise OFError(f"Client state field {key!r} must be a list of strings")
    return tuple(value)


def _optional_str(payload: dict[str, object], key: str) -> str | None:
    """Return an optional string field from the payload."""
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise OFError(f"Client state field {key!r} must be a string")
    return value


def default_device_name() -> str:
    """Return a stable default device name for local writer registration."""
    explicit = os.environ.get("OF_DEVICE_NAME")
    if explicit:
        return explicit
    hostname = socket.gethostname().split(".", 1)[0]
    return f"{hostname}.local"


def default_host_id() -> str:
    """Return a stable-shape host identifier."""
    explicit = os.environ.get("OF_DEVICE_HOST_ID")
    if explicit:
        return explicit
    return str(uuid.uuid4()).upper()
