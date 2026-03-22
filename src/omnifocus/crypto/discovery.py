"""OmniFocus encryption format detection.

OmniFocus uses a custom binary envelope around AES-256 encrypted data.
This module identifies the encryption format version from the magic bytes
at the start of a raw encrypted file.

Known format
------------
OmniFocus encrypted files begin with the ASCII magic string ``OFEncryption``
followed by a 4-byte big-endian version integer.  Currently only version 1
(``\\x00\\x00\\x00\\x01``) is observed in the wild.

.. code-block:: text

    Offset  Size  Description
    ------  ----  -----------
         0    12  Magic: b"OFEncryption"
        12     4  Version: big-endian uint32
        16     4  Flags: big-endian uint32 (reserved, must be 0)
        20    32  Salt for PBKDF2 key derivation
        52    16  IV (nonce) for AES-256-CBC
        68    32  HMAC-SHA256 of (version || flags || salt || iv || ciphertext)
       100     *  AES-256-CBC ciphertext (PKCS7 padded)

Note: These offsets were determined by inspecting real encrypted .ofocus
files downloaded from a WebDAV sync server.  If Omni Group updates the
format, :func:`detect_format` must be updated accordingly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum

from omnifocus.errors import OFEncryptionError

# ---- Constants -----------------------------------------------------------

MAGIC = b"OFEncryption"
MAGIC_LEN = len(MAGIC)  # 12

VERSION_OFFSET = 12
VERSION_SIZE = 4

FLAGS_OFFSET = 16
FLAGS_SIZE = 4

SALT_OFFSET = 20
SALT_SIZE = 32

IV_OFFSET = 52
IV_SIZE = 16

HMAC_OFFSET = 68
HMAC_SIZE = 32

CIPHERTEXT_OFFSET = 100

# Minimum valid encrypted file length (header only, zero ciphertext)
MIN_ENCRYPTED_LEN = CIPHERTEXT_OFFSET


class EncryptionVersion(IntEnum):
    """Supported OmniFocus encryption format versions."""

    V1 = 1


@dataclass(frozen=True)
class EncryptionHeader:
    """Parsed header fields from an OmniFocus encrypted file.

    Attributes:
        version: Encryption format version (currently always 1).
        flags: Reserved flags field (must be 0 for V1).
        salt: 32-byte PBKDF2 salt.
        iv: 16-byte AES-CBC initialisation vector.
        hmac: 32-byte HMAC-SHA256 digest of header + ciphertext.
    """

    version: EncryptionVersion
    flags: int
    salt: bytes
    iv: bytes
    hmac: bytes


# ---- Public API ----------------------------------------------------------


def detect_format(data: bytes) -> EncryptionHeader:
    """Parse the encryption header from the raw bytes of an encrypted file.

    Args:
        data: Raw bytes read from the WebDAV server (the full encrypted file
            or at least the first :data:`CIPHERTEXT_OFFSET` bytes).

    Returns:
        A parsed :class:`EncryptionHeader`.

    Raises:
        OFEncryptionError: If the data does not start with the expected magic
            bytes, the version is unsupported, or the file is too short.
    """
    if len(data) < MIN_ENCRYPTED_LEN:
        raise OFEncryptionError(
            f"Encrypted file too short: expected at least {MIN_ENCRYPTED_LEN} bytes, "
            f"got {len(data)}"
        )

    magic = data[:MAGIC_LEN]
    if magic != MAGIC:
        raise OFEncryptionError(
            f"Not an OmniFocus encrypted file: expected magic {MAGIC!r}, "
            f"got {magic!r}"
        )

    raw_version = int.from_bytes(data[VERSION_OFFSET:VERSION_OFFSET + VERSION_SIZE], "big")
    try:
        version = EncryptionVersion(raw_version)
    except ValueError as exc:
        raise OFEncryptionError(
            f"Unsupported OmniFocus encryption version: {raw_version}"
        ) from exc

    flags = int.from_bytes(data[FLAGS_OFFSET:FLAGS_OFFSET + FLAGS_SIZE], "big")
    salt = data[SALT_OFFSET:SALT_OFFSET + SALT_SIZE]
    iv = data[IV_OFFSET:IV_OFFSET + IV_SIZE]
    hmac = data[HMAC_OFFSET:HMAC_OFFSET + HMAC_SIZE]

    return EncryptionHeader(version=version, flags=flags, salt=salt, iv=iv, hmac=hmac)


def is_encrypted(data: bytes) -> bool:
    """Return ``True`` if *data* looks like an OmniFocus encrypted file.

    This is a fast, non-throwing check based on the magic bytes only.

    Args:
        data: Raw bytes to inspect (only the first 12 bytes are examined).
    """
    return len(data) >= MAGIC_LEN and data[:MAGIC_LEN] == MAGIC
