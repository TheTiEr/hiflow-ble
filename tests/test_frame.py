"""Roundtrip tests for V0/V1 frame builders.

Crypto correctness is independent of the specific encRand value — any 16-byte
key works for the SHA-256³-based derivation. We use synthetic fixed test
vectors here so the tests stay deterministic without leaking real device IDs.
"""

from __future__ import annotations

import pytest

from hiflow_ble.crypt_util import derive_key, derive_nonce, derive_v0_iv, derive_v0_key
from hiflow_ble.errors import EncRandStale
from hiflow_ble.frame import (
    build_frame,
    build_frame_v0,
    crc16_modbus,
    parse_frame,
    parse_frame_v0,
)

# Synthetic test vectors — not a real device.
ENC_RAND = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
SN = "0000000000AA"


def test_v1_roundtrip() -> None:
    pt = b"the quick brown fox jumps over the lazy dog"
    frame = build_frame(ENC_RAND, 0xA311, 0x0042, pt)
    assert frame.startswith(b"HM\xa3\x11\x00\x42")
    cmd, tid, decoded = parse_frame(ENC_RAND, frame)
    assert (cmd, tid, decoded) == (0xA311, 0x0042, pt)


def test_v1_empty_plaintext() -> None:
    frame = build_frame(ENC_RAND, 0xA311, 1, b"")
    cmd, tid, decoded = parse_frame(ENC_RAND, frame)
    assert (cmd, tid, decoded) == (0xA311, 1, b"")


def test_v0_roundtrip() -> None:
    pt = b"hello hiflow"
    frame = build_frame_v0(SN, 0xA301, 0x0001, pt)
    assert frame.startswith(b"HM\xa3\x01\x00\x01")
    cmd, tid, decoded = parse_frame_v0(SN, frame)
    assert (cmd, tid, decoded) == (0xA301, 0x0001, pt)


def test_v0_padding_block_aligned() -> None:
    # Exactly 16 bytes — PKCS7 must add a full extra block.
    pt = b"sixteenbyteblock"
    frame = build_frame_v0(SN, 0xA301, 0x0001, pt)
    _, _, decoded = parse_frame_v0(SN, frame)
    assert decoded == pt


def test_crc16_modbus_known_vector() -> None:
    # CRC16-Modbus("123456789") = 0x4B37
    assert crc16_modbus(b"123456789") == 0x4B37


def test_key_derivation_stable() -> None:
    # The V1 key is the first 16 bytes of SHA-256³(encRand). Must be stable.
    k = derive_key(ENC_RAND)
    assert k == derive_key(ENC_RAND)
    assert len(k) == 16


def test_nonce_changes_with_cmd_and_tid() -> None:
    a = derive_nonce(ENC_RAND, 0xA311, 1)
    b = derive_nonce(ENC_RAND, 0xA311, 2)
    c = derive_nonce(ENC_RAND, 0xA211, 1)
    assert a != b and a != c and b != c
    assert len(a) == 12


def test_v0_iv_differs_from_key() -> None:
    assert derive_v0_iv(SN, 0xA301, 1) != derive_v0_key(SN)


# ---------- robustness tests ----------

def test_parse_frame_with_wrong_enc_rand_raises_enc_rand_stale() -> None:
    """The most important new behavior: GCM tag mismatch surfaces as
    EncRandStale, which the HA coordinator uses to trigger re-pairing."""
    pt = b"hello world"
    # Build with one key…
    frame = build_frame(ENC_RAND, 0xA311, 1, pt)
    # …decode with another. Decryption must fail with EncRandStale (NOT
    # ValueError, NOT InvalidTag, NOT a generic Exception).
    wrong_key = bytes(16)  # all zeros, definitely different
    with pytest.raises(EncRandStale):
        parse_frame(wrong_key, frame)


def test_parse_frame_crc_mismatch_still_raises_value_error() -> None:
    """CRC mismatch should NOT be classified as EncRandStale — it's a transport
    corruption signal, not a key-rotation signal. Test by corrupting one byte
    of the ciphertext after building."""
    pt = b"hello world"
    frame = bytearray(build_frame(ENC_RAND, 0xA311, 1, pt))
    # Flip a bit in the ciphertext region (offset 10 = start of ct).
    frame[10] ^= 0x01
    with pytest.raises(ValueError, match="crc mismatch"):
        parse_frame(ENC_RAND, bytes(frame))


def test_parse_frame_v0_unchanged_by_enc_rand_path() -> None:
    """V0 (CBC) frames don't have a GCM tag, so the EncRandStale path doesn't
    apply. Sanity-check that the V0 path still works after the V1 changes."""
    pt = b"sn-keyed handshake payload"
    frame = build_frame_v0(SN, 0xA301, 1, pt)
    _, _, decoded = parse_frame_v0(SN, frame)
    assert decoded == pt
