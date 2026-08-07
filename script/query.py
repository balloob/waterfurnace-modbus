#!/usr/bin/env python3
"""Query a WaterFurnace Aurora over Modbus and print every value.

Connects over Modbus TCP (a serial gateway) or a serial/USB port, reads the whole
device once, and dumps every sub-system's values to the terminal. Handy for
checking a real unit without Home Assistant.

The Aurora ABC speaks Modbus RTU on RS-485 at 19200 8E1, unit address 1. Over a
network it is almost always an RTU-over-TCP (transparent serial) gateway, so the
``tcp`` transport defaults to the ``rtu`` framer.

The library only needs the connection *protocol*; this script needs a concrete
backend, so run it with the ``cli`` extra::

    uv run --extra cli python script/query.py 192.168.1.50 --unit 1
    uv run --extra cli python script/query.py /dev/ttyUSB0 --transport serial
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time

from modbus_connection import ModbusError
from modbus_connection.cli_helper import (
    CountingUnit,
    add_connection_args,
    connect_from_args,
    print_component,
)

from waterfurnace_modbus import Series7

# The connections the Aurora is reachable over: an RTU-over-TCP gateway (the
# usual network path), native Modbus TCP for the rare socket-framing gateway,
# and the RS-485 line itself. UDP and TLS are not paths this device offers.
CONNECTIONS = (("tcp", "rtu"), ("tcp", "socket"), ("serial", "rtu"))

# (label, attribute name on Series7) — the order things are printed.
SECTIONS: list[tuple[str, str]] = [
    ("Device", "info"),
    ("Configuration", "config"),
    ("Peripherals", "peripherals"),
    ("Status", "status"),
    ("Sensors", "sensors"),
    ("Compressor", "compressor"),
    ("Blower", "blower"),
    ("Pump", "pump"),
    ("Hot water", "dhw"),
    ("Thermostat", "thermostat"),
    ("Humidistat", "humidistat"),
    ("Energy", "energy"),
    ("Dealer", "dealer"),
]


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        epilog=(
            "Defaults are the Aurora's: RTU framing over TCP, and 19200 8E1 on "
            "serial, at unit address 1."
        ),
    )
    add_connection_args(parser, connections=CONNECTIONS)
    # The unit id is not part of connecting, so it sits outside that group.
    parser.add_argument(
        "--unit",
        type=int,
        default=1,
        help="Modbus unit/station address (default: 1)",
    )
    # add_connection_args carries the generic Modbus defaults (socket framing,
    # 9600 8N1); a parser-level default overrides an argument-level one, so this
    # states the Aurora's line settings without restating its arguments.
    parser.set_defaults(framer="rtu", baudrate=19200, parity="E")
    return parser.parse_args(argv)


def _print(device: Series7) -> None:
    for label, attr in SECTIONS:
        print()
        print_component(getattr(device, attr), title=label)
    # Only print zones the unit actually has.
    zone_count = device.config.number_of_zones or 0
    for index, zone in enumerate(device.zones[:zone_count], start=1):
        print()
        print_component(zone, title=f"Zone {index}")


async def _run(args: argparse.Namespace) -> int:
    # A connection connects on demand, so this could go straight to the read.
    # This one-shot dump connects eagerly anyway, to tell a connect failure apart
    # from a device that answers but refuses a read.
    try:
        connection = await connect_from_args(args)
    except ModbusError as err:
        print(f"Could not connect: {err}", file=sys.stderr)
        return 1
    counting = CountingUnit(connection.for_unit(args.unit))
    try:
        device = Series7(counting)
        start = time.monotonic()
        await device.async_update()
        elapsed = time.monotonic() - start
    except ModbusError as err:
        print(f"Error reading device: {err}", file=sys.stderr)
        return 1
    finally:
        await connection.close()
    _print(device)
    print(f"\nQueried in {elapsed * 1000:.0f} ms ({counting.reads} Modbus reads)")
    return 0


def main() -> int:
    return asyncio.run(_run(_parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
