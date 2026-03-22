"""OmniFocus OmniFileEncryption v2 encryption and decryption.

Key derivation
--------------
Document keys are stored in a plist file named ``encrypted`` at the root of
the ``.ofocus`` bundle.  The plist contains PBKDF2 parameters and a wrapped
key blob:

.. code-block:: text

    {
        "method":    "password",
        "algorithm": "PBKDF2; aes128-wrap",
        "rounds":    <int>,
        "salt":      <bytes>,
        "prf":       "sha1" | "sha256" | "sha512",  # default sha1
        "key":       <bytes>,   # AES-128-WRAP wrapped key slot blob
    }

Derivation steps:

1. ``wrapping_key = PBKDF2(passphrase_utf8, salt, rounds, prf, length=16)``
2. ``slot_blob = AES_KEY_UNWRAP(wrapping_key, plist["key"])``
3. Parse slot blob (see :func:`_parse_slots`): each slot record is
   ``type(1) || len_units(1) || slot_id(2 BE) || data(len_units*4)``.
   For type 3/4 (AES_CTR_HMAC), data is 32 bytes: first 16 = AES-128 key,
   last 16 = HMAC-SHA-256 key.

Per-file format
---------------
After the header (parsed by :func:`~omnifocus.crypto.discovery.parse_file_header`),
each file contains one or more segments followed by a 32-byte file HMAC:

.. code-block:: text

    [12B IV | 20B seg_MAC | up to 65536B AES-128-CTR ciphertext] * N
    32B file_HMAC

Segment MAC: ``HMAC-SHA256(hmac_key, IV || seg_idx_BE32 || ciphertext)[:20]``
File HMAC:   ``HMAC-SHA256(hmac_key, \\x01 || seg_mac_0 || … || seg_mac_N)``
AES nonce:   ``IV || \\x00\\x00\\x00\\x00`` (12-byte IV + 4 zero bytes = 16B counter)

Usage::

    from omnifocus.crypto.encryption import load_document_keys, decrypt_file

    doc_keys = load_document_keys(passphrase, plist_bytes)
    aes_key, hmac_key = doc_keys[key_id]
    plaintext = decrypt_file(encrypted_bytes, aes_key, hmac_key)
"""

from __future__ import annotations

import hmac as _hmac
import os
import plistlib
import struct

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.keywrap import InvalidUnwrap, aes_key_unwrap, aes_key_wrap

from omnifocus.crypto.discovery import MAGIC, MAGIC_LEN, parse_file_header
from omnifocus.errors import OFEncryptionError

# ---- Segment / file constants --------------------------------------------

_SEGMENT_SIZE = 65536       # bytes of plaintext / ciphertext per segment
_SEG_IV_LEN = 12            # bytes
_SEG_MAC_LEN = 20           # truncated HMAC-SHA256 bytes per segment
_FILE_HMAC_LEN = 32         # bytes at end of file

# ---- Key-slot type IDs ---------------------------------------------------

_SLOT_ACTIVE_AES_CTR_HMAC = 3
_SLOT_RETIRED_AES_CTR_HMAC = 4

# ---- PBKDF2 PRF map ------------------------------------------------------

_PRF_MAP: dict[str, type] = {
    "sha1":   hashes.SHA1,
    "sha256": hashes.SHA256,
    "sha512": hashes.SHA512,
}

# PBKDF2 parameters used when *creating* test bundles
_TEST_PBKDF2_ROUNDS = 100_000
_TEST_PBKDF2_PRF = "sha256"


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def load_document_keys(
    passphrase: str, encrypted_plist: bytes
) -> dict[int, tuple[bytes, bytes]]:
    """Derive per-slot AES and HMAC keys from the bundle ``encrypted`` plist.

    Args:
        passphrase: The OmniFocus database passphrase.
        encrypted_plist: Raw bytes of the ``encrypted`` plist file downloaded
            from the WebDAV bundle root.

    Returns:
        A mapping ``{slot_id: (aes_key_16, hmac_key_16)}``.

    Raises:
        OFEncryptionError: If the plist is malformed, the passphrase is wrong,
            or the PRF algorithm is unsupported.
    """
    try:
        data = plistlib.loads(encrypted_plist)
    except Exception as exc:
        raise OFEncryptionError(
            f"Failed to parse 'encrypted' plist: {exc}"
        ) from exc

    if isinstance(data, list):
        data = data[0]

    salt = bytes(data["salt"])
    rounds = int(data["rounds"])
    prf_str = data.get("prf", "sha1")
    if prf_str not in _PRF_MAP:
        raise OFEncryptionError(
            f"Unsupported PRF algorithm in 'encrypted' plist: {prf_str!r}"
        )

    kdf = PBKDF2HMAC(
        algorithm=_PRF_MAP[prf_str](),
        length=16,
        salt=salt,
        iterations=rounds,
    )
    wrapping_key = kdf.derive(passphrase.encode("utf-8"))

    wrapped = bytes(data["key"])
    try:
        slot_blob = aes_key_unwrap(wrapping_key, wrapped)
    except InvalidUnwrap as exc:
        raise OFEncryptionError(
            "HMAC verification failed — wrong passphrase or corrupted key data"
        ) from exc

    return {
        slot_id: (aes_key, hmac_key)
        for slot_id, _, aes_key, hmac_key in _parse_slot_records(slot_blob)
    }


