"""OmniFocus sync protocol helpers.

Provides functions to classify and sort the ZIP files returned by
:meth:`~omnifocus.sync.webdav.WebDAVClient.list_bundle` into:

- A single **baseline** file (``00000000000000=*.zip``)
- Zero or more **transaction** files (all other ``.zip`` files)

Transaction filenames encode a UTC timestamp which determines the order in
which they must be applied during merge::

    20260322T154011=<clientID>+<parentID>.zip

The part before the ``=`` is parsed as ``YYYYMMDDTHHMMSS``.

Usage::

    from omnifocus.sync.protocol import classify_bundle_files

    filenames = await client.list_bundle()
    baseline, transactions = classify_bundle_files(filenames)
"""

from __future__ import annotations

from dataclasses import dataclass

from omnifocus.errors import OFBundleNotFound

# Prefix that identifies the baseline ZIP
_BASELINE_PREFIX = "00000000000000="


@dataclass(frozen=True)
class TransactionRef:
    """Parsed metadata for a single transaction filename.

    Attributes:
        filename: Original transaction filename.
        client_id: Client identifier stored before the ``+`` separator.
        parent_id: Predecessor identifier stored after the ``+`` separator.
    """

    filename: str
    client_id: str
    parent_id: str


def classify_bundle_files(filenames: list[str]) -> tuple[str, list[str]]:
    """Separate the baseline ZIP from transaction ZIPs.

    Args:
        filenames: List of ``.zip`` filenames as returned by
            :meth:`~omnifocus.sync.webdav.WebDAVClient.list_bundle`.

    Returns:
        A tuple ``(baseline_filename, sorted_transaction_filenames)`` where
        transactions are sorted lexicographically (== chronologically).

    Raises:
        OFBundleNotFound: If no baseline file is found in *filenames*.
    """
    baseline: str | None = None
    transactions: list[str] = []

    for name in filenames:
        if name.startswith(_BASELINE_PREFIX):
            baseline = name
        else:
            transactions.append(name)

    if baseline is None:
        raise OFBundleNotFound(
            "No baseline ZIP found in the .ofocus bundle. "
            f"Expected a file starting with '{_BASELINE_PREFIX}'. "
            f"Found: {filenames!r}"
        )

    return baseline, sorted(transactions)


def is_baseline(filename: str) -> bool:
    """Return ``True`` if *filename* is a baseline ZIP.

    Args:
        filename: A ``.zip`` filename from the bundle directory.
    """
    return filename.startswith(_BASELINE_PREFIX)


def client_id_from_filename(filename: str) -> str | None:
    """Extract the client identifier from a transaction or baseline filename.

    The OmniFocus filename format is::

        <timestamp>=<clientID>+<parentID>.zip

    Args:
        filename: A ``.zip`` filename.

    Returns:
        The client ID string, or ``None`` if the filename does not match the
        expected format.
    """
    stem = filename.removesuffix(".zip")
    if "=" not in stem:
        return None
    after_eq = stem.split("=", 1)[1]
    if "+" in after_eq:
        return after_eq.split("+", 1)[0]
    return after_eq or None


def parent_id_from_filename(filename: str) -> str | None:
    """Extract the predecessor identifier from a transaction filename.

    Args:
        filename: A ``.zip`` filename.

    Returns:
        The parent identifier string, or ``None`` if the filename does not
        match the expected transaction format.
    """
    stem = filename.removesuffix(".zip")
    if "=" not in stem:
        return None
    after_eq = stem.split("=", 1)[1]
    if "+" not in after_eq:
        return None
    parent_id = after_eq.split("+", 1)[1]
    return parent_id or None


def parse_transaction_filename(filename: str) -> TransactionRef | None:
    """Parse a transaction filename into its client and predecessor parts."""
    client_id = client_id_from_filename(filename)
    parent_id = parent_id_from_filename(filename)
    if client_id is None or parent_id is None:
        return None
    return TransactionRef(filename=filename, client_id=client_id, parent_id=parent_id)


def latest_transaction_ref(filenames: list[str]) -> TransactionRef | None:
    """Return metadata for the latest transaction in the bundle listing."""
    _, tx_names = classify_bundle_files(filenames)
    if not tx_names:
        return None
    return parse_transaction_filename(tx_names[-1])
