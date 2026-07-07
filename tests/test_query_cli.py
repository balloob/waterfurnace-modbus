"""Light tests for the script/query.py CLI (no real backend needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from modbus_connection.mock import MockModbusUnit

from waterfurnace_modbus import HeatingMode, Series7, SystemOutput

_SPEC = importlib.util.spec_from_file_location(
    "waterfurnace_query", Path(__file__).resolve().parents[1] / "script" / "query.py"
)
assert _SPEC and _SPEC.loader
query = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(query)


def test_format_values() -> None:
    assert query._format(None) == "—"
    assert query._format(HeatingMode.HEAT) == "heat"
    assert query._format(21.5) == "21.5"
    # IntFlag renders the set flags by name.
    outputs = SystemOutput.COMPRESSOR_1 | SystemOutput.BLOWER
    assert query._format(outputs) == "compressor_1|blower"
    assert query._format(SystemOutput(0)) == "none"


def test_parse_args_tcp() -> None:
    args = query._parse_args(["tcp", "1.2.3.4", "--unit", "1"])
    assert args.transport == "tcp"
    assert args.host == "1.2.3.4"
    assert args.unit == 1
    assert args.port == 502
    assert args.framer == "rtu"  # RTU-over-TCP default for Aurora gateways


def test_parse_args_serial() -> None:
    args = query._parse_args(["serial", "/dev/ttyUSB0"])
    assert args.transport == "serial"
    assert args.device == "/dev/ttyUSB0"
    assert args.unit == 1  # Aurora default slave address
    assert args.baudrate == 19200  # Aurora serial default
    assert args.parity == "E"  # 8E1


def test_values_lists_every_subsystem_field(mock_modbus_unit: MockModbusUnit) -> None:
    """Each sub-system's public fields are enumerated, methods excluded."""
    device = Series7(mock_modbus_unit)
    rows = query._values(device.compressor)
    names = {name for name, _value, _unit in rows}
    assert {"speed_actual", "discharge_pressure", "superheat"} <= names
    # Methods / private helpers are not data rows.
    assert "async_update" not in names
    assert all(not n.startswith("_") for n in names)
    # Framework internals (public in modbus-connection >= 3.3) must not leak in.
    assert names.isdisjoint(
        {"register_items", "bit_items", "register_ranges", "max_span", "max_gap"}
    )


def test_print_runs(
    capsys: pytest.CaptureFixture[str], mock_modbus_unit: MockModbusUnit
) -> None:
    device = Series7(mock_modbus_unit)
    query._print(device)
    out = capsys.readouterr().out
    assert "Device" in out
    assert "Compressor" in out
    assert "Thermostat" in out
