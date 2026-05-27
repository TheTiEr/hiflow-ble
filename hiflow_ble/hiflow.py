"""HiFlow BLE client.

Public API mirrors ``hoymiles_wifi.dtu.DTU``: every ``async_*`` method returns
the same protobuf response message types so downstream code (e.g. the Home
Assistant integration) can be written against the familiar shape.

Two important differences vs. DTU:

1. **Persistent connection.** DTU opens a fresh TCP socket per request. BLE
   GATT setup costs ~5 s, so we keep one ``BleakClient`` open for the
   instance's lifetime. Use ``HiFlow`` as an async context manager, or call
   :meth:`connect` / :meth:`disconnect` explicitly.

2. **V0 pairing path.** Before encrypted (V1) requests work, the device's
   ``encRand`` session key must be extracted via the SN-keyed handshake. Call
   :meth:`async_extract_enc_rand` once; the result is stable across power
   cycles and can be persisted by the caller.
"""

from __future__ import annotations

import asyncio
import hashlib as _hashlib
import struct
import sys
import time
import uuid as _uuid_mod
from datetime import datetime
from typing import Any

from bleak import BleakClient
from bleak.exc import BleakError

from . import logger
from .errors import BleLinkError
from .const import (
    CMD_APP_INFO_DATA_RES_DTO,
    CMD_APP_GET_HIST_ED_RES,
    CMD_APP_GET_HIST_POWER_RES,
    CMD_COMMAND_RES_DTO,
    CMD_COMM_CMD_RES_DTO,
    CMD_COMM_CMD_STATUS_RES,
    CMD_GET_CONFIG,
    CMD_HB_RES_DTO,
    CMD_NETWORK_INFO_RES,
    CMD_REAL_DATA_RES_DTO,
    CMD_REAL_RES_DTO,
    CMD_SET_CONFIG,
    CMD_ACTION_DTU_REBOOT,
    CMD_ACTION_LIMIT_POWER,
    CMD_ACTION_MI_REBOOT,
    CMD_ACTION_MI_SHUTDOWN,
    CMD_ACTION_MI_START,
    DEFAULT_MTU,
    DEFAULT_TIMEOUT,
    DEV_DTU,
    OFFSET,
    RX_UUID,
    TX_UUID,
    V0_CMDS,
)
from .frame import build_frame, build_frame_v0, parse_frame, parse_frame_v0
from .hoymiles import NetworkState, convert_inverter_serial_number
from .protobuf import (
    AlarmData_pb2,
    APPHeartbeatPB_pb2,
    APPInfomationData_pb2,
    AppGetHistED_pb2,
    AppGetHistPower_pb2,
    CommandPB_pb2,
    GetConfig_pb2,
    NetworkInfo_pb2,
    RealDataNew_pb2,
    RealData_pb2,
    SetConfig_pb2,
)


def _bcmd_to_int(b: bytes) -> int:
    """Big-endian 2-byte command bytes → int."""
    return struct.unpack(">H", b)[0]


def _now_pb_time() -> bytes:
    """Datetime in the wire format used by every Hoymiles request: bytes."""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S").encode("utf-8")


# ---------- tiny protobuf decoder (only needed during V0 pairing) ----------

def _read_varint(data: bytes, off: int) -> tuple[int, int]:
    n, shift = 0, 0
    while True:
        b = data[off]
        off += 1
        n |= (b & 0x7F) << shift
        if not (b & 0x80):
            break
        shift += 7
    return n, off


def _parse_pb(data: bytes) -> dict[int, list]:
    out: dict[int, list] = {}
    off = 0
    while off < len(data):
        tag, off = _read_varint(data, off)
        field, wire = tag >> 3, tag & 7
        if wire == 0:
            v, off = _read_varint(data, off)
        elif wire == 2:
            n, off = _read_varint(data, off)
            v = data[off : off + n]
            off += n
        elif wire == 1:
            v = data[off : off + 8]
            off += 8
        elif wire == 5:
            v = data[off : off + 4]
            off += 4
        else:
            raise ValueError(f"unsupported wire type {wire}")
        out.setdefault(field, []).append(v)
    return out


def _extract_enc_rand_from_appinfo(pt: bytes) -> bytes:
    """Walk APPInfoDataReqDTO → MAPPDtuInfo (field 8) → encRand (field 27)."""
    fields = _parse_pb(pt)
    if 8 not in fields:
        raise ValueError(f"no MAPPDtuInfo (field 8) in APP_INFO response: {pt.hex()}")
    sub = _parse_pb(fields[8][0])
    if 27 not in sub:
        raise ValueError(f"no encRand (field 27) in MAPPDtuInfo: {fields[8][0].hex()}")
    enc_rand = sub[27][0]
    if len(enc_rand) != 16:
        raise ValueError(f"encRand has wrong length {len(enc_rand)}: {enc_rand.hex()}")
    return bytes(enc_rand)