def _parse_slot_records(blob: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """Parse a decrypted slot blob into ``(slot_id, slot_type, aes_key, hmac_key)`` records."""
    slots: list[tuple[int, int, bytes, bytes]] = []
    i = 0
    while i < len(blob):
        slot_type = blob[i]
        if slot_type == 0:
            break
        if i + 4 > len(blob):
            break
        data_len = blob[i + 1] * 4
        slot_id = struct.unpack(">H", blob[i + 2: i + 4])[0]
        slot_data = blob[i + 4: i + 4 + data_len]

        if slot_type in (_SLOT_ACTIVE_AES_CTR_HMAC, _SLOT_RETIRED_AES_CTR_HMAC):
            if len(slot_data) >= 32:
                slots.append((slot_id, slot_type, slot_data[:16], slot_data[16:32]))

        i += 4 + data_len
    return slots


def load_writable_document_key(
    passphrase: str, encrypted_plist: bytes
) -> tuple[int, bytes, bytes]:
    """Return the active AES/HMAC key slot for outbound encryption.

    Args:
        passphrase: The OmniFocus database passphrase.
        encrypted_plist: Raw bytes of the bundle ``encrypted`` plist.

    Returns:
        ``(slot_id, aes_key, hmac_key)`` for the active writable slot.

    Raises:
        OFEncryptionError: If no active AES-CTR/HMAC slot exists.
    """
    try:
        data = plistlib.loads(encrypted_plist)
    except Exception as exc:
        raise OFEncryptionError(
            f"Failed to parse 'encrypted' plist: {exc}"
        ) from exc

    if isinstance(data, list):
        data = data[0]

    salt = bytes(data["salt"])
    rounds = int(data["rounds"])
    prf_str = data.get("prf", "sha1")
    if prf_str not in _PRF_MAP:
        raise OFEncryptionError(
            f"Unsupported PRF algorithm in 'encrypted' plist: {prf_str!r}"
        )

    kdf = PBKDF2HMAC(
        algorithm=_PRF_MAP[prf_str](),
        length=16,
        salt=salt,
        iterations=rounds,
    )
    wrapping_key = kdf.derive(passphrase.encode("utf-8"))

    wrapped = bytes(data["key"])
    try:
        slot_blob = aes_key_unwrap(wrapping_key, wrapped)
    except InvalidUnwrap as exc:
        raise OFEncryptionError(
            "HMAC verification failed — wrong passphrase or corrupted key data"
        ) from exc

    for slot_id, slot_type, aes_key, hmac_key in _parse_slot_records(slot_blob):
        if slot_type == _SLOT_ACTIVE_AES_CTR_HMAC:
            return slot_id, aes_key, hmac_key
    raise OFEncryptionError("No active writable encryption key slot found in bundle")


# ---------------------------------------------------------------------------
# Per-file decryption
# ---------------------------------------------------------------------------


def decrypt_file(data: bytes, aes_key: bytes, hmac_key: bytes) -> bytes:
    """Decrypt a single OmniFileEncryption v2 file.

    Args:
        data: Raw encrypted file bytes (including the per-file header).
        aes_key: 16-byte AES-128 key from the document key slot.
        hmac_key: 16-byte HMAC-SHA256 key from the document key slot.

    Returns:
        Decrypted plaintext bytes.

    Raises:
        OFEncryptionError: On MAC verification failure or malformed data.
    """
    _, offset = parse_file_header(data)

    file_end = len(data)
    segments_end = file_end - _FILE_HMAC_LEN
    if segments_end < offset:
        raise OFEncryptionError("Encrypted file too short to contain segment data")

    file_hmac_stored = data[segments_end:]

    pos = offset
    seg_idx = 0
    plaintext_parts: list[bytes] = []
    seg_macs: list[bytes] = []

    while pos < segments_end:
        if segments_end - pos < _SEG_IV_LEN + _SEG_MAC_LEN:
            raise OFEncryptionError(
                f"Truncated segment {seg_idx}: not enough bytes for IV + MAC"
            )

        iv = data[pos: pos + _SEG_IV_LEN]
        seg_mac = bytes(data[pos + _SEG_IV_LEN: pos + _SEG_IV_LEN + _SEG_MAC_LEN])
        ct_start = pos + _SEG_IV_LEN + _SEG_MAC_LEN
        ct_end = min(ct_start + _SEGMENT_SIZE, segments_end)
        ct = data[ct_start:ct_end]

        # Verify segment MAC: HMAC(hmac_key, IV || seg_idx_BE32 || ct)[:20]
        h = _hmac.new(hmac_key, digestmod="sha256")
        h.update(iv)
        h.update(struct.pack(">I", seg_idx))
        h.update(ct)
        computed_mac = h.digest()[:_SEG_MAC_LEN]
        if not _hmac.compare_digest(computed_mac, seg_mac):
            raise OFEncryptionError(
                f"Segment {seg_idx} MAC verification failed — "
                "wrong key or corrupted data"
            )

        # Decrypt: AES-128-CTR with nonce = IV || 0x00000000
        nonce = iv + b"\x00\x00\x00\x00"
        cipher = Cipher(algorithms.AES(aes_key), modes.CTR(nonce))
        dec = cipher.decryptor()
        plaintext_parts.append(dec.update(ct) + dec.finalize())

        seg_macs.append(seg_mac)
        pos = ct_end
        seg_idx += 1

    # Verify file HMAC: HMAC(hmac_key, \x01 || seg_mac_0 || … || seg_mac_N)
    h = _hmac.new(hmac_key, digestmod="sha256")
    h.update(b"\x01")
    for m in seg_macs:
        h.update(m)
    computed_file_hmac = h.digest()
    if not _hmac.compare_digest(computed_file_hmac, bytes(file_hmac_stored)):
        raise OFEncryptionError(
            "File HMAC verification failed — wrong key or corrupted data"
        )

    return b"".join(plaintext_parts)


# ---------------------------------------------------------------------------
# Per-file encryption  (used for testing and the write path)
# ---------------------------------------------------------------------------


def encrypt_file(
    data: bytes, aes_key: bytes, hmac_key: bytes, key_id: int = 1
) -> bytes:
    """Encrypt *data* in OmniFileEncryption v2 format.

    Args:
        data: Plaintext bytes to encrypt.
        aes_key: 16-byte AES-128 key.
        hmac_key: 16-byte HMAC-SHA256 key.
        key_id: Key slot ID recorded in the file header.

    Returns:
        Encrypted file bytes (header + segments + file HMAC).
    """
    # Header: MAGIC + info_length(2B) + key_id(2B), padded to 16-byte boundary
    info_length = 2  # only the key_id field, no per-file key material
    header_core = MAGIC + struct.pack(">HH", info_length, key_id)
    pad_len = (16 - len(header_core) % 16) % 16
    file_header = header_core + b"\x00" * pad_len

    # Split plaintext into segments (at least one, even for empty data)
    chunks = [data[i: i + _SEGMENT_SIZE] for i in range(0, len(data), _SEGMENT_SIZE)]
    if not chunks:
        chunks = [b""]

    seg_macs: list[bytes] = []
    segment_parts: list[bytes] = []

    for seg_idx, chunk in enumerate(chunks):
        iv = os.urandom(_SEG_IV_LEN)

        # Encrypt: AES-128-CTR with nonce = IV || 0x00000000
        nonce = iv + b"\x00\x00\x00\x00"
        cipher = Cipher(algorithms.AES(aes_key), modes.CTR(nonce))
        enc = cipher.encryptor()
        ct = enc.update(chunk) + enc.finalize()

        # Compute segment MAC
        h = _hmac.new(hmac_key, digestmod="sha256")
        h.update(iv)
        h.update(struct.pack(">I", seg_idx))
        h.update(ct)
        seg_mac = h.digest()[:_SEG_MAC_LEN]

        seg_macs.append(seg_mac)
        segment_parts.append(iv + seg_mac + ct)

    # Compute file HMAC
    h = _hmac.new(hmac_key, digestmod="sha256")
    h.update(b"\x01")
    for m in seg_macs:
        h.update(m)
    file_hmac = h.digest()

    return file_header + b"".join(segment_parts) + file_hmac


# ---------------------------------------------------------------------------
# Bundle helper (testing + future write path)
# ---------------------------------------------------------------------------


def create_encrypted_bundle(
    plaintext: bytes, passphrase: str, slot_id: int = 1
) -> tuple[bytes, bytes]:
    """Create a complete encrypted bundle entry for testing.

    Generates random AES-128 and HMAC keys, wraps them in a PBKDF2-derived
    AES-128 key, builds the ``encrypted`` plist, and encrypts *plaintext*.

    Args:
        plaintext: Raw bytes to encrypt (e.g. a ZIP archive).
        passphrase: Encryption passphrase.
        slot_id: Key slot ID to embed in the file header (default 1).

    Returns:
        ``(encrypted_plist_bytes, encrypted_file_bytes)``
    """
    aes_key = os.urandom(16)
    hmac_key = os.urandom(16)

    # Build slot blob: type(1) + len_units(1) + slot_id(2 BE) + key_data(32)
    slot_data = aes_key + hmac_key  # 32 bytes
    data_len_units = len(slot_data) // 4  # 8
    slot_blob = (
        bytes([_SLOT_ACTIVE_AES_CTR_HMAC, data_len_units])
        + struct.pack(">H", slot_id)
        + slot_data
    )
    # Terminate with a type-0 byte and pad to a multiple of 8 (AES key-wrap requirement)
    slot_blob += b"\x00"
    while len(slot_blob) % 8 != 0:
        slot_blob += b"\x00"

    # Derive the wrapping key from the passphrase
    salt = os.urandom(32)
    kdf = PBKDF2HMAC(
        algorithm=_PRF_MAP[_TEST_PBKDF2_PRF](),
        length=16,
        salt=salt,
        iterations=_TEST_PBKDF2_ROUNDS,
    )
    wrapping_key = kdf.derive(passphrase.encode("utf-8"))

    # Wrap the slot blob
    wrapped = aes_key_wrap(wrapping_key, slot_blob)

    # Assemble the plist
    plist_data: dict = {
        "method": "password",
        "algorithm": "PBKDF2; aes128-wrap",
        "rounds": _TEST_PBKDF2_ROUNDS,
        "salt": salt,
        "prf": _TEST_PBKDF2_PRF,
        "key": wrapped,
    }
    encrypted_plist = plistlib.dumps(plist_data)

    encrypted_file = encrypt_file(plaintext, aes_key, hmac_key, key_id=slot_id)
    return encrypted_plist, encrypted_file


# ---------------------------------------------------------------------------
# Convenience aliases kept for backward compatibility
# ---------------------------------------------------------------------------


def encrypt(data: bytes, passphrase: str) -> tuple[bytes, bytes]:
    """Alias for :func:`create_encrypted_bundle`.

    Returns ``(encrypted_plist_bytes, encrypted_file_bytes)``.
    """
    return create_encrypted_bundle(data, passphrase)


def decrypt(file_data: bytes, passphrase: str, encrypted_plist: bytes) -> bytes:
    """Convenience wrapper: derive keys from *passphrase* + plist then decrypt.

    Args:
        file_data: Encrypted file bytes.
        passphrase: Encryption passphrase.
        encrypted_plist: Raw bytes of the bundle ``encrypted`` plist.

    Returns:
        Decrypted plaintext bytes.
    """
    doc_keys = load_document_keys(passphrase, encrypted_plist)
    _, offset = parse_file_header(file_data)
    key_id = int.from_bytes(
        file_data[MAGIC_LEN + 2: MAGIC_LEN + 4], "big"
    )
    if key_id not in doc_keys:
        raise OFEncryptionError(f"Key slot {key_id} not found in document keys")
    aes_key, hmac_key = doc_keys[key_id]
    return decrypt_file(file_data, aes_key, hmac_key)
