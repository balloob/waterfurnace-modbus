#!/usr/bin/env python3
"""Query a WaterFurnace Aurora over Modbus and print every value.

Connects over Modbus TCP (a serial gateway) or a serial/USB port, reads the whole
device once, and dumps every sub-system's values to the terminal. Handy for
checking a real unit without Home Assistant.

The Aurora ABC speaks Modbus RTU on RS-485 at 19200 8E1, unit address 1. Over a
network it is almost always an RTU-over-TCP (transparent serial) gateway, so the
``tcp`` transport defaults to the ``rtu`` framer.

The library only needs the connection *protocol*; this script picks the pymodbus
backend, so run it with the ``cli`` extra::

    uv run --extra cli python script/query.py tcp 192.168.1.50 --unit 1
    uv run --extra cli python script/query.py serial /dev/ttyUSB0 --unit 1
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import sys
import time
from enum import Flag, IntEnum
from typing import cast

from modbus_connection import ModbusConnection, ModbusError, ModbusUnit
from modbus_connection.model import Component, RegisterField

from waterfurnace_modbus import Series7

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

# Names carried by the framework base (register_items, ranges, max_span, write, …);
# a sub-system's own data fields never collide with these, so skipping them keeps
# the dump to real device values.
_FRAMEWORK_NAMES = frozenset(dir(Component))


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="transport", required=True)

    # Shared options available on each transport (so `--unit` can follow the host).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--unit",
        type=int,
        default=1,
        help="Modbus unit/station address (default: 1)",
    )

    tcp = sub.add_parser(
        "tcp", parents=[common], help="connect over Modbus TCP (network gateway)"
    )
    tcp.add_argument("host", help="hostname or IP of the gateway/device")
    tcp.add_argument("--port", type=int, default=502, help="TCP port (default: 502)")
    tcp.add_argument(
        "--framer",
        choices=("rtu", "socket"),
        default="rtu",
        help=(
            "wire framing: 'rtu' for RTU-over-TCP (transparent serial gateways, "
            "the Aurora default) or 'socket' for native Modbus TCP (default: rtu)"
        ),
    )

    serial = sub.add_parser(
        "serial", parents=[common], help="connect over a serial/USB port"
    )
    serial.add_argument("device", help="serial device, e.g. /dev/ttyUSB0")
    serial.add_argument("--baudrate", type=int, default=19200, help="default: 19200")
    serial.add_argument("--parity", choices=("N", "E", "O"), default="E")
    serial.add_argument("--stopbits", type=int, choices=(1, 2), default=1)
    serial.add_argument("--bytesize", type=int, choices=(7, 8), default=8)

    return parser.parse_args(argv)


async def _open(args: argparse.Namespace) -> ModbusConnection:
    # Imported here so the module loads (and --help works) without a backend.
    from modbus_connection.pymodbus import connect_serial, connect_tcp

    if args.transport == "serial":
        return await connect_serial(
            args.device,
            baudrate=args.baudrate,
            parity=args.parity,
            stopbits=args.stopbits,
            bytesize=args.bytesize,
        )
    return await connect_tcp(args.host, port=args.port, framer=args.framer)


class _CountingUnit:
    """Wraps a ModbusUnit to count the Modbus reads it performs."""

    def __init__(self, unit: ModbusUnit) -> None:
        self._unit = unit
        self.reads = 0

    async def read_input_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_input_registers(address, count)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.reads += 1
        return await self._unit.read_holding_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_coils(address, count)

    async def read_discrete_inputs(self, address: int, count: int) -> list[bool]:
        self.reads += 1
        return await self._unit.read_discrete_inputs(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._unit, name)


def _format(value: object) -> str:
    if value is None:
        return "—"
    if isinstance(value, Flag):
        set_flags = [f.name.lower() for f in type(value) if f.name and f in value]
        return "|".join(set_flags) if set_flags else "none"
    if isinstance(value, IntEnum):
        return value.name.lower()
    return str(value)


def _values(component: Component) -> list[tuple[str, str, str]]:
    """Public (name, value, unit) rows for a sub-system, in declaration order."""
    rows: list[tuple[str, str, str]] = []
    cls = type(component)
    for name in dir(component):
        if name.startswith("_") or name in _FRAMEWORK_NAMES:
            continue
        static = inspect.getattr_static(cls, name, None)
        # Skip methods/coroutines; keep RegisterField descriptors, properties,
        # and plain class constants (e.g. manufacturer).
        if callable(static) and not isinstance(static, property):
            continue
        value = getattr(component, name)
        if callable(value):
            continue
        unit = static.unit or "" if isinstance(static, RegisterField) else ""
        rows.append((name, _format(value), unit))
    return rows


def _print_section(label: str, rows: list[tuple[str, str, str]]) -> None:
    print(f"\n{label}")
    print("-" * len(label))
    width = max((len(name) for name, _, _ in rows), default=0)
    for name, value, unit in rows:
        suffix = f" {unit}" if unit and value != "—" else ""
        print(f"  {name:<{width}}  {value}{suffix}")


def _print(device: Series7) -> None:
    for label, attr in SECTIONS:
        _print_section(label, _values(getattr(device, attr)))
    # Only print zones the unit actually has.
    zone_count = device.config.number_of_zones or 0
    for index, zone in enumerate(device.zones[:zone_count], start=1):
        _print_section(f"Zone {index}", _values(zone))


async def _run(args: argparse.Namespace) -> int:
    try:
        connection = await _open(args)
    except ModbusError as err:
        print(f"Could not connect: {err}", file=sys.stderr)
        return 1
    counting = _CountingUnit(connection.for_unit(args.unit))
    try:
        device = Series7(cast(ModbusUnit, counting))
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