# ---------- CommCmd handshake helpers ----------
# The device requires an application-layer handshake (action=64 login, optionally
# action=82 PIN pairing, action=104 time-sync) before it will accept V1 data
# requests.  We encode these frames by hand using minimal protobuf since the
# CommCmd types aren't covered by the pre-compiled .proto files.

def _pb_varint_encode(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)

def _pb_varint_field(field: int, n: int) -> bytes:
    return bytes([(field << 3) | 0]) + _pb_varint_encode(n)

def _pb_string_field(field: int, s: bytes | str) -> bytes:
    if isinstance(s, str):
        s = s.encode()
    return bytes([(field << 3) | 2]) + _pb_varint_encode(len(s)) + s

def _build_comm_cmd_res(action: int, data: str | bytes = "") -> bytes:
    """CommCmdResDTO wire encoding: field 1=time, 2=action, 5=tid, 6=data."""
    t = int(time.time())
    payload = _pb_varint_field(1, t) + _pb_varint_field(2, action) + _pb_varint_field(5, t)
    payload += _pb_string_field(6, data)
    return payload

def _build_comm_cmd_status_res(action: int) -> bytes:
    """CommCmdStatusResDTO wire encoding: field 1=time, 2=action, 4=tid."""
    t = int(time.time())
    return _pb_varint_field(1, t) + _pb_varint_field(2, action) + _pb_varint_field(4, t)

def _get_action_sts(pt: bytes) -> tuple[int, int]:
    """Parse (action, sts) from CommCmdStatusReqDTO device response.

    Fields: 3=action (varint), 11=sts (varint).
    sts semantics differ per action:
      action=64: 0=in-progress, 1=OK, 3=PIN-needed
      action=82: 0=SUCCESS,     1=wrong-PIN
      action=104: 0=OK
    """
    fields = _parse_pb(pt)
    action = fields[3][0] if 3 in fields else 0
    sts = fields[11][0] if 11 in fields else 0
    return int(action), int(sts)


def generate_ble_id() -> str:
    """Generate a BLE identity string using the same algorithm as the S-Miles app.

    Replicates ``com.hoymiles.utils.BleIdUtil.b()`` exactly:
      1. raw  = str(currentTimeMillis) + str(randomUUID)
      2. md5  = MD5(raw.encode("utf-8"))  → 32 hex chars
      3. digits = [int(c, 16) % 10 for c in md5]   # map hex→0-9
      4. apply column-first permutation of 30 slots
      5. take first 18 digits → Long.parseLong() (drops leading zeros)

    The result is a 17-18 digit decimal string suitable as a stable bleId.
    """
    ts = int(time.time() * 1000)
    uid = str(_uuid_mod.uuid4())
    md5_hex = _hashlib.md5(f"{ts}{uid}".encode("utf-8")).hexdigest()
    digits = [int(c, 16) % 10 for c in md5_hex]  # 32 digits
    PERM = [
        0, 5, 10, 15, 20, 25,
        1, 6, 11, 16, 21, 26,
        2, 7, 12, 17, 22, 27,
        3, 8, 13, 18, 23, 28,
        4, 9, 14, 19, 24, 29,
    ]
    permuted = [digits[i] for i in PERM]
    return str(int("".join(str(d) for d in permuted[:18])))


# ---------- main class ----------

