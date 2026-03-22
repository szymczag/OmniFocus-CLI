"""Tests for :mod:`omnifocus.crypto.discovery` and :mod:`omnifocus.crypto.encryption`."""

from __future__ import annotations

import struct

import pytest

from omnifocus.crypto.discovery import (
    CIPHERTEXT_OFFSET,
    HMAC_OFFSET,
    HMAC_SIZE,
    MAGIC,
    MAGIC_LEN,
    EncryptionVersion,
    detect_format,
    is_encrypted,
)
from omnifocus.crypto.encryption import decrypt, encrypt
from omnifocus.errors import OFEncryptionError

PASSPHRASE = "correct-horse-battery-staple"
PLAINTEXT = b"PK\x03\x04 fake zip content for testing purposes only"


# ---------------------------------------------------------------------------
# is_encrypted
# ---------------------------------------------------------------------------


class TestIsEncrypted:
    def test_valid_magic(self) -> None:
        data = MAGIC + b"\x00" * 200
        assert is_encrypted(data) is True

    def test_zip_magic(self) -> None:
        assert is_encrypted(b"PK\x03\x04" + b"\x00" * 100) is False

    def test_too_short(self) -> None:
        assert is_encrypted(b"OFEncr") is False

    def test_empty(self) -> None:
        assert is_encrypted(b"") is False


# ---------------------------------------------------------------------------
# detect_format
# ---------------------------------------------------------------------------


class TestDetectFormat:
    def _make_header(
        self,
        version: int = 1,
        flags: int = 0,
        salt: bytes | None = None,
        iv: bytes | None = None,
        hmac: bytes | None = None,
    ) -> bytes:
        """Build a synthetic encryption header."""
        salt = salt or b"\xAA" * 32
        iv = iv or b"\xBB" * 16
        hmac_val = hmac or b"\xCC" * 32
        header = (
            MAGIC
            + version.to_bytes(4, "big")
            + flags.to_bytes(4, "big")
            + salt
            + iv
            + hmac_val
        )
        # Pad to minimum length with fake ciphertext
        return header + b"\x00" * 16

    def test_valid_v1(self) -> None:
        data = self._make_header()
        hdr = detect_format(data)
        assert hdr.version == EncryptionVersion.V1
        assert hdr.flags == 0
        assert hdr.salt == b"\xAA" * 32
        assert hdr.iv == b"\xBB" * 16
        assert hdr.hmac == b"\xCC" * 32

    def test_too_short(self) -> None:
        with pytest.raises(OFEncryptionError, match="too short"):
            detect_format(b"OFEncryption")

    def test_wrong_magic(self) -> None:
        data = b"OtherMagicXX" + b"\x00" * 200
        with pytest.raises(OFEncryptionError, match="Not an OmniFocus encrypted file"):
            detect_format(data)

    def test_unsupported_version(self) -> None:
        data = self._make_header(version=99)
        with pytest.raises(OFEncryptionError, match="Unsupported"):
            detect_format(data)

    def test_flags_preserved(self) -> None:
        data = self._make_header(flags=42)
        hdr = detect_format(data)
        assert hdr.flags == 42


# ---------------------------------------------------------------------------
# encrypt / decrypt round-trip
# ---------------------------------------------------------------------------


class TestEncryptDecryptRoundTrip:
    def test_basic_round_trip(self) -> None:
        encrypted = encrypt(PLAINTEXT, PASSPHRASE)
        result = decrypt(encrypted, PASSPHRASE)
        assert result == PLAINTEXT

    def test_different_passphrase_fails(self) -> None:
        encrypted = encrypt(PLAINTEXT, PASSPHRASE)
        with pytest.raises(OFEncryptionError, match="HMAC verification failed"):
            decrypt(encrypted, "wrong-passphrase")

    def test_encrypt_produces_different_bytes_each_call(self) -> None:
        """Random salt and IV must produce different ciphertext each call."""
        enc1 = encrypt(PLAINTEXT, PASSPHRASE)
        enc2 = encrypt(PLAINTEXT, PASSPHRASE)
        assert enc1 != enc2

    def test_both_start_with_magic(self) -> None:
        enc = encrypt(PLAINTEXT, PASSPHRASE)
        assert enc[:MAGIC_LEN] == MAGIC

    def test_encrypted_longer_than_plaintext(self) -> None:
        enc = encrypt(PLAINTEXT, PASSPHRASE)
        assert len(enc) > len(PLAINTEXT)

    def test_large_payload(self) -> None:
        big = b"A" * 1_000_000
        enc = encrypt(big, PASSPHRASE)
        result = decrypt(enc, PASSPHRASE)
        assert result == big

    def test_empty_passphrase(self) -> None:
        enc = encrypt(PLAINTEXT, "")
        result = decrypt(enc, "")
        assert result == PLAINTEXT

    def test_unicode_passphrase(self) -> None:
        pw = "Ünïcödé pässwörð 🔑"
        enc = encrypt(PLAINTEXT, pw)
        result = decrypt(enc, pw)
        assert result == PLAINTEXT


# ---------------------------------------------------------------------------
# decrypt error paths
# ---------------------------------------------------------------------------


class TestDecryptErrors:
    def test_tampered_hmac(self) -> None:
        enc = encrypt(PLAINTEXT, PASSPHRASE)
        # Flip one byte in the HMAC
        enc_list = bytearray(enc)
        enc_list[HMAC_OFFSET] ^= 0xFF
        with pytest.raises(OFEncryptionError, match="HMAC verification failed"):
            decrypt(bytes(enc_list), PASSPHRASE)

    def test_tampered_ciphertext(self) -> None:
        enc = encrypt(PLAINTEXT, PASSPHRASE)
        enc_list = bytearray(enc)
        enc_list[CIPHERTEXT_OFFSET] ^= 0xFF
        with pytest.raises(OFEncryptionError, match="HMAC verification failed"):
            decrypt(bytes(enc_list), PASSPHRASE)

    def test_not_encrypted_file(self) -> None:
        with pytest.raises(OFEncryptionError, match="Not an OmniFocus"):
            decrypt(b"PK\x03\x04" + b"\x00" * 200, PASSPHRASE)

    def test_empty_ciphertext_in_header(self) -> None:
        """A file with a valid header but zero ciphertext bytes must fail."""
        # Build just the header, nothing after
        import os
        salt = os.urandom(32)
        iv = os.urandom(16)
        hmac_val = b"\x00" * 32
        header = (
            MAGIC
            + (1).to_bytes(4, "big")
            + (0).to_bytes(4, "big")
            + salt
            + iv
            + hmac_val
        )
        assert len(header) == CIPHERTEXT_OFFSET
        with pytest.raises(OFEncryptionError, match="no ciphertext"):
            decrypt(header, PASSPHRASE)
