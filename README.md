# hiflow-ble

Python library for communicating with **Hoymiles HiFlow Pro** (HMS-\*-WB series)
microinverters over Bluetooth Low Energy — fully local, no cloud round-trip.

The WB variant of the HMS series uses BLE as its only local interface (the TCP
port 10081 used by other Hoymiles devices is not available on this hardware).
This library mirrors the `async_*` API shape of
[hoymiles-wifi](https://github.com/suaveolent/hoymiles-wifi) so it is easy to
consume from a Home Assistant integration or any other async Python project.

---

## Hardware

Designed for the **Hoymiles HMS-\*-WB** microinverters, which advertise over
BLE as `RMI-XXXXXXXXXXXX`.

| Model | BLE name prefix | Serial prefix |
|---|---|---|
| HMS-800-2WB (HiFlow Pro 800) | `RMI-` | `0x1610` |
| HMS-1600-4WB (HiFlow Pro 1600) | `RMI-` | `0x1164` |

Other HMS-WB models likely work. Open an issue with your model and the first
four hex characters of the inverter serial if yours is not listed.

---

## Install

```bash
pip install hiflow-ble
```

Dependencies: [bleak](https://github.com/hbldh/bleak),
[cryptography](https://cryptography.io),
[protobuf](https://github.com/protocolbuffers/protobuf).

In Home Assistant, [bleak-retry-connector](https://github.com/Bluetooth-Devices/bleak-retry-connector)
is also used when available (automatic, no extra install needed).

---

## Quick start

### First-time pairing (no encRand yet)

```python
import asyncio
from hiflow_ble.hiflow import HiFlow, generate_ble_id

async def main():
    ble_id = generate_ble_id()   # stable ID — persist and reuse across sessions

    async with HiFlow("AA:BB:CC:DD:EE:FF", sn="0000000000AA", ble_id=ble_id) as hf:
        # Step 1: V0 handshake — extracts and caches the per-device encRand.
        enc_rand = await hf.async_extract_enc_rand()
        print(f"encRand = {enc_rand.hex()}")  # persist this!

        # Step 2: CommCmd handshake — required before data requests.
        # On first pair, pass your BLE PIN (set in the S-Miles app).
        ok = await hf.async_do_comm_cmd_handshake(ble_id=ble_id, pin="1234")
        if not ok:
            raise RuntimeError("CommCmd handshake failed")

        # Step 3: fetch data.
        real = await hf.async_get_real_data_new()
        print(real)

asyncio.run(main())
```

### Subsequent connections (encRand already known)

```python
async with HiFlow(
    "AA:BB:CC:DD:EE:FF",
    enc_rand=bytes.fromhex("<32-hex-char encRand>"),
    ble_id="<persisted ble_id>",
) as hf:
    await hf.async_do_comm_cmd_handshake()   # bleId already whitelisted — no PIN needed
    real = await hf.async_get_real_data_new()
    print(real)
```

---

## API reference

### `HiFlow(address, *, enc_rand, sn, timeout, max_reconnect_attempts, reconnect_backoff, ble_id, pin)`

Persistent BLE client for a single HiFlow Pro inverter.

| Parameter | Type | Default | Description |
|---|---|---|---|
| `address` | `str` or `BLEDevice` | required | BLE MAC (`"AA:BB:CC:DD:EE:FF"`) or a Bleak `BLEDevice` object. HA passes a `BLEDevice` so traffic is routed through the correct adapter or ESPHome proxy. |
| `enc_rand` | `bytes \| None` | `None` | 16-byte V1 session key. If `None`, call `async_extract_enc_rand()` before any regular request. |
| `sn` | `str \| None` | `None` | 12-char serial tail of the BLE advertisement name (the part after `RMI-`). Required only for the V0 pairing handshake. |
| `timeout` | `int` | `10` | Per-request timeout in seconds. |
| `max_reconnect_attempts` | `int` | `3` | How many times `_ensure_connected` retries before raising `BleLinkError`. |
| `reconnect_backoff` | `float` | `2.0` | Base seconds between retries; doubles each attempt (exponential backoff). |
| `ble_id` | `str` | `""` | The BLE identity string used in the CommCmd handshake. Generate once with `generate_ble_id()` and persist. If empty, one is generated automatically on first use. |
| `pin` | `str` | `""` | BLE PIN set in the S-Miles app. Required on first pairing when `bleId` is not yet whitelisted. Stored so the handshake can reuse it on reconnects if needed. |

Use as an async context manager (`async with HiFlow(...) as hf`) or call
`connect()` / `disconnect()` explicitly.

---

### Lifecycle

#### `await hf.connect()`

Establish the BLE GATT link (single attempt). Idempotent on already-connected
clients. Uses `bleak_retry_connector.establish_connection` when available
(HA/Linux), falls back to plain `BleakClient.connect()`. Calls `pair()` on
Windows before subscribing to notifications (required by WinRT).

#### `await hf.disconnect()`

Tear down the BLE link without raising. Does not remove the OS-level bond.

#### `hf.is_connected` → `bool`

`True` while the underlying `BleakClient` reports connected.

#### `hf.state` → `NetworkState`

Current link state: `NetworkState.Unknown`, `.Online`, or `.Offline`.

---

### Pairing

#### `await hf.async_extract_enc_rand()` → `bytes`

Run the **V0 pairing handshake** and return the 16-byte `encRand`.

This sends a SN-keyed AES-128-CBC `APPInfoData` request to the device and
extracts `encRand` from the `APPDtuInfoMO.enc_rand` field (protobuf field 27).
The result is cached in `hf.enc_rand` and is stable across power cycles — persist
it and pass it to the constructor on subsequent connects.

Requires `sn` to be set. Raises `BleLinkError` on transport failure.

#### `await hf.async_do_comm_cmd_handshake(ble_id="", pin="", tz_offset=3600)` → `bool`

Run the **CommCmd application-layer handshake**. Must be called after every
connect (and after `async_extract_enc_rand`) before the device will respond to
data requests.

Sequence:

| Step | Action code | Description |
|---|---|---|
| 1 | 64 | Login with `bleId`. sts=1 → already whitelisted (skip to 3). sts=3 → unknown bleId (step 2). |
| 2 | 82 | Submit BLE PIN. sts=0 → PIN correct, bleId whitelisted. sts=1 → wrong PIN. |
| 3 | 104 | Time-sync: sends `unix_timestamp,tz_offset_sec`. |

Returns `True` on success, `False` on failure. Sets `hf._handshake_done = True`
and saves the whitelisted `bleId` to `hf.ble_id` on success.

`ble_id` and `pin` override `hf.ble_id` / `hf.pin` for this call.

---

### Data queries

All query methods return a parsed protobuf message or `None` on failure.

| Method | Returns | Description |
|---|---|---|
| `async_get_real_data_new()` | `RealDataNewReqDTO` | Current measurements — AC side (SGSMO), DC ports (PvMO), paged automatically. **Primary data source.** |
| `async_get_real_data()` | `RealDataReqDTO` | Legacy RealData format. |
| `async_get_config()` | `GetConfigReqDTO` | Config registers: power limit (`limit_power_mypower` in tenths of %), grid profile, WiFi mode, etc. |
| `async_network_info()` | `NetworkInfoReqDTO` | BLE/WiFi network info. |
| `async_app_information_data()` | `APPInfoDataReqDTO` | V1-encrypted variant of APPInfo (firmware versions, DTU info). |
| `async_app_get_hist_power()` | `AppGetHistPowerReqDTO` | Historical power curve (paged). |
| `async_app_get_hist_ed()` | `AppGetHistEDReqDTO` | Historical daily energy. |
| `async_get_alarm_list()` | `CommandReqDTO` | Alarm/event list. |
| `async_heartbeat()` | `HBReqDTO` | Heartbeat — keeps the BLE link warm. |

#### `RealDataNew` field map

`RealDataNewReqDTO` has:
- `sgs_data[]` — AC side per inverter (`SGSMO`): `active_power`, `reactive_power`, `voltage`, `frequency`, `current`, `power_factor`, `temperature`, `warning_number`, …
- `pv_data[]` — DC side per port (`PvMO`): `voltage`, `current`, `power`, `energy_total`, `energy_daily`, `error_code`, `port_number`, `serial_number`
- `device_serial_number` — DTU serial

All numeric values use fixed-point encoding; scaling factors vary by field
(e.g. `active_power × 0.1` → Watt, `voltage × 0.1` → Volt,
`current × 0.01` → Ampere).

`error_code = 0` means normal. `0x03000000` (50331648) means no DC input
(night or panels disconnected) — not a hardware fault.

---

### Control

#### `await hf.async_set_power_limit(power_limit: int)` → `CommandReqDTO | None`

Set the output power limit (0–100 %). Sends `A:{percent×10},B:0,C:0` as the
command data payload. The device stores the value as tenths of percent
(`limit_power_mypower`), so 75 % → `750`.

#### `await hf.async_set_wifi(ssid: str, password: str)` → `SetConfigReqDTO | None`

Update WiFi credentials. Reads the current config first, then writes back with
only the WiFi fields changed.

#### `await hf.async_restart_dtu()` → `CommandReqDTO | None`

Reboot the DTU. The BLE link drops during restart.

#### `await hf.async_turn_on_inverter(inverter_serial: str)` → `CommandReqDTO | None`

Re-enable inverter output. `inverter_serial` is the hex-string serial from
`pv_data.serial_number`.

#### `await hf.async_turn_off_inverter(inverter_serial: str)` → `CommandReqDTO | None`

Shut down inverter output.

#### `await hf.async_reboot_inverter(inverter_serial: str)` → `CommandReqDTO | None`

Reboot the inverter microcontroller.

---

### Helper functions

#### `generate_ble_id()` → `str`

Generate a BLE identity string using the same algorithm as the S-Miles app
(`BleIdUtil.b()`): MD5 of `timestamp + UUID4`, hex digits mapped 0–9 via
`% 10`, column-first permutation of 30 slots, first 18 digits as a decimal
integer. Generate once per device and persist the result.

---

## Error handling

```python
from hiflow_ble.errors import HiFlowError, BleLinkError, EncRandStale

try:
    real = await hf.async_get_real_data_new()
except EncRandStale:
    # encRand rotated (factory reset / firmware update).
    # Re-run V0 pairing to get a fresh key.
    enc_rand = await hf.async_extract_enc_rand()
    real = await hf.async_get_real_data_new()
except BleLinkError as e:
    print(f"BLE unreachable: {e}")
```

| Exception | When raised |
|---|---|
| `HiFlowError` | Base class for all library errors. |
| `BleLinkError` | BLE link could not be established after all retry attempts. |
| `EncRandStale` | V1 GCM tag mismatch — `encRand` has been rotated. Re-pair to fix. |

Most transport errors (timeouts, BleakError) are caught internally and return
`None` from the query method rather than raising, to simplify polling loops.
`EncRandStale` is propagated so callers can decide whether to re-pair.

---

## CLI

```
hiflow-ble <target> <--enc-rand HEX | --auto-pair | --extract SN> [command] [args]
```

`target` is a BLE MAC address (`AA:BB:CC:DD:EE:FF`) or an advertisement name
(`RMI-XXXXXXXXXXXX`). When a name is given, the CLI scans for up to 15 seconds.

### Source of encRand (one required)

| Flag | Description |
|---|---|
| `--enc-rand <32 hex chars>` | Use a previously extracted encRand directly. |
| `--auto-pair` | Run V0 pairing to extract encRand, then execute the command. |
| `--extract <SN>` | Run V0 pairing, print encRand, then exit (no command needed). |

### Options

| Flag | Default | Description |
|---|---|---|
| `--sn <12 chars>` | from BLE name | Override the 12-char serial tail. |
| `--timeout <int>` | `10` | Per-request BLE timeout in seconds. |
| `--as-json` | — | Print protobuf responses as JSON. |
| `--verbose` / `-v` | — | Enable debug logging. |

### Commands

| Command | Description |
|---|---|
| `get-real-data-new` | Current measurements (recommended). |
| `get-real-data` | Legacy RealData format. |
| `get-config` | Config registers (power limit, grid profile, …). |
| `network-info` | BLE/WiFi network information. |
| `app-information-data` | DTU/inverter firmware versions. |
| `app-get-hist-power` | Historical power curve. |
| `app-get-hist-ed` | Historical daily energy. |
| `get-alarm-list` | Event/alarm history. |
| `heartbeat` | Send a keepalive frame. |
| `set-power-limit <0-100>` | Set output power limit in percent. |
| `set-wifi <ssid> <password>` | Update WiFi credentials. |
| `restart-dtu` | Reboot the DTU. |
| `turn-on-inverter <serial>` | Re-enable inverter output. |
| `turn-off-inverter <serial>` | Shut down inverter output. |
| `reboot-inverter <serial>` | Reboot the inverter. |

### Examples

```bash
# Extract encRand for the first time
hiflow-ble RMI-XXXXXXXXXXXX --extract XXXXXXXXXXXX

# Current measurements as JSON
hiflow-ble AA:BB:CC:DD:EE:FF --enc-rand <hex32> get-real-data-new --as-json

# Set power limit to 70 %
hiflow-ble AA:BB:CC:DD:EE:FF --enc-rand <hex32> set-power-limit 70

# Full verbose run with auto-pairing
hiflow-ble RMI-XXXXXXXXXXXX --auto-pair get-config --as-json --verbose
```

---

## Protocol

### BLE GATT layout

| Attribute | UUID |
|---|---|
| Service | `0000e0ff-3c17-d293-8e48-14fe2e4da212` |
| TX (write) | `0000ffe1-0000-1000-8000-00805f9b34fb` |
| RX (notify) | `0000ffe2-0000-1000-8000-00805f9b34fb` |
| MTU | 512 bytes (negotiated) |

### Frame format

V0 and V1 share the same 10-byte header:

```
[0:2]    "HM" magic (0x484D)
[2:4]    cmd   — big-endian uint16
[4:6]    tid   — big-endian uint16, monotonic transaction ID
[6:8]    CRC16-Modbus of ciphertext
[8:10]   length = len(ciphertext) + 10
[10:N]   ciphertext
[N:N+16] AES-128-GCM auth tag  (V1 only — V0 frames end at N)
```

### Encryption: V0 (pairing)

Used only for the initial `APPInfoData` exchange to extract `encRand`.

- **Cipher:** AES-128-CBC + PKCS7 padding
- **Key:** `triple-SHA-256(sn.encode() + b"Hoymiles@#123456")[:16]`
- **IV:** `triple-SHA-256(pack(">HH", cmd, tid) + sn.encode())[16:32]`

`sn` is the 12-char serial tail of the BLE name.

### Encryption: V1 (all regular commands)

- **Cipher:** AES-128-GCM
- **Key:** `triple-SHA-256(enc_rand)[:16]`  — static per device
- **Nonce:** `triple-SHA-256(pack("<HH", cmd, tid) + enc_rand)[20:32]`  — per (cmd, tid)
- **AAD:** `pack("<HH", cmd, tid)`

`encRand` is a 16-byte per-device secret embedded in the `APPDtuInfoMO.enc_rand`
field (protobuf field 27) of the V0 `APPInfoData` response. It is stored in
flash and stable across power cycles; it can change after a factory reset or
firmware update (`EncRandStale` is raised in that case).

`triple-SHA-256(b)` means `SHA-256(SHA-256(SHA-256(b)))`.

### Command codes

| Constant | Hex | Direction | Description |
|---|---|---|---|
| `CMD_APP_INFO_DATA_RES_DTO` | `0xA301` | app→device | V0 pairing — fetches `encRand` |
| `CMD_HB_RES_DTO` | `0xA302` | app→device | Heartbeat |
| `CMD_REAL_DATA_RES_DTO` | `0xA303` | app→device | Legacy RealData |
| `CMD_COMMAND_RES_DTO` | `0xA305` | app→device | Control commands |
| `CMD_GET_CONFIG` | `0xA309` | app→device | Read config |
| `CMD_SET_CONFIG` | `0xA310` | app→device | Write config |
| `CMD_REAL_RES_DTO` | `0xA311` | app→device | RealDataNew (paged) |
| `CMD_NETWORK_INFO_RES` | `0xA314` | app→device | Network info |
| `CMD_APP_GET_HIST_POWER_RES` | `0xA315` | app→device | Historical power |
| `CMD_APP_GET_HIST_ED_RES` | `0xA316` | app→device | Historical daily energy |
| `CMD_COMM_CMD_RES_DTO` | `0xA318` | app→device | CommCmd handshake send |
| `CMD_COMM_CMD_STATUS_RES` | `0xA319` | app→device | CommCmd handshake poll |

Device responses arrive on `cmd − 0x0100` (e.g. `0xA211`, `0xA218`).

### Control action codes (inside `CMD_COMMAND_RES_DTO`)

| Constant | Value | Description |
|---|---|---|
| `CMD_ACTION_DTU_REBOOT` | 1 | Reboot DTU |
| `CMD_ACTION_MI_REBOOT` | 3 | Reboot inverter |
| `CMD_ACTION_MI_START` | 6 | Turn on inverter |
| `CMD_ACTION_MI_SHUTDOWN` | 7 | Turn off inverter |
| `CMD_ACTION_LIMIT_POWER` | 8 | Set power limit |

---

## Notes

**One connection at a time.**
The inverter only accepts one BLE central. Close the S-Miles app (force-stop on
Android) before connecting — otherwise the device may not advertise and scans
return nothing.

**Windows pairing.**
WinRT requires the device to be OS-paired before `start_notify()` is allowed.
The library calls `BleakClient.pair()` on Windows at connect time (no-op if
already paired). On Linux/BlueZ, `pair()` is skipped because the inverter rejects
BLE-level bonding with `AuthenticationFailed`.

**BlueZ InProgress.**
A failed connect on Linux can leave BlueZ with a pending `LE_Create_Connection`
for up to 30 seconds. The library detects `InProgress` errors and waits
automatically before retrying.

**Persist `encRand` and `bleId`.**
Both are stable across sessions. Storing them avoids the full V0 pairing
handshake and the PIN prompt on every connect. The HA integration persists them
in the config entry automatically.

---

## Home Assistant integration

[ha-hiflow-ble](https://github.com/TheTiEr/ha-hiflow-ble) is the ready-to-use
HA custom integration built on top of this library.

---

## Contributing

Pull requests welcome. Open an issue first for anything larger than a bug fix.

When reporting a bug please include:
- Python and `bleak` versions
- Inverter model and serial prefix (first 4 hex chars)
- Relevant log output (`logging.basicConfig(level=logging.DEBUG)`)
- Platform (Linux/BlueZ, macOS, Windows)
