"""Model-name resolution from the inverter serial number.

The HiFlow Pro family shares series byte 0x16. The second byte carries the
MPPT count, so 0x1620 (HMS-1600/2000-4WB) must not be reported as a 2-input
model — and must not fall through to "Unknown", which the caller renders as
the device model in Home Assistant while the library logs a warning for every
entity it builds.
"""

from __future__ import annotations

import logging

import pytest

from hiflow_ble.hoymiles import (
    InverterPower,
    InverterType,
    get_inverter_model_name,
    get_inverter_power,
    get_inverter_type,
)

# Serial tails are synthetic; only the first two bytes are significant here.
HIFLOW_PRO_4WB = "1620A031169F"
HIFLOW_PRO_2WB = "1610A03116AA"


def test_4wb_prefix_resolves_to_model_name():
    assert get_inverter_model_name(HIFLOW_PRO_4WB) == "HMS-1600/2000-4WB"


def test_4wb_prefix_is_a_four_input_model():
    serial_bytes = bytes.fromhex(HIFLOW_PRO_4WB)
    assert get_inverter_type(serial_bytes) is InverterType.FOUR_WB
    assert get_inverter_power(serial_bytes) is InverterPower.P_1600_2000


def test_4wb_prefix_does_not_warn(caplog):
    with caplog.at_level(logging.WARNING):
        get_inverter_model_name(HIFLOW_PRO_4WB)
    assert caplog.records == []


@pytest.mark.parametrize(
    ("serial", "expected"),
    [
        (HIFLOW_PRO_2WB, "HMS-600/700/800-2WT"),
        ("1164A03116AA", "HMS-1600/1800/2000-4T"),
        ("1141A03116AA", "HM-600/700/800-2T"),
    ],
)
def test_known_prefixes_are_unchanged(serial, expected):
    """Guard against the 0x16 branch changing existing devices."""
    assert get_inverter_model_name(serial) == expected


def test_unknown_prefix_still_degrades_gracefully():
    assert get_inverter_model_name("9999A03116AA") == "Unknown"
