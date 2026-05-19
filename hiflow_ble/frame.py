"""Frame builders/parsers for the HiFlow BLE protocol.

Both V0 and V1 share the same on-wire header layout::

    [0:2]   "HM" magic (0x484D)
    [2:4]   cmd  (big-endian uint16)
    [4:6]   tid  (big-endian uint16, monotonic transaction id)
    [6:8]   CRC16-Modbus of the ciphertext
    [8:10]  length = len(ciphertext) + 10  (excludes the 16-byte tag for V1)
    [10:N]  ciphertext
    [N:N+16] AES-128-GCM auth tag  (V1 only — V0 frames stop at N)
"""

from __future__ import annotations

import struct

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.padding import PKCS7

from .crypt_util import (
    aad,
    derive_key,
    derive_nonce,
    derive_v0_iv,
    derive_v0_key,
)
from .errors import EncRandStale


# ---------- CRC16 Modbus ----------

_CRC_TABLE: list[int] = []


def crc16_modbus(data: bytes) -> int:
    """CRC16-Modbus, poly 0xA001, init 0xFFFF, no final XOR."""
    if not _CRC_TABLE:
        for byte in range(256):
            c = byte
            for _ in range(8):
                c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
            _CRC_TABLE.append(c)
    crc = 0xFFFF
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xFF]
    return crc & 0xFFFF


# ---------- V1 (encRand-keyed AES-128-GCM) ----------

def build_frame(enc_rand: bytes, cmd: int, tid: int, plaintext: bytes) -> bytes:
    """Build a V1 (encRand AES-128-GCM) request frame."""
    ct_and_tag = AESGCM(derive_key(enc_rand)).encrypt(
        derive_nonce(enc_rand, cmd, tid), plaintext, aad(cmd, tid)
    )
    ct, tag = ct_and_tag[:-16], ct_and_tag[-16:]
    crc = crc16_modbus(ct)
    header = b"HM" + struct.pack(
        ">HHHH", cmd & 0xFFFF, tid & 0xFFFF, crc, len(ct) + 10
    )
    return header + ct + tag


def parse_frame(enc_rand: bytes, buf: bytes) -> tuple[int, int, bytes]:
    """Parse a V1 (encRand AES-128-GCM) response frame.

    Returns (cmd, tid, plaintext).
    """
    if buf[0:2] != b"HM":
        raise ValueError(f"bad magic: {buf[:2].hex()}")
    cmd, tid, crc, length = struct.unpack(">HHHH", buf[2:10])
    ct_len = length - 10
    ct = buf[10 : 10 + ct_len]
    tag = buf[10 + ct_len : 10 + ct_len + 16]
    if crc16_modbus(ct) != crc:
        raise ValueError(
            f"crc mismatch: got {crc16_modbus(ct):04x}, frame says {crc:04x}"
        )
    try:
        pt = AESGCM(derive_key(enc_rand)).decrypt(
            derive_nonce(enc_rand, cmd, tid), ct + tag, aad(cmd, tid)
        )
    except InvalidTag as e:
        # GCM authenticator rejected the ciphertext: either someone is
        # tampering with the frame or — far more likely in practice — the
        # device rotated its encRand and our cached key is stale.
        raise EncRandStale(
            f"V1 GCM tag mismatch on cmd=0x{cmd:04x} tid={tid} — encRand may have rotated"
        ) from e
    return cmd, tid, pt


# ---------- V0 (SN-keyed AES-128-CBC) — pairing only ----------

def build_frame_v0(sn: str, cmd: int, tid: int, plaintext: bytes) -> bytes:
    """Build a V0 (SN-keyed AES-128-CBC) request frame."""
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    enc = Cipher(
        algorithms.AES(derive_v0_key(sn)), modes.CBC(derive_v0_iv(sn, cmd, tid))
    ).encryptor()
    ct = enc.update(padded) + enc.finalize()
    crc = crc16_modbus(ct)
    return (
        b"HM"
        + struct.pack(">HHHH", cmd & 0xFFFF, tid & 0xFFFF, crc, len(ct) + 10)
        + ct
    )


def parse_frame_v0(sn: str, buf: bytes) -> tuple[int, int, bytes]:
    """Parse a V0 (SN-keyed AES-128-CBC) response frame.

    Returns (cmd, tid, plaintext).
    """
    if buf[0:2] != b"HM":
        raise ValueError(f"bad magic: {buf[:2].hex()}")
    cmd, tid, crc, length = struct.unpack(">HHHH", buf[2:10])
    ct = buf[10 : 10 + length - 10]
    if crc16_modbus(ct) != crc:
        raise ValueError(
            f"crc mismatch: got {crc16_modbus(ct):04x}, frame says {crc:04x}"
        )
    dec = Cipher(
        algorithms.AES(derive_v0_key(sn)), modes.CBC(derive_v0_iv(sn, cmd, tid))
    ).decryptor()
    padded = dec.update(ct) + dec.finalize()
    unpadder = PKCS7(128).unpadder()
    pt = unpadder.update(padded) + unpadder.finalize()
    return cmd, tid, pt
