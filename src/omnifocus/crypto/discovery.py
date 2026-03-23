"""OmniFocus OmniFileEncryption v2 format detection.

OmniFocus uses the *OmniFileEncryption* segmented encryption scheme.  Each
encrypted file starts with a 20-byte magic string followed by a small per-file
header that identifies which key slot to use.  The actual key material lives in
a separate ``encrypted`` plist file stored in the ``.ofocus`` bundle root.

Per-file header layout
----------------------

.. code-block:: text

    Offset  Size  Description
    ------  ----  -----------
         0    20  Magic: b"OmniFileEncryption\\x00\\x00"
        20     2  info_length: uint16 BE — byte count of the key-info section
        22     2  key_id: uint16 BE — selects a slot from the document plist
        24     *  Per-file key material (0 bytes for AES_CTR_HMAC keys)
        ?      *  Zero padding to the next 16-byte boundary

After the header, the file contains one or more encrypted segments followed by
a 32-byte file HMAC.  See :mod:`omnifocus.crypto.encryption` for the segment
format and decryption logic.

The ``encrypted`` plist format and the ``load_document_keys`` function for
deriving AES and HMAC keys from a passphrase are in
:mod:`omnifocus.crypto.encryption`.
"""

from __future__ import annotations

__author__ = "Maciej Szymczak <maciej@szymczak.at>"

from omnifocus.errors import OFEncryptionError

# ---- Constants -----------------------------------------------------------

MAGIC = b"OmniFileEncryption\x00\x00"
MAGIC_LEN = len(MAGIC)  # 20

# Minimum bytes needed to read the fixed header fields (magic + info_length + key_id)
_MIN_HEADER_LEN = MAGIC_LEN + 4


# ---- Public API ----------------------------------------------------------


def is_encrypted(data: bytes) -> bool:
    """Return ``True`` if *data* looks like an OmniFileEncryption file.

    This is a fast, non-throwing check based on the magic bytes only.

    Args:
        data: Raw bytes to inspect (only the first :data:`MAGIC_LEN` bytes are
            examined).
    """
    return len(data) >= MAGIC_LEN and data[:MAGIC_LEN] == MAGIC


def parse_file_header(data: bytes) -> tuple[int, int]:
    """Parse the per-file encryption header.

    Args:
        data: Raw bytes of the encrypted file (must contain at least the
            header).

    Returns:
        A tuple ``(key_id, data_offset)`` where *key_id* is the slot index
        to look up in the document ``encrypted`` plist, and *data_offset* is
        the byte offset at which encrypted segment data begins.

    Raises:
        OFEncryptionError: If the magic is wrong or the file is too short.
    """
    if len(data) < _MIN_HEADER_LEN:
        raise OFEncryptionError(
            f"Encrypted file too short: need at least {_MIN_HEADER_LEN} bytes, " f"got {len(data)}"
        )
    if data[:MAGIC_LEN] != MAGIC:
        raise OFEncryptionError(
            f"Not an OmniFileEncryption file: expected magic {MAGIC!r}, "
            f"got {data[:MAGIC_LEN]!r}"
        )

    info_length = int.from_bytes(data[MAGIC_LEN : MAGIC_LEN + 2], "big")
    key_id = int.from_bytes(data[MAGIC_LEN + 2 : MAGIC_LEN + 4], "big")

    # Segments start after: magic + 2-byte info_length field + info_length bytes,
    # rounded up to the next 16-byte boundary.
    raw_offset = MAGIC_LEN + 2 + info_length
    remainder = raw_offset % 16
    offset = raw_offset + (16 - remainder) % 16

    return key_id, offset
