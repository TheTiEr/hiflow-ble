# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

## [0.3.0] - 2026-11-13

### Added
- Recognise serial prefix `0x1620` (HMS-1600/2000-4WB): new `InverterType.FOUR_WB`
  and `InverterPower.P_1600_2000`, so this model no longer resolves to the
  2-input `TWO_WT` type or falls through to `"Unknown"`. (#5)
- `HiFlow(tz_offset=...)` constructor parameter and `local_utc_offset_seconds()`
  helper: the UTC offset sent in protobuf time fields (`request.offset`) is now
  derived from the host's local timezone on every request instead of a fixed
  `OFFSET = 3600` (CET) constant, so it stays correct across DST transitions.
  `async_do_comm_cmd_handshake(tz_offset=...)` default changed from the fixed
  constant to `None` (= use `hf.tz_offset`); existing callers are unaffected. (#4)

### Fixed
- Turn on / turn off / restart / DTU-restart commands now carry the current
  time (`request.time`). The device silently dropped `CommandResDTO` requests
  without a timestamp — no reply, no effect — which is why the control buttons
  did nothing on some units (ha-hiflow-ble#23). (#6)

## [0.2.0] - 2026-07-26

### Fixed
- Add a 60 s cooldown after a BlueZ `NotPermitted` error on `start_notify`.
- Serialise `_ensure_connected` with `_connect_lock` to prevent concurrent
  reconnects from corrupting habluetooth's connect-slot accounting.

## [0.1.0] - 2026-06-13

### Added
- First public release: local BLE communication with Hoymiles HiFlow Pro
  (HMS-\*-WB / HF-\*-WB) microinverters, no cloud/S-Miles account required.
- V0 pairing (SN-keyed AES-128-CBC) to extract the per-device `encRand` key.
- V1 encrypted sessions (AES-128-GCM) for all regular data requests.
- CommCmd handshake (login / PIN / time-sync).
- `RealDataNew` live sensor data, `GetConfig`/`SetConfig`, control commands
  (`set_power_limit`, `turn_on_inverter`, `turn_off_inverter`, `restart_dtu`),
  historical data (`app_get_hist_power`, `app_get_hist_ed`).
- Automatic `encRand` refresh on GCM tag mismatch (`EncRandStale`).
- BlueZ `InProgress` detection with backoff.
- CLI: `hiflow-ble <MAC|RMI-XXX> [--auto-pair|--enc-rand HEX|--extract SN] [--pin PIN] <command>`.

[0.3.0]: https://github.com/TheTiEr/hiflow-ble/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/TheTiEr/hiflow-ble/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/TheTiEr/hiflow-ble/releases/tag/v0.1.0
