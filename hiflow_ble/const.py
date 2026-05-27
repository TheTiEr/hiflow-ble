"""Constants for the HiFlow BLE integration."""

# ---------- BLE GATT layout ----------
SERVICE_UUID = "0000e0ff-3c17-d293-8e48-14fe2e4da212"
TX_UUID = "0000ffe1-0000-1000-8000-00805f9b34fb"
RX_UUID = "0000ffe2-0000-1000-8000-00805f9b34fb"

DEFAULT_MTU = 512
DEFAULT_TIMEOUT = 10

# ---------- Frame ----------
CMD_HEADER = b"HM"

# Same command space as hoymiles-wifi TCP (see hoymiles_wifi/const.py).
# Stored as bytes (big-endian) for parity with the upstream library.
CMD_APP_INFO_DATA_REQ_DTO = b"\xa2\x01"
CMD_APP_INFO_DATA_RES_DTO = b"\xa3\x01"
CMD_HB_REQ_DTO = b"\xa3\x02"
CMD_HB_RES_DTO = b"\xa3\x02"
CMD_REAL_DATA_RES_DTO = b"\xa3\x03"
CMD_COMMAND_RES_DTO = b"\xa3\x05"
CMD_GET_CONFIG = b"\xa3\x09"
CMD_SET_CONFIG = b"\xa3\x10"
CMD_REAL_RES_DTO = b"\xa3\x11"
CMD_NETWORK_INFO_RES = b"\xa3\x14"
CMD_APP_GET_HIST_POWER_RES = b"\xa3\x15"
CMD_APP_GET_HIST_ED_RES = b"\xa3\x16"

# Action codes used inside CMD_COMMAND_RES_DTO payloads.
CMD_ACTION_DTU_REBOOT = 1
CMD_ACTION_MI_REBOOT = 3
CMD_ACTION_MI_START = 6
CMD_ACTION_MI_SHUTDOWN = 7
CMD_ACTION_LIMIT_POWER = 8

# CommCmd application-layer handshake commands (post-V0, V1-encrypted).
# Sent after encRand is known; required before the device will accept data cmds.
# S-Miles app: ClientConstants.q0 (CMD_COMM_CMD_RES_DTO) / r0 (CMD_COMM_CMD_STATUS_RES).
CMD_COMM_CMD_RES_DTO    = b"\xa3\x18"   # 0xA318  CommCmdResDTO   (app→device)
CMD_COMM_CMD_STATUS_RES = b"\xa3\x19"   # 0xA319  CommCmdStatusResDTO (app→device)
# Device responds on (cmd − 0x0100): 0xA218 / 0xA219 respectively.

# Commands that travel on the V0 (SN-keyed AES-128-CBC) path: only the initial
# pairing handshake. Everything else is V1 (encRand-keyed AES-128-GCM with a
# 16-byte trailing tag).
V0_CMDS = {
    int.from_bytes(CMD_APP_INFO_DATA_REQ_DTO, "big"),
    int.from_bytes(CMD_APP_INFO_DATA_RES_DTO, "big"),
    0x8901,
    0x7901,
}

# 16-byte salt mixed with the device serial for V0 key derivation.
SALT_V0 = b"Hoymiles@#123456"

# Time offset (seconds) used in protobuf time fields. CET/CEST = 3600/7200.
# hoymiles-wifi hard-codes 28800 (CST); we default to CET because the primary
# target audience is Europe-based HiFlow Pro owners.
OFFSET = 3600

# Max power-limit percentage accepted by the device.
MAX_POWER_LIMIT = 100

# Device type bytes (subset of hoymiles-wifi; only what HiFlow needs).
DEV_DTU = 1
DEV_MICRO = 3
DEV_INV = 6
