"""Tests for :mod:`omnifocus.crypto.discovery` and :mod:`omnifocus.crypto.encryption`."""

from __future__ import annotations

import plistlib
import struct

import pytest

from omnifocus.crypto.discovery import (
    MAGIC,
    MAGIC_LEN,
    is_encrypted,
    parse_file_header,
)
from omnifocus.crypto.encryption import (
    _FILE_HMAC_LEN,
    _SEG_IV_LEN,
    _SEG_MAC_LEN,
    _parse_slots,
    create_encrypted_bundle,
    decrypt,
    encrypt,
    decrypt_file,
    encrypt_file,
    load_document_keys,
)
from omnifocus.errors import OFEncryptionError

PASSPHRASE = "correct-horse-battery-staple"
PLAINTEXT = b"PK\x03\x04 fake zip content for testing purposes only"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_keys() -> tuple[bytes, bytes]:
    """Return a deterministic (aes_key, hmac_key) pair for tests."""
    return b"A" * 16, b"B" * 16


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
        # Partial magic — fewer than MAGIC_LEN bytes
        assert is_encrypted(MAGIC[:10]) is False

    def test_empty(self) -> None:
        assert is_encrypted(b"") is False

    def test_old_magic_not_recognised(self) -> None:
        # Ensure the old 12-byte "OFEncryption" magic is NOT recognised
        assert is_encrypted(b"OFEncryption" + b"\x00" * 200) is False


# ---------------------------------------------------------------------------
# parse_file_header
# ---------------------------------------------------------------------------


class TestParseFileHeader:
    def _make_header(self, info_length: int = 2, key_id: int = 1) -> bytes:
        """Build a minimal valid per-file header."""
        header_core = MAGIC + struct.pack(">HH", info_length, key_id)
        pad_len = (16 - len(header_core) % 16) % 16
        return header_core + b"\x00" * pad_len

    def test_valid_header_returns_key_id_and_offset(self) -> None:
        data = self._make_header(info_length=2, key_id=1) + b"\xCC" * 100
        kid, offset = parse_file_header(data)
        assert kid == 1
        # magic(20) + info_length(2) + 2 = 24; pad to 32
        assert offset == 32

    def test_offset_aligned_to_16(self) -> None:
        # info_length=0 → raw_offset=22 → pad to 32
        data = self._make_header(info_length=0, key_id=5) + b"\xCC" * 100
        kid, offset = parse_file_header(data)
        assert kid == 5
        assert offset == 32
        assert offset % 16 == 0

    def test_info_length_18_offset_48(self) -> None:
        # info_length=18 → raw_offset=20+2+18=40 → 40%16=8 → padded to 48
        data = self._make_header(info_length=18, key_id=2) + b"\xCC" * 100
        _, offset = parse_file_header(data)
        assert offset == 48

    def test_too_short(self) -> None:
        with pytest.raises(OFEncryptionError, match="too short"):
            parse_file_header(b"OmniFile")

    def test_wrong_magic(self) -> None:
        data = b"OtherMagicXXXXXXXXXX" + b"\x00" * 200
        with pytest.raises(OFEncryptionError, match="Not an OmniFileEncryption file"):
            parse_file_header(data)

    def test_key_id_zero(self) -> None:
        data = self._make_header(info_length=2, key_id=0) + b"\xCC" * 100
        kid, _ = parse_file_header(data)
        assert kid == 0


# ---------------------------------------------------------------------------
# encrypt_file / decrypt_file round-trip
# ---------------------------------------------------------------------------


