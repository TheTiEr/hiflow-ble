"""Control commands must carry the current time.

The device silently ignores a CommandResDTO without ``time``: no reply, no
effect. With the current time, turn on/off switched an HMS-2000-4WB within
10 s (see ha-hiflow-ble#23).
"""

from __future__ import annotations

import asyncio
import time

import pytest

from hiflow_ble.const import (
    CMD_ACTION_DTU_REBOOT,
    CMD_ACTION_MI_REBOOT,
    CMD_ACTION_MI_SHUTDOWN,
    CMD_ACTION_MI_START,
    CMD_COMMAND_RES_DTO,
)
from hiflow_ble.hiflow import HiFlow

# Synthetic serial — not a real device.
INVERTER_SERIAL = "1620A0000001"


def _sent_request(method_name: str, *args):
    hf = HiFlow("AA:BB:CC:DD:EE:FF")
    sent = []

    async def fake_send(command, request, response_type):
        sent.append((command, request))
        return None

    hf.async_send_request = fake_send
    asyncio.run(getattr(hf, method_name)(*args))
    assert len(sent) == 1
    return sent[0]


@pytest.mark.parametrize(
    ("method_name", "action"),
    [
        ("async_turn_on_inverter", CMD_ACTION_MI_START),
        ("async_turn_off_inverter", CMD_ACTION_MI_SHUTDOWN),
        ("async_reboot_inverter", CMD_ACTION_MI_REBOOT),
    ],
)
def test_inverter_action_carries_time(method_name: str, action: int) -> None:
    before = int(time.time())
    command, request = _sent_request(method_name, INVERTER_SERIAL)
    assert command == CMD_COMMAND_RES_DTO
    assert request.action == action
    assert before <= request.time <= int(time.time())
    assert list(request.mi_to_sn) == [int(INVERTER_SERIAL, 16)]


def test_restart_dtu_carries_time() -> None:
    before = int(time.time())
    command, request = _sent_request("async_restart_dtu")
    assert command == CMD_COMMAND_RES_DTO
    assert request.action == CMD_ACTION_DTU_REBOOT
    assert before <= request.time <= int(time.time())
