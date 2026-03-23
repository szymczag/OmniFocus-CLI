"""Shared pytest fixtures for omnifocus-cli tests.

All fixtures are hermetic: no network I/O, no filesystem writes outside of
``tmp_path``, no OmniFocus app dependency.
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

import io
import zipfile
from pathlib import Path

import pytest

from omnifocus.models import OFModel
from omnifocus.parser import build_model

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ---------------------------------------------------------------------------
# ZIP helpers
# ---------------------------------------------------------------------------


def make_zip(xml_content: str) -> bytes:
    """Wrap *xml_content* in a ``contents.xml`` entry inside an in-memory ZIP.

    This mirrors the structure of real ``.ofocus`` transaction files.

    Args:
        xml_content: UTF-8 XML string to store as ``contents.xml``.

    Returns:
        Raw bytes of the ZIP archive.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("contents.xml", xml_content.encode("utf-8"))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Sample XML fixture (loaded from file)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def sample_xml() -> str:
    """Return the contents of ``tests/fixtures/sample.xml``."""
    return (FIXTURES_DIR / "sample.xml").read_text(encoding="utf-8")


@pytest.fixture(scope="session")
def sample_zip(sample_xml: str) -> bytes:
    """Return a ZIP archive containing the sample ``contents.xml``."""
    return make_zip(sample_xml)


@pytest.fixture(scope="session")
def sample_model(sample_zip: bytes) -> OFModel:
    """Return a fully-parsed :class:`OFModel` from the sample fixture."""
    return build_model(sample_zip)