class TestEncryptDecryptRoundTrip:
    def test_basic_round_trip(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        result = decrypt_file(enc, aes_key, hmac_key)
        assert result == PLAINTEXT

    def test_encrypt_starts_with_magic(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        assert enc[:MAGIC_LEN] == MAGIC

    def test_random_iv_produces_different_ciphertext(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc1 = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        enc2 = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        assert enc1 != enc2

    def test_large_payload_multiple_segments(self) -> None:
        """Payload > 65536 bytes spans multiple segments."""
        aes_key, hmac_key = _make_keys()
        big = b"X" * 200_000
        enc = encrypt_file(big, aes_key, hmac_key)
        assert decrypt_file(enc, aes_key, hmac_key) == big

    def test_empty_plaintext(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(b"", aes_key, hmac_key)
        assert decrypt_file(enc, aes_key, hmac_key) == b""

    def test_single_byte(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(b"\xFF", aes_key, hmac_key)
        assert decrypt_file(enc, aes_key, hmac_key) == b"\xFF"

    def test_unicode_via_passphrase(self) -> None:
        """End-to-end with create_encrypted_bundle using a unicode passphrase."""
        pw = "Ünïcödé pässwörð 🔑"
        plist, enc = create_encrypted_bundle(PLAINTEXT, pw)
        keys = load_document_keys(pw, plist)
        aes_key, hmac_key = next(iter(keys.values()))
        assert decrypt_file(enc, aes_key, hmac_key) == PLAINTEXT

    def test_custom_key_id(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key, key_id=7)
        kid, _ = parse_file_header(enc)
        assert kid == 7
        assert decrypt_file(enc, aes_key, hmac_key) == PLAINTEXT


# ---------------------------------------------------------------------------
# decrypt_file error paths
# ---------------------------------------------------------------------------


class TestDecryptErrors:
    def test_wrong_hmac_key_fails_segment_mac(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        with pytest.raises(OFEncryptionError, match="MAC verification failed"):
            decrypt_file(enc, aes_key, b"W" * 16)

    def test_wrong_aes_key_still_fails_segment_mac(self) -> None:
        """Wrong AES key alone doesn't flip bits in the MAC — MAC still verified first."""
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        with pytest.raises(OFEncryptionError, match="MAC verification failed"):
            decrypt_file(enc, b"W" * 16, b"W" * 16)

    def test_tampered_ciphertext_fails_segment_mac(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = bytearray(encrypt_file(PLAINTEXT, aes_key, hmac_key))
        # Flip a byte in the first segment's ciphertext area
        header_end = 32  # MAGIC(20)+2+2 padded to 32
        ct_offset = header_end + _SEG_IV_LEN + _SEG_MAC_LEN
        enc[ct_offset] ^= 0xFF
        with pytest.raises(OFEncryptionError, match="MAC verification failed"):
            decrypt_file(bytes(enc), aes_key, hmac_key)

    def test_tampered_file_hmac_fails(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = bytearray(encrypt_file(PLAINTEXT, aes_key, hmac_key))
        enc[-1] ^= 0xFF  # flip last byte of file HMAC
        with pytest.raises(OFEncryptionError, match="File HMAC verification failed"):
            decrypt_file(bytes(enc), aes_key, hmac_key)

    def test_not_encrypted_file_raises(self) -> None:
        with pytest.raises(OFEncryptionError, match="Not an OmniFileEncryption file"):
            decrypt_file(b"PK\x03\x04" + b"\x00" * 200, *_make_keys())

    def test_file_too_short_for_segments(self) -> None:
        aes_key, hmac_key = _make_keys()
        # Build a file header only (no segments, no file HMAC)
        header = MAGIC + struct.pack(">HH", 2, 1) + b"\x00" * 8
        with pytest.raises(OFEncryptionError):
            decrypt_file(header, aes_key, hmac_key)

    def test_truncated_segment_header(self) -> None:
        aes_key, hmac_key = _make_keys()
        enc = encrypt_file(PLAINTEXT, aes_key, hmac_key)
        # Chop the file so there is exactly one byte after the header
        header_end = 32
        truncated = enc[:header_end + 1] + enc[-_FILE_HMAC_LEN:]
        with pytest.raises(OFEncryptionError):
            decrypt_file(truncated, aes_key, hmac_key)


# ---------------------------------------------------------------------------
# create_encrypted_bundle + load_document_keys round-trip
# ---------------------------------------------------------------------------


class TestCreateAndLoadBundle:
    def test_basic_round_trip(self) -> None:
        plist, enc = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        keys = load_document_keys(PASSPHRASE, plist)
        assert len(keys) >= 1
        aes_key, hmac_key = next(iter(keys.values()))
        assert decrypt_file(enc, aes_key, hmac_key) == PLAINTEXT

    def test_wrong_passphrase_raises(self) -> None:
        plist, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        with pytest.raises(OFEncryptionError, match="HMAC verification failed"):
            load_document_keys("wrong-passphrase", plist)

    def test_different_plist_each_call(self) -> None:
        plist1, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        plist2, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        assert plist1 != plist2  # random salt and keys

    def test_slot_id_stored_in_file(self) -> None:
        plist, enc = create_encrypted_bundle(PLAINTEXT, PASSPHRASE, slot_id=3)
        kid, _ = parse_file_header(enc)
        assert kid == 3

    def test_slot_id_present_in_plist_keys(self) -> None:
        plist, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE, slot_id=3)
        keys = load_document_keys(PASSPHRASE, plist)
        assert 3 in keys


# ---------------------------------------------------------------------------
# load_document_keys error paths
# ---------------------------------------------------------------------------


class TestLoadDocumentKeysErrors:
    def test_invalid_plist_raises(self) -> None:
        with pytest.raises(OFEncryptionError, match="Failed to parse 'encrypted' plist"):
            load_document_keys(PASSPHRASE, b"not a plist")

    def test_unsupported_prf_raises(self) -> None:
        plist, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        data = plistlib.loads(plist)
        data["prf"] = "md5"
        bad_plist = plistlib.dumps(data)
        with pytest.raises(OFEncryptionError, match="Unsupported PRF"):
            load_document_keys(PASSPHRASE, bad_plist)

    def test_list_plist_format(self) -> None:
        """plist wrapped in a list (OmniGroup format variant) still works."""
        plist, enc = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        data = plistlib.loads(plist)
        list_plist = plistlib.dumps([data])
        keys = load_document_keys(PASSPHRASE, list_plist)
        aes_key, hmac_key = next(iter(keys.values()))
        assert decrypt_file(enc, aes_key, hmac_key) == PLAINTEXT


# ---------------------------------------------------------------------------
# _parse_slots edge cases
# ---------------------------------------------------------------------------


class TestParseSlots:
    def test_unknown_slot_type_is_skipped(self) -> None:
        """Slot type 5 (PlaintextMask) is not AES_CTR_HMAC and must be skipped."""
        # type=5, len_units=8 (32 bytes data), slot_id=1
        blob = bytes([5, 8]) + b"\x00\x01" + b"\xAA" * 32 + b"\x00"
        slots = _parse_slots(blob)
        assert 1 not in slots

    def test_truncated_blob_stops_parsing(self) -> None:
        """If fewer than 4 bytes remain after the type byte, parsing stops."""
        # Only 1 byte in the blob — can't read len_units + slot_id
        slots = _parse_slots(b"\x03")
        assert slots == {}

    def test_slot_data_too_short_is_skipped(self) -> None:
        """AES_CTR_HMAC slot with fewer than 32 bytes of data is ignored."""
        # type=3, len_units=4 (16 bytes data) — needs >= 32 bytes for AES+HMAC keys
        blob = bytes([3, 4]) + b"\x00\x01" + b"\xAA" * 16 + b"\x00"
        slots = _parse_slots(blob)
        assert 1 not in slots

    def test_loop_exhausts_naturally_without_null_terminator(self) -> None:
        """Blob ends without a null terminator — while loop exits normally."""
        # type=3, len_units=8 (32 bytes data), slot_id=1, no trailing null byte
        blob = bytes([3, 8]) + b"\x00\x01" + b"A" * 16 + b"B" * 16
        slots = _parse_slots(blob)
        assert slots[1] == (b"A" * 16, b"B" * 16)


# ---------------------------------------------------------------------------
# decrypt convenience wrapper
# ---------------------------------------------------------------------------


class TestDecryptConvenienceWrapper:
    def test_round_trip_via_decrypt(self) -> None:
        plist, enc = create_encrypted_bundle(PLAINTEXT, PASSPHRASE)
        result = decrypt(enc, PASSPHRASE, plist)
        assert result == PLAINTEXT

    def test_encrypt_alias_round_trip(self) -> None:
        """encrypt() alias creates a valid (plist, file) tuple."""
        plist, enc = encrypt(PLAINTEXT, PASSPHRASE)
        result = decrypt(enc, PASSPHRASE, plist)
        assert result == PLAINTEXT

    def test_unknown_key_slot_raises(self) -> None:
        plist, _ = create_encrypted_bundle(PLAINTEXT, PASSPHRASE, slot_id=1)
        # Encrypt with a key_id that does not exist in the plist (slot_id=99)
        bad_file = encrypt_file(PLAINTEXT, b"A" * 16, b"B" * 16, key_id=99)
        with pytest.raises(OFEncryptionError, match="Key slot 99 not found"):
            decrypt(bad_file, PASSPHRASE, plist)