class HiFlow:
    """Persistent BLE client for a single HiFlow Pro inverter."""

    def __init__(
        self,
        address,
        enc_rand: bytes | None = None,
        sn: str | None = None,
        timeout: int = DEFAULT_TIMEOUT,
        max_reconnect_attempts: int = 3,
        reconnect_backoff: float = 2.0,
        ble_id: str = "",
        pin: str = "",
    ):
        """Initialize.

        Args:
            address: BLE MAC address (``"AA:BB:CC:DD:EE:FF"``) **or** a
                ``BLEDevice`` instance (Home Assistant provides the latter via
                ``bluetooth.async_ble_device_from_address``, which keeps the
                connection routed through whichever adapter actually sees the
                device — including ESPHome Bluetooth proxies).
            enc_rand: 16-byte V1 session key. If ``None``, you must call
                :meth:`async_extract_enc_rand` after :meth:`connect` before any
                regular request will work.
            sn: 12-char serial tail of the BLE advertisement name (the bit
                after ``RMI-``). Required only for the V0 pairing handshake;
                can be inferred from the BLE name.
            timeout: per-request timeout (seconds).
            max_reconnect_attempts: how many times :meth:`_ensure_connected`
                will retry before giving up.
            reconnect_backoff: base seconds between retries; doubles each attempt.
            ble_id: the bleId string generated by the S-Miles app algorithm.
                If empty, :meth:`async_do_comm_cmd_handshake` generates and
                stores one automatically (so it's the same across reconnects).
            pin: BLE PIN set by the user in the S-Miles app.  Required when the
                bleId is not yet in the device's whitelist (i.e. first pairing).
                Stored here so :meth:`async_do_comm_cmd_handshake` can fall back
                to the PIN step on every reconnect if needed.
        """
        self.address = address
        self.enc_rand = enc_rand
        self.sn = sn
        self.timeout = timeout
        self._max_reconnect_attempts = max_reconnect_attempts
        self._reconnect_backoff = reconnect_backoff
        self.ble_id: str = ble_id       # persisted across sessions; generated on first use
        self.pin: str = pin             # user's custom BLE PIN (for whitelist-add)

        self.state: NetworkState = NetworkState.Unknown
        self._tid = 1
        self._mutex = asyncio.Lock()
        self._client: BleakClient | None = None
        self._rx_buf = bytearray()
        self._rx_event = asyncio.Event()
        self._handshake_done: bool = False  # reset on every disconnect

    # ---------- state ----------

    def get_state(self) -> NetworkState:
        return self.state

    def set_state(self, new_state: NetworkState) -> None:
        if self.state != new_state:
            self.state = new_state
            logger.debug("HiFlow link state: %s", new_state)

    @property
    def is_connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    # ---------- lifecycle ----------

    def _on_disconnect(self, _client) -> None:
        """Bleak disconnect callback — fires when the device drops us.

        Runs synchronously on the event loop; we keep it side-effect-light:
        mark offline, wake any waiter, and detach the client so the next call
        triggers a fresh ``connect()``.
        """
        logger.info("HiFlow: BLE link dropped (disconnected_callback)")
        self.set_state(NetworkState.Offline)
        self._handshake_done = False   # must re-handshake on next connect
        # Unblock any pending notify-waiter so the request fails fast instead
        # of timing out — the request-side code checks the buffer afterwards.
        self._rx_event.set()
        self._client = None

    async def connect(self) -> None:
        """Establish the BLE link (single attempt).

        Idempotent on already-connected clients. Use :meth:`_ensure_connected`
        for retry-with-backoff behavior.

        When ``bleak_retry_connector`` is installed (standard in Home Assistant),
        uses :func:`bleak_retry_connector.establish_connection` for reliable
        connection establishment (platform-specific retries, timeout tuning).
        Falls back to plain ``BleakClient.connect()`` for standalone use.
        """
        if self.is_connected:
            return

        # _connected is set by whichever path succeeds; the other paths raise.
        _connected: BleakClient | None = None

        # ── Try bleak-retry-connector first (HA / Linux path) ──────────────
        # When installed (standard in HA), establish_connection handles platform-
        # specific retries and proper timeout tuning — avoids the BlueZ warning.
        try:
            from bleak_retry_connector import establish_connection as _ec  # type: ignore[import]
            from bleak.backends.device import BLEDevice  # type: ignore[import]

            if isinstance(self.address, BLEDevice):
                name = self.address.name or str(self.address.address)
                _connected = await _ec(
                    BleakClient,
                    self.address,
                    name,
                    disconnected_callback=self._on_disconnect,
                    max_attempts=self._max_reconnect_attempts,
                )
        except ImportError:
            pass  # bleak-retry-connector not installed — use plain connect below
        except Exception:
            self.set_state(NetworkState.Offline)
            raise

        # ── Plain BleakClient.connect() fallback (standalone / string address) ──
        if _connected is None:
            _plain = BleakClient(self.address, disconnected_callback=self._on_disconnect)
            try:
                await _plain.connect(timeout=self.timeout * 2)
            except Exception:
                self.set_state(NetworkState.Offline)
                raise
            _connected = _plain

        client: BleakClient = _connected  # always non-None here: all other paths raise

        # pair() is required on Windows/WinRT before CCCD writes (start_notify).
        # On Linux/BlueZ the inverter rejects BLE-level bonding (AuthenticationFailed),
        # which causes BlueZ to silently drop the connection — skip it on non-Windows.
        if sys.platform == "win32":
            try:
                await client.pair()
            except Exception as e:
                logger.debug("pair() failed: %s", e)

        try:
            await client.exchange_mtu(DEFAULT_MTU)
        except Exception:
            pass

        try:
            await client.start_notify(RX_UUID, self._on_notify)
        except Exception:
            # Could not subscribe to notifications — link is useless without it.
            try:
                await client.disconnect()
            except Exception:
                pass
            self.set_state(NetworkState.Offline)
            raise

        self._client = client
        self.set_state(NetworkState.Online)

    async def _ensure_connected(self) -> None:
        """Connect with exponential backoff.

        Raises :class:`hiflow_ble.errors.BleLinkError` if every attempt fails.
        Always tears down any stale client before retrying so we don't trust a
        BleakClient whose ``is_connected`` flag has gone out of sync.
        """
        if self.is_connected:
            return
        await self._safe_disconnect()
        last_err: Exception | None = None
        for attempt in range(self._max_reconnect_attempts):
            try:
                await self.connect()
                return
            except Exception as e:
                last_err = e
                logger.debug(
                    "HiFlow connect attempt %d/%d failed: %s",
                    attempt + 1, self._max_reconnect_attempts, e,
                )
                if attempt < self._max_reconnect_attempts - 1:
                    await asyncio.sleep(self._reconnect_backoff * (2 ** attempt))
        raise BleLinkError(
            f"could not establish BLE link after {self._max_reconnect_attempts} attempts: {last_err}"
        )

    async def _safe_disconnect(self) -> None:
        """Tear down ``self._client`` without raising. Sets state to Offline."""
        if self._client is None:
            self.set_state(NetworkState.Offline)
            return
        client = self._client
        self._client = None
        try:
            try:
                await client.stop_notify(RX_UUID)
            except Exception:
                pass
            await client.disconnect()
        except Exception as e:
            logger.debug("HiFlow disconnect raised (ignored): %s", e)
        finally:
            self.set_state(NetworkState.Offline)

    async def disconnect(self) -> None:
        """Public alias for :meth:`_safe_disconnect`.

        Kept for API parity with ``async with HiFlow(...) as hf`` lifecycles —
        and so external callers don't have to know about the underscore.
        Does not unpair the OS-level bond (we want fast reconnects).
        """
        await self._safe_disconnect()

    async def __aenter__(self) -> HiFlow:
        await self._ensure_connected()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._safe_disconnect()

    # ---------- low-level transport ----------

    def _on_notify(self, _char, data: bytearray) -> None:
        """Aggregate BLE notify chunks until a full HM frame is buffered."""
        self._rx_buf.extend(data)
        if len(self._rx_buf) >= 10 and self._rx_buf[:2] == b"HM":
            cmd = struct.unpack(">H", self._rx_buf[2:4])[0]
            length = struct.unpack(">H", self._rx_buf[8:10])[0]
            # V1 frames carry an extra 16-byte tag; V0 frames don't.
            need = length if cmd in V0_CMDS else length + 16
            if len(self._rx_buf) >= need:
                self._rx_event.set()

    async def async_send_request(
        self,
        command: bytes,
        request: Any,
        response_type: Any,
    ):
        """Send a request, await a response, return parsed protobuf or ``None``.

        Reconnect-with-backoff is applied transparently — callers don't need to
        manage the BLE lifecycle. If decrypt fails with an authenticator-tag
        mismatch (almost certainly a rotated ``encRand``), the underlying
        :class:`hiflow_ble.errors.EncRandStale` is **propagated** so the caller
        can re-run the V0 handshake and retry. All other failure modes return
        ``None``.

        ``command`` is a 2-byte BE command code (matches the ``CMD_*`` constants).
        For commands in ``V0_CMDS`` the SN-keyed CBC path is used; everything
        else is encRand-keyed AES-128-GCM (V1).
        """
        try:
            await self._ensure_connected()
        except BleLinkError as e:
            logger.debug("HiFlow.async_send_request: BLE unavailable: %s", e)
            return None

        cmd_int = _bcmd_to_int(command)
        is_v0 = cmd_int in V0_CMDS

        if is_v0:
            if not self.sn:
                logger.error("SN required for V0 (pairing) requests")
                return None
        elif not self.enc_rand:
            logger.error("encRand required for V1 requests — call async_extract_enc_rand first")
            return None

        async with self._mutex:
            tid = self._tid
            self._tid = (self._tid + 1) & 0x7FFF
            payload = request.SerializeToString()
            if is_v0:
                frame = build_frame_v0(self.sn, cmd_int, tid, payload)
            else:
                frame = build_frame(self.enc_rand, cmd_int, tid, payload)

            self._rx_buf.clear()
            self._rx_event.clear()
            try:
                await self._client.write_gatt_char(TX_UUID, frame, response=True)
                await asyncio.wait_for(self._rx_event.wait(), timeout=self.timeout)
            except (asyncio.TimeoutError, BleakError, EOFError) as e:
                logger.debug("HiFlow BLE transport error: %s", e)
                # Tear down so the next call reconnects fresh — Bleak's
                # ``is_connected`` flag is unreliable after partial failures.
                await self._safe_disconnect()
                return None
            except Exception as e:
                # Catch-all for whatever the underlying BLE backend may throw:
                # WinRT errors, dbus errors, etc. Treat all of them as "link dead".
                logger.debug("HiFlow unexpected transport error: %s", e)
                await self._safe_disconnect()
                return None

            buf = bytes(self._rx_buf)

        # The disconnect callback fires ``_rx_event.set()`` to unblock us if
        # the device drops mid-request. The buffer will be empty / lack the
        # HM magic in that case.
        if len(buf) < 10 or buf[:2] != b"HM":
            logger.debug("HiFlow: no/short frame after wait (link likely dropped)")
            await self._safe_disconnect()
            return None

        # parse_frame raises EncRandStale on GCM tag mismatch — propagate so
        # the caller can run V0 re-pairing. Other errors (CRC mismatch, PKCS7
        # unpad failure) bubble up unchanged.
        if is_v0:
            _cmd, _tid, pt = parse_frame_v0(self.sn, buf)
        else:
            _cmd, _tid, pt = parse_frame(self.enc_rand, buf)

        try:
            parsed = response_type.FromString(pt)
            if not parsed:
                raise ValueError("empty parse result")
        except Exception as e:
            logger.debug("Protobuf parse failed: %s", e)
            self.set_state(NetworkState.Unknown)
            return None

        self.set_state(NetworkState.Online)
        return parsed

    # ---------- pairing ----------

    async def async_extract_enc_rand(self) -> bytes:
        """Run the V0 pairing handshake and return (and cache) ``encRand``.

        Connects (or reconnects with backoff) automatically if the link is
        down. Requires ``self.sn`` to be set — derive it from the BLE name
        first if needed.

        Raises :class:`hiflow_ble.errors.BleLinkError` if the link cannot be
        established. Other exceptions (parse failure, missing field) propagate
        unchanged.
        """
        if not self.sn:
            raise RuntimeError("SN required — set self.sn from the BLE name first")
        await self._ensure_connected()

        request = APPInfomationData_pb2.APPInfoDataResDTO()
        request.time_ymd_hms = _now_pb_time()
        request.offset = OFFSET
        request.time = int(time.time())
        # Custom decode: the response contains encRand which we need before
        # the standard FromString decode is meaningful. Walk raw bytes.
        async with self._mutex:
            tid = self._tid
            self._tid = (self._tid + 1) & 0x7FFF
            frame = build_frame_v0(
                self.sn, _bcmd_to_int(CMD_APP_INFO_DATA_RES_DTO), tid,
                request.SerializeToString(),
            )
            self._rx_buf.clear()
            self._rx_event.clear()
            try:
                await self._client.write_gatt_char(TX_UUID, frame, response=True)
                await asyncio.wait_for(self._rx_event.wait(), timeout=self.timeout)
            except (asyncio.TimeoutError, BleakError, Exception) as e:
                logger.debug("V0 pairing transport error: %s", e)
                await self._safe_disconnect()
                raise BleLinkError(f"V0 pairing transport failed: {e}") from e
            buf = bytes(self._rx_buf)
        if len(buf) < 10 or buf[:2] != b"HM":
            await self._safe_disconnect()
            raise BleLinkError("V0 pairing: no/short response (link dropped?)")
        _cmd, _tid, pt = parse_frame_v0(self.sn, buf)
        self.enc_rand = _extract_enc_rand_from_appinfo(pt)
        logger.info("Extracted encRand: %s", self.enc_rand.hex())
        return self.enc_rand

    # ---------- CommCmd handshake ----------

    async def _raw_request(
        self, cmd_int: int, raw_payload: bytes, timeout: float | None = None
    ) -> bytes | None:
        """Send a V1 (encRand-keyed) raw frame; return the raw decrypted payload.

        Unlike :meth:`async_send_request` this skips protobuf serialisation/
        deserialisation — used by the CommCmd handshake whose message types are
        not covered by the compiled ``.proto`` files.

        Returns ``None`` on any transport, encryption, or parse failure.
        """
        if not self.enc_rand:
            logger.error("_raw_request: encRand not set — call async_extract_enc_rand first")
            return None
        try:
            await self._ensure_connected()
        except BleLinkError as e:
            logger.debug("_raw_request: BLE unavailable: %s", e)
            return None

        t = timeout if timeout is not None else float(self.timeout)
        async with self._mutex:
            tid = self._tid
            self._tid = (self._tid + 1) & 0x7FFF
            frame = build_frame(self.enc_rand, cmd_int, tid, raw_payload)
            self._rx_buf.clear()
            self._rx_event.clear()
            try:
                await self._client.write_gatt_char(TX_UUID, frame, response=True)
                await asyncio.wait_for(self._rx_event.wait(), timeout=t)
            except (asyncio.TimeoutError, BleakError, Exception) as e:
                logger.debug("_raw_request transport error (cmd=0x%04x): %s", cmd_int, e)
                await self._safe_disconnect()
                return None
            buf = bytes(self._rx_buf)

        if len(buf) < 10 or buf[:2] != b"HM":
            logger.debug("_raw_request: no/short frame (link likely dropped)")
            await self._safe_disconnect()
            return None
        try:
            _cmd, _tid, pt = parse_frame(self.enc_rand, buf)
            return pt
        except Exception as e:
            logger.debug("_raw_request: frame parse failed: %s", e)
            return None

    async def async_do_comm_cmd_handshake(
        self,
        ble_id: str = "",
        pin: str = "",
        tz_offset: int = OFFSET,
    ) -> bool:
        """Run the CommCmd application-layer handshake.

        The device requires this sequence after every BLE (re-)connect before
        it will respond to V1 data requests:

          1. CommCmdResDTO(action=64, data=bleId)    → CMD 0xA318
          2. CommCmdStatusResDTO(action=64)           → CMD 0xA319, poll
             sts=1: bleId in whitelist → skip to step 3
             sts=3: unknown bleId → step 2b (PIN needed)
          2b. CommCmdResDTO(action=82, data=PIN)     → CMD 0xA318
          2c. CommCmdStatusResDTO(action=82)          → CMD 0xA319, poll
              sts=0: PIN correct, bleId whitelisted
              sts=1: wrong PIN
          3. CommCmdResDTO(action=104, data=time)     → CMD 0xA318
          4. CommCmdStatusResDTO(action=104)           → CMD 0xA319

        sts semantics are action-specific:
          action=64:  0=in-progress, 1=OK, 3=PIN-needed
          action=82:  0=SUCCESS,     1=wrong-PIN
          action=104: 0=OK

        Arguments override ``self.ble_id`` / ``self.pin`` for this call; the
        first successful ``ble_id`` is saved to ``self.ble_id`` so that
        subsequent reconnects reuse the same (already-whitelisted) identity.

        Returns ``True`` if handshake completed, ``False`` on failure (caller
        can decide whether to abort or continue attempting data requests).
        Sets ``self._handshake_done = True`` on success.
        """
        if not self.enc_rand:
            logger.error("CommCmd handshake: encRand not set")
            return False

        # Determine which bleId and PIN to use; generate bleId if none known yet.
        used_id = ble_id or self.ble_id
        if not used_id:
            used_id = generate_ble_id()
            logger.debug("CommCmd handshake: generated new bleId=%s", used_id)
        used_pin = pin or self.pin

        cmd_res = int.from_bytes(CMD_COMM_CMD_RES_DTO, "big")     # 0xA318
        cmd_sts = int.from_bytes(CMD_COMM_CMD_STATUS_RES, "big")  # 0xA319

        # ── Step 1: action=64 login ─────────────────────────────────────────
        logger.debug("CommCmd handshake: TX action=64 bleId=%s", used_id)
        if await self._raw_request(cmd_res, _build_comm_cmd_res(64, used_id)) is None:
            logger.warning("CommCmd handshake: action=64 send failed")
            return False

        login_sts = -1
        for _ in range(5):
            pt = await self._raw_request(cmd_sts, _build_comm_cmd_status_res(64))
            if pt is None:
                break
            _, sts = _get_action_sts(pt)
            logger.debug("CommCmd login poll sts=%d", sts)
            if sts == 0:        # in-progress for action=64
                await asyncio.sleep(1.0)
                continue
            login_sts = sts
            break

        # ── Step 2b: action=82 PIN if bleId not whitelisted ─────────────────
        if login_sts == 3:
            if not used_pin:
                logger.warning(
                    "CommCmd handshake: device returned sts=3 (PIN needed) but "
                    "no PIN is configured. Set pin= or HiFlow.pin."
                )
            else:
                logger.debug("CommCmd handshake: sts=3 → TX action=82 PIN")
                if await self._raw_request(cmd_res, _build_comm_cmd_res(82, used_pin)) is None:
                    logger.warning("CommCmd handshake: action=82 send failed")
                    return False
                for _ in range(8):
                    pt = await self._raw_request(cmd_sts, _build_comm_cmd_status_res(82))
                    if pt is None:
                        break
                    _, sts = _get_action_sts(pt)
                    logger.debug("CommCmd pairing poll sts=%d", sts)
                    if sts == 0:    # SUCCESS for action=82
                        login_sts = 1   # treat as "authenticated"
                        # Save bleId — it's now whitelisted on the device.
                        self.ble_id = used_id
                        break
                    if sts == 1:    # wrong PIN for action=82
                        logger.error(
                            "CommCmd handshake: wrong PIN (action=82 sts=1). "
                            "Check the BLE PIN configured for this device."
                        )
                        break
        elif login_sts == 1:
            # bleId already in whitelist (normal case on reconnect).
            self.ble_id = used_id

        if login_sts != 1:
            logger.debug(
                "CommCmd handshake: login_sts=%d — proceeding to time-sync anyway",
                login_sts,
            )

        # ── Step 3+4: action=104 time-sync ──────────────────────────────────
        ts = int(time.time())
        time_data = f"{ts},{tz_offset}\r"
        logger.debug("CommCmd handshake: TX action=104 time-sync")
        if await self._raw_request(cmd_res, _build_comm_cmd_res(104, time_data)) is None:
            logger.warning("CommCmd handshake: action=104 send failed")
            return False
        pt = await self._raw_request(cmd_sts, _build_comm_cmd_status_res(104))
        if pt is not None:
            _, sts = _get_action_sts(pt)
            logger.debug("CommCmd time-sync sts=%d", sts)

        self._handshake_done = True
        logger.info("CommCmd handshake complete (bleId=%s, login_sts=%d)", used_id, login_sts)
        return True

    # ---------- data queries ----------

    async def async_get_real_data(self) -> RealData_pb2.RealDataReqDTO | None:
        """Get real data (legacy RealData)."""
        request = RealData_pb2.RealDataResDTO()
        request.time_ymd_hms = _now_pb_time()
        request.time = int(time.time())
        request.offset = OFFSET
        request.error_code = 0
        return await self.async_send_request(
            CMD_REAL_DATA_RES_DTO, request, RealData_pb2.RealDataReqDTO,
        )

    async def async_get_real_data_new(self) -> RealDataNew_pb2.RealDataNewReqDTO | None:
        """Get real data (modern RealDataNew). Combines paged responses."""
        combined = RealDataNew_pb2.RealDataNewReqDTO()
        request = RealDataNew_pb2.RealDataNewResDTO()
        request.time_ymd_hms = _now_pb_time()
        request.offset = OFFSET
        request.time = int(time.time())
        request.cp = 0
        response = await self.async_send_request(
            CMD_REAL_RES_DTO, request, RealDataNew_pb2.RealDataNewReqDTO,
        )
        if response is None:
            return None
        combined.MergeFrom(response)
        for cp in range(1, response.ap):
            request.cp = cp
            additional = await self.async_send_request(
                CMD_REAL_RES_DTO, request, RealDataNew_pb2.RealDataNewReqDTO,
            )
            if additional is not None:
                combined.MergeFrom(additional)
        return combined if combined.ByteSize() > 0 else None

    async def async_get_config(self) -> GetConfig_pb2.GetConfigReqDTO | None:
        """Get config (power limit, grid profile, etc.)."""
        request = GetConfig_pb2.GetConfigResDTO()
        request.offset = OFFSET
        request.time = int(time.time()) - 60
        return await self.async_send_request(
            CMD_GET_CONFIG, request, GetConfig_pb2.GetConfigReqDTO,
        )

    async def async_network_info(self) -> NetworkInfo_pb2.NetworkInfoReqDTO | None:
        """Get network info."""
        request = NetworkInfo_pb2.NetworkInfoResDTO()
        request.offset = OFFSET
        request.time = int(time.time())
        return await self.async_send_request(
            CMD_NETWORK_INFO_RES, request, NetworkInfo_pb2.NetworkInfoReqDTO,
        )

    async def async_app_information_data(
        self,
    ) -> APPInfomationData_pb2.APPInfoDataReqDTO | None:
        """Get app information data (encrypted V1 variant once paired)."""
        request = APPInfomationData_pb2.APPInfoDataResDTO()
        request.time_ymd_hms = _now_pb_time()
        request.offset = OFFSET
        request.time = int(time.time())
        # NOTE: the V1-encrypted variant uses a different command code from the
        # V0 pairing handshake. The wifi library uses CMD_APP_INFO_DATA_RES_DTO
        # for both, but for BLE we only ever issue the V0 one during pairing.
        # Most data we'd need is already in RealDataNew anyway.
        return await self.async_send_request(
            CMD_APP_INFO_DATA_RES_DTO,
            request,
            APPInfomationData_pb2.APPInfoDataReqDTO,
        )

    async def async_app_get_hist_power(
        self,
    ) -> AppGetHistPower_pb2.AppGetHistPowerReqDTO | None:
        """Get historical power. Combines paged responses."""
        combined = AppGetHistPower_pb2.AppGetHistPowerReqDTO()
        request = AppGetHistPower_pb2.AppGetHistPowerResDTO()
        request.cp = 0
        request.offset = OFFSET
        request.requested_time = int(time.time())
        request.requested_day = 0
        response = await self.async_send_request(
            CMD_APP_GET_HIST_POWER_RES,
            request,
            AppGetHistPower_pb2.AppGetHistPowerReqDTO,
        )
        if response is None:
            return None
        initial_absolute_start = response.absolute_start
        combined.MergeFrom(response)
        for cp in range(1, response.ap):
            request.cp = cp
            additional = await self.async_send_request(
                CMD_APP_GET_HIST_POWER_RES,
                request,
                AppGetHistPower_pb2.AppGetHistPowerReqDTO,
            )
            if additional is not None:
                combined.MergeFrom(additional)
        combined.absolute_start = initial_absolute_start
        return combined if combined.ByteSize() > 0 else None

    async def async_app_get_hist_ed(
        self,
    ) -> AppGetHistED_pb2.AppGetHistEDReqDTO | None:
        """Get historical energy daily."""
        request = AppGetHistED_pb2.AppGetHistEDResDTO()
        request.offset = OFFSET
        request.time = int(time.time())
        return await self.async_send_request(
            CMD_APP_GET_HIST_ED_RES, request, AppGetHistED_pb2.AppGetHistEDReqDTO,
        )

    async def async_get_alarm_list(self) -> CommandPB_pb2.CommandReqDTO | None:
        """Request the inverter's alarm list."""
        request = CommandPB_pb2.CommandResDTO()
        # CMD_ACTION_ALARM_LIST = 50 (see hoymiles_wifi const.py).
        request.action = 50
        request.package_nub = 1
        request.dev_kind = 0
        request.tid = int(time.time())
        return await self.async_send_request(
            CMD_COMMAND_RES_DTO, request, CommandPB_pb2.CommandReqDTO,
        )

    async def async_heartbeat(self) -> APPHeartbeatPB_pb2.HBReqDTO | None:
        """Send a heartbeat (helps keep the BLE link warm)."""
        request = APPHeartbeatPB_pb2.HBResDTO()
        request.time_ymd_hms = _now_pb_time()
        request.offset = OFFSET
        request.time = int(time.time())
        return await self.async_send_request(
            CMD_HB_RES_DTO, request, APPHeartbeatPB_pb2.HBReqDTO,
        )

    # ---------- control ----------

    async def async_set_power_limit(
        self, power_limit: int
    ) -> CommandPB_pb2.CommandReqDTO | None:
        """Set DTU/inverter power limit (0–100 %)."""
        if power_limit < 0 or power_limit > 100:
            logger.error("Invalid power limit: %s", power_limit)
            return None
        request = CommandPB_pb2.CommandResDTO()
        request.time = int(time.time())
        request.action = CMD_ACTION_LIMIT_POWER
        request.package_nub = 1
        request.tid = int(time.time())
        request.data = f"A:{power_limit * 10},B:0,C:0\r".encode()
        return await self.async_send_request(
            CMD_COMMAND_RES_DTO, request, CommandPB_pb2.CommandReqDTO,
        )

    async def async_set_wifi(
        self, ssid: str, password: str
    ) -> SetConfig_pb2.SetConfigReqDTO | None:
        """Update the inverter's WiFi credentials.

        Reads the current config first (so we don't blank out unrelated fields),
        then mutates the wifi-related ones and writes back.
        """
        current = await self.async_get_config()
        if current is None:
            logger.error("Failed to get config — cannot update WiFi")
            return None
        request = SetConfig_pb2.SetConfigResDTO()
        # Carry over every scalar field from the current config so we only
        # overwrite the ones we care about. Protobuf MergeFrom handles that.
        request.CopyFrom(SetConfig_pb2.SetConfigResDTO())
        for f in current.DESCRIPTOR.fields:
            if not f.message_type:
                value = getattr(current, f.name)
                try:
                    setattr(request, f.name, value)
                except Exception:
                    pass
        request.time = int(time.time())
        request.offset = OFFSET
        request.app_page = 1
        request.netmode_select = 1  # WIFI
        request.wifi_ssid = ssid.encode("utf-8")
        request.wifi_password = password.encode("utf-8")
        return await self.async_send_request(
            CMD_SET_CONFIG, request, SetConfig_pb2.SetConfigReqDTO,
        )

    async def async_restart_dtu(self) -> CommandPB_pb2.CommandReqDTO | None:
        """Reboot the DTU (BLE link will drop)."""
        request = CommandPB_pb2.CommandResDTO()
        request.action = CMD_ACTION_DTU_REBOOT
        request.package_nub = 1
        request.tid = int(time.time())
        return await self.async_send_request(
            CMD_COMMAND_RES_DTO, request, CommandPB_pb2.CommandReqDTO,
        )

    async def async_turn_on_inverter(
        self, inverter_serial: str
    ) -> CommandPB_pb2.CommandReqDTO | None:
        """Turn on the inverter."""
        return await self._send_inverter_action(inverter_serial, CMD_ACTION_MI_START)

    async def async_turn_off_inverter(
        self, inverter_serial: str
    ) -> CommandPB_pb2.CommandReqDTO | None:
        """Turn off the inverter."""
        return await self._send_inverter_action(inverter_serial, CMD_ACTION_MI_SHUTDOWN)

    async def async_reboot_inverter(
        self, inverter_serial: str
    ) -> CommandPB_pb2.CommandReqDTO | None:
        """Reboot the inverter."""
        return await self._send_inverter_action(inverter_serial, CMD_ACTION_MI_REBOOT)

    async def _send_inverter_action(
        self, inverter_serial: str, action: int
    ) -> CommandPB_pb2.CommandReqDTO | None:
        inverter_serial_int = convert_inverter_serial_number(inverter_serial)
        request = CommandPB_pb2.CommandResDTO()
        request.action = action
        request.package_nub = 1
        request.dev_kind = DEV_DTU
        request.tid = int(time.time())
        request.mi_to_sn.extend([inverter_serial_int])
        return await self.async_send_request(
            CMD_COMMAND_RES_DTO, request, CommandPB_pb2.CommandReqDTO,
        )
