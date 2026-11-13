"""Tests for the DST-aware UTC offset sent in protobuf time fields.

The offset must follow the host's local timezone at call time. Hard-coding it
(the previous behaviour, ``OFFSET = 3600``) makes the inverter's clock wrong by
an hour for the whole of CEST, which the device reports back as stale data.

These tests stub ``hiflow.datetime`` rather than touching ``TZ``/``tzset`` so
they are deterministic and portable.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from hiflow_ble import hiflow as hiflow_mod
from hiflow_ble.const import OFFSET
from hiflow_ble.hiflow import HiFlow, local_utc_offset_seconds

ADDRESS = "AA:BB:CC:DD:EE:FF"

CET = timedelta(hours=1)
CEST = timedelta(hours=2)
EDT = timedelta(hours=-4)


class _Moment:
    """Minimal stand-in for an aware ``datetime``."""

    def __init__(self, offset: timedelta | None) -> None:
        self._offset = offset

    def astimezone(self) -> "_Moment":
        return self

    def utcoffset(self) -> timedelta | None:
        return self._offset


class _DateTimeStub:
    """Stand-in for the ``datetime`` class with a fixed local offset."""

    def __init__(self, offset: timedelta | None) -> None:
        self._offset = offset

    def now(self) -> _Moment:
        return _Moment(self._offset)


@pytest.fixture
def local_offset(monkeypatch):
    """Pin the host's apparent local UTC offset."""

    def _set(offset: timedelta | None) -> None:
        monkeypatch.setattr(hiflow_mod, "datetime", _DateTimeStub(offset))

    return _set


@pytest.mark.parametrize(
    ("offset", "expected"),
    [(CET, 3600), (CEST, 7200), (EDT, -14400), (timedelta(0), 0)],
)
def test_local_offset_follows_timezone(local_offset, offset, expected):
    local_offset(offset)
    assert local_utc_offset_seconds() == expected


def test_local_offset_falls_back_when_unavailable(local_offset):
    local_offset(None)
    assert local_utc_offset_seconds() == OFFSET


def test_default_tracks_local_timezone(local_offset):
    hiflow = HiFlow(ADDRESS)

    local_offset(CET)
    assert hiflow.tz_offset == 3600

    # Same instance, after a DST transition — no reconstruction needed.
    local_offset(CEST)
    assert hiflow.tz_offset == 7200


def test_explicit_offset_overrides_local(local_offset):
    local_offset(CEST)
    assert HiFlow(ADDRESS, tz_offset=3600).tz_offset == 3600
    assert HiFlow(ADDRESS, tz_offset=0).tz_offset == 0
