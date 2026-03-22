"""OmniFocus AES-256-CBC encryption and decryption.

Implements the binary envelope format documented in
:mod:`omnifocus.crypto.discovery`.

Security notes
--------------
- Key derivation uses PBKDF2-HMAC-SHA256 with 100,000 iterations.
- The AES key (32 bytes) and HMAC key (32 bytes) are derived from a single
  512-bit output of PBKDF2: ``key_material = PBKDF2(passphrase, salt, 64 bytes)``
  with the first 32 bytes used as the AES key and the last 32 bytes as the
  HMAC key.
- HMAC-SHA256 is computed over ``version || flags || salt || iv || ciphertext``
  and verified with :func:`hmac.compare_digest` to prevent timing attacks before
  any decryption is attempted.
- The passphrase is accepted as a :class:`str` and encoded to UTF-8 internally.
  The raw passphrase and derived key material are *not* retained on the instance.

Usage::

    from omnifocus.crypto.encryption import decrypt, encrypt

    # Decrypt
    plaintext_zip = decrypt(encrypted_bytes, passphrase="my secret")

    # Encrypt
    encrypted = encrypt(plaintext_zip, passphrase="my secret")
"""

from __future__ import annotations

import hmac as _hmac
import os

from cryptography.hazmat.primitives import hashes, padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from omnifocus.crypto.discovery import (
    CIPHERTEXT_OFFSET,
    FLAGS_OFFSET,
    FLAGS_SIZE,
    HMAC_OFFSET,
    HMAC_SIZE,
    IV_OFFSET,
    IV_SIZE,
    MAGIC,
    MAGIC_LEN,
    SALT_OFFSET,
    SALT_SIZE,
    VERSION_OFFSET,
    VERSION_SIZE,
    EncryptionVersion,
    detect_format,
)
from omnifocus.errors import OFEncryptionError

# PBKDF2 parameters
_PBKDF2_ITERATIONS = 100_000
_DERIVED_KEY_LEN = 64  # 32 bytes AES key + 32 bytes HMAC key


# ---------------------------------------------------------------------------
# Key derivation
# ---------------------------------------------------------------------------


def _derive_keys(passphrase: str, salt: bytes) -> tuple[bytes, bytes]:
    """Derive AES and HMAC keys from *passphrase* and *salt*.

    Args:
        passphrase: User-supplied passphrase (UTF-8).
        salt: 32-byte random salt from the encryption header.

    Returns:
        ``(aes_key, hmac_key)`` — each 32 bytes.
    """
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=_DERIVED_KEY_LEN,
        salt=salt,
        iterations=_PBKDF2_ITERATIONS,
    )
    key_material = kdf.derive(passphrase.encode("utf-8"))
    aes_key = key_material[:32]
    hmac_key = key_material[32:]
    return aes_key, hmac_key


# ---------------------------------------------------------------------------
# HMAC computation
# ---------------------------------------------------------------------------


def _compute_hmac(
    hmac_key: bytes,
    version_bytes: bytes,
    flags_bytes: bytes,
    salt: bytes,
    iv: bytes,
    ciphertext: bytes,
) -> bytes:
    """Compute the HMAC-SHA256 of header fields + ciphertext.

    The MAC covers: ``version || flags || salt || iv || ciphertext``.
    """
    h = _hmac.new(hmac_key, digestmod="sha256")
    h.update(version_bytes)
    h.update(flags_bytes)
    h.update(salt)
    h.update(iv)
    h.update(ciphertext)
    return h.digest()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def decrypt(data: bytes, passphrase: str) -> bytes:
    """Decrypt an OmniFocus encrypted file.

    Args:
        data: Raw bytes of the encrypted file as downloaded from WebDAV.
        passphrase: The OmniFocus database passphrase.

    Returns:
        Plaintext bytes (a ZIP archive containing ``contents.xml``).

    Raises:
        OFEncryptionError: If the file is not in the expected format, the
            HMAC verification fails (wrong passphrase or tampered data), or
            decryption / unpadding fails.
    """
    header = detect_format(data)  # raises OFEncryptionError on bad magic / version

    ciphertext = data[CIPHERTEXT_OFFSET:]
    if not ciphertext:
        raise OFEncryptionError("Encrypted file has no ciphertext after the header")

    aes_key, hmac_key = _derive_keys(passphrase, header.salt)

    version_bytes = data[VERSION_OFFSET:VERSION_OFFSET + VERSION_SIZE]
    flags_bytes = data[FLAGS_OFFSET:FLAGS_OFFSET + FLAGS_SIZE]

    expected_hmac = _compute_hmac(
        hmac_key, version_bytes, flags_bytes, header.salt, header.iv, ciphertext
    )
    if not _hmac.compare_digest(expected_hmac, header.hmac):
        raise OFEncryptionError(
            "HMAC verification failed — wrong passphrase or corrupted data"
        )

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(header.iv))
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    unpadder = padding.PKCS7(128).unpadder()
    try:
        plaintext = unpadder.update(padded_plaintext) + unpadder.finalize()
    except ValueError as exc:
        raise OFEncryptionError(f"PKCS7 unpadding failed: {exc}") from exc

    return plaintext


def encrypt(data: bytes, passphrase: str) -> bytes:
    """Encrypt a ZIP archive using the OmniFocus V1 format.

    Generates a fresh random salt and IV on every call.

    Args:
        data: Plaintext bytes to encrypt (a ZIP archive).
        passphrase: The OmniFocus database passphrase.

    Returns:
        Raw bytes in the OmniFocus encrypted file format.
    """
    salt = os.urandom(SALT_SIZE)
    iv = os.urandom(IV_SIZE)

    aes_key, hmac_key = _derive_keys(passphrase, salt)

    padder = padding.PKCS7(128).padder()
    padded = padder.update(data) + padder.finalize()

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded) + encryptor.finalize()

    version = EncryptionVersion.V1
    version_bytes = version.to_bytes(VERSION_SIZE, "big")
    flags_bytes = (0).to_bytes(FLAGS_SIZE, "big")

    mac = _compute_hmac(hmac_key, version_bytes, flags_bytes, salt, iv, ciphertext)

    # Assemble the file
    header = (
        MAGIC
        + version_bytes
        + flags_bytes
        + salt
        + iv
        + mac
    )
    assert len(header) == CIPHERTEXT_OFFSET, (  # noqa: S101
        f"Header size mismatch: {len(header)} != {CIPHERTEXT_OFFSET}"
    )
    return header + ciphertext
