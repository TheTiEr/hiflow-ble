"""HiFlow / Hoymiles BLE protocol crypto.

The wire protocol has two modes:

* **V0** (SN-keyed AES-128-CBC + PKCS7) — used only for the initial
  pairing handshake (``CMD_APP_INFO_DATA_REQ_DTO`` / ``…_RES_DTO``). The
  device-side key is derived from the 12-character serial number (the tail of
  the BLE advertisement name ``RMI-XXXXXXXXXXXX``) and the fixed salt
  ``SALT_V0``.

* **V1** (encRand-keyed AES-128-GCM) — used for every regular command once
  ``encRand`` is known. ``encRand`` is a 16-byte per-device secret that the
  device emits inside the ``APPInfoDataReqDTO`` reply to the V0 handshake. It
  is flash-fixed and stable across power cycles.

Both key/nonce derivations share a triple-SHA-256 helper (``_sha3``).
"""

from __future__ import annotations

import hashlib
import struct

from .const import SALT_V0


def _sha3(b: bytes) -> bytes:
    """SHA-256 applied three times in sequence."""
    return hashlib.sha256(
        hashlib.sha256(hashlib.sha256(b).digest()).digest()
    ).digest()


# ---------- V1: encRand-keyed AES-128-GCM ----------

def derive_key(enc_rand: bytes) -> bytes:
    """16-byte AES-128 key. Static per device."""
    assert len(enc_rand) == 16
    return _sha3(enc_rand)[:16]


def derive_nonce(enc_rand: bytes, cmd: int, tid: int) -> bytes:
    """12-byte GCM nonce. Per (cmd, tid) pair."""
    return _sha3(struct.pack("<HH", cmd, tid) + enc_rand)[20:32]


def aad(cmd: int, tid: int) -> bytes:
    """4-byte AAD: cmd_LE || tid_LE."""
    return struct.pack("<HH", cmd, tid)


# ---------- V0: SN-keyed AES-128-CBC ----------

def derive_v0_key(sn: str) -> bytes:
    """16-byte AES-128 key. ``sn`` is the 12-char serial tail of the BLE name."""
    return _sha3(sn.encode() + SALT_V0)[:16]


def derive_v0_iv(sn: str, cmd: int, tid: int) -> bytes:
    """16-byte CBC IV. Per (cmd, tid) pair."""
    return _sha3(struct.pack(">HH", cmd, tid) + sn.encode())[16:32]
