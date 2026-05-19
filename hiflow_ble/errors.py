"""Exception types for hiflow-ble."""

from __future__ import annotations


class HiFlowError(Exception):
    """Base class for all hiflow-ble errors."""


class BleLinkError(HiFlowError):
    """BLE link could not be established (or was lost and could not be re-established)."""


class EncRandStale(HiFlowError):
    """A V1 frame failed to decrypt — the per-device ``encRand`` session key has
    almost certainly been rotated by the device (factory reset, firmware update,
    or the app re-paired). The caller should run a fresh V0 pairing handshake
    to extract a new ``encRand`` and retry the request."""
