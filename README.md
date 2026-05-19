# hiflow-ble

Python library for talking to the Hoymiles **HiFlow Pro** (HMS-WB-series)
micro-inverter family over Bluetooth Low Energy — fully local, no cloud
round-trip.

The WB-variant of the HMS series uses BLE-only as its local link (the TCP
NettyClient and port 10081 used by other Hoymiles devices are not available on
this hardware). This library is the BLE-equivalent of
[`hoymiles-wifi`](https://github.com/suaveolent/hoymiles-wifi) and mirrors the
same `async_*` method shape so it is straightforward to consume from a Home
Assistant integration.

## Hardware

Designed for the Hoymiles **HMS WB-series** micro-inverters, which advertise
themselves as `RMI-XXXXXXXXXXXX` over BLE. If you have a compatible device,
please open an issue with the model number so we can track confirmed hardware.

## Install

```bash
pip install hiflow-ble
```

## Quick start

```python
import asyncio
from hiflow_ble.hiflow import HiFlow

async def main():
    async with HiFlow("AA:BB:CC:DD:EE:FF", sn="0000000000AA") as hf:
        # First time: extract encRand via the SN-keyed pairing handshake.
        await hf.async_extract_enc_rand()
        # Now we can issue normal (V1) requests.
        real = await hf.async_get_real_data_new()
        print(real)

asyncio.run(main())
```

If you already have `encRand` (it is stable per device flash), pass it directly:

```python
async with HiFlow("AA:BB:CC:DD:EE:FF",
                  enc_rand=bytes.fromhex("<32-hex-char encRand>")) as hf:
    real = await hf.async_get_real_data_new()
```

## CLI

```bash
hiflow-ble RMI-XXXXXXXXXXXX --auto-pair get-real-data-new --as-json
hiflow-ble AA:BB:CC:DD:EE:FF --enc-rand <32-hex-chars> get-real-data-new
```

Run `hiflow-ble --help` for the full command list.

## Notes

- The inverter only accepts **one** BLE central at a time. Close the official
  S-Miles app before connecting — otherwise the device stops advertising and
  scans return nothing.
- Windows/WinRT requires the device to be OS-paired before `start_notify()`
  is allowed. The library calls `BleakClient.pair()` on connect, which is a
  no-op if you are already paired.

## Protocol

AES-128-GCM keyed off a 16-byte per-device secret `encRand`, framed identically
to the Hoymiles TCP protocol (`HM`-magic + big-endian header + ciphertext + GCM
tag). For the gory details see the `crypt_util.py` and `frame.py` modules.
