"""CLI for hiflow-ble.

Usage::

    hiflow-ble <MAC|RMI-XXX> --auto-pair [--pin 1234] get-real-data-new --as-json
    hiflow-ble <MAC|RMI-XXX> --enc-rand <hex32> get-real-data-new
    hiflow-ble <MAC|RMI-XXX> --extract <SN_12chars> [--pin 1234]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

from bleak import BleakScanner
from google.protobuf.json_format import MessageToJson

from .hiflow import HiFlow, generate_ble_id

COMMANDS = [
    "get-real-data",
    "get-real-data-new",
    "get-config",
    "network-info",
    "app-information-data",
    "app-get-hist-power",
    "app-get-hist-ed",
    "get-alarm-list",
    "heartbeat",
    "set-power-limit",
    "set-wifi",
    "restart-dtu",
    "turn-on-inverter",
    "turn-off-inverter",
    "reboot-inverter",
]


def _command_to_method(cmd: str) -> str:
    return "async_" + cmd.replace("-", "_")


async def _resolve_address(target: str) -> tuple[str, str | None]:
    """Resolve a 'RMI-XXX' name to a MAC. Returns (mac, sn|None)."""
    if ":" in target:
        return target, None
    print(f"Scanning for {target} ...", file=sys.stderr)
    dev = await BleakScanner.find_device_by_name(target, timeout=15)
    if not dev:
        print(f"Device {target!r} not found", file=sys.stderr)
        sys.exit(1)
    sn = None
    if target.startswith(("RMI-", "MI-", "MSA-", "RMSA-")):
        sn = target.split("-", 1)[1][-12:].upper()
    print(f"Found at {dev.address}" + (f", SN={sn}" if sn else ""), file=sys.stderr)
    return dev.address, sn


def _format_response(resp: Any, as_json: bool) -> str:
    if resp is None:
        return "<no response>"
    if as_json:
        return MessageToJson(resp, preserving_proto_field_name=True)
    return str(resp)


async def _main_async() -> None:
    parser = argparse.ArgumentParser(prog="hiflow-ble")
    parser.add_argument("target", help="BLE MAC (XX:XX:..) or advertisement name (RMI-XXX)")
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--enc-rand", help="hex encRand (32 hex chars)")
    src.add_argument("--auto-pair", action="store_true",
                     help="extract encRand via V0 handshake first, then run command")
    src.add_argument("--extract", metavar="SN",
                     help="run V0 pairing only and print encRand, then exit")
    parser.add_argument("--pin", default="",
                        help="BLE PIN set in the S-Miles app (required on first pairing "
                             "when the bleId is not yet whitelisted on the device)")
    parser.add_argument("--sn", help="12-char serial tail (overrides name-derived SN)")
    parser.add_argument("--timeout", type=int, default=10)
    parser.add_argument("--as-json", action="store_true", help="print responses as JSON")
    parser.add_argument("--verbose", "-v", action="store_true")
    parser.add_argument("command", nargs="?", choices=COMMANDS,
                        help="action to perform after connect")
    parser.add_argument("args", nargs=argparse.REMAINDER,
                        help="positional args for the chosen command")
    ns = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if ns.verbose else logging.INFO)

    address, sn_from_name = await _resolve_address(ns.target)
    sn = ns.sn or sn_from_name
    if ns.extract:
        sn = ns.extract.upper()

    enc_rand = bytes.fromhex(ns.enc_rand) if ns.enc_rand else None
    ble_id = generate_ble_id()

    async with HiFlow(
        address,
        enc_rand=enc_rand,
        sn=sn,
        timeout=ns.timeout,
        ble_id=ble_id,
        pin=ns.pin,
    ) as hf:
        if ns.auto_pair or ns.extract:
            if not sn:
                print("Need SN: use --sn or scan by RMI-XXX name", file=sys.stderr)
                sys.exit(1)
            er = await hf.async_extract_enc_rand()
            print(f"encRand = {er.hex()}", file=sys.stderr)
            if ns.extract:
                return

        # Run CommCmd handshake before any data command — required after every connect.
        if ns.command or ns.auto_pair:
            ok = await hf.async_do_comm_cmd_handshake(ble_id=ble_id, pin=ns.pin)
            if not ok:
                print(
                    "CommCmd handshake failed. If this is the first pairing, "
                    "pass the BLE PIN via --pin. Make sure the S-Miles app is closed.",
                    file=sys.stderr,
                )
                sys.exit(1)

        if not ns.command:
            print("No command given — pass one of:", ", ".join(COMMANDS), file=sys.stderr)
            sys.exit(1)

        method_name = _command_to_method(ns.command)
        method = getattr(hf, method_name)
        # Cast remaining positional args heuristically: power_limit → int, serials → str.
        call_args = list(ns.args)
        if ns.command == "set-power-limit":
            call_args = [int(call_args[0])]
        elif ns.command == "set-wifi":
            if len(call_args) < 2:
                print("set-wifi needs <ssid> <password>", file=sys.stderr)
                sys.exit(1)
        resp = await method(*call_args)
        print(_format_response(resp, ns.as_json))


def run_main() -> None:
    try:
        asyncio.run(_main_async())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    run_main()
