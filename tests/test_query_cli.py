"""Light tests for the script/query.py CLI (no real backend needed)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from modbus_connection.mock import MockModbusUnit

from waterfurnace_modbus import Series7

from .conftest import HOLDING

_SPEC = importlib.util.spec_from_file_location(
    "waterfurnace_query", Path(__file__).resolve().parents[1] / "script" / "query.py"
)
assert _SPEC and _SPEC.loader
query = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(query)


def test_parse_args_tcp() -> None:
    args = query._parse_args(["1.2.3.4", "--unit", "1"])
    assert args.transport == "tcp"  # the default transport
    assert args.target == "1.2.3.4"
    assert args.unit == 1
    assert args.port is None  # unset: the backend's 502
    assert args.framer == "rtu"  # RTU-over-TCP default for Aurora gateways


def test_parse_args_serial() -> None:
    args = query._parse_args(["/dev/ttyUSB0", "--transport", "serial"])
    assert args.transport == "serial"
    assert args.target == "/dev/ttyUSB0"
    assert args.unit == 1  # Aurora default slave address
    assert args.baudrate == 19200  # Aurora serial default
    assert args.parity == "E"  # 8E1
    assert args.framer == "rtu"


def test_parse_args_rejects_a_transport_the_aurora_does_not_offer() -> None:
    """Only the RS-485 line and a gateway in front of it are on offer."""
    with pytest.raises(SystemExit):
        query._parse_args(["1.2.3.4", "--transport", "udp"])


def _rows(out: str) -> set[str]:
    """The printed rows with their alignment padding collapsed."""
    return {" ".join(line.split()) for line in out.splitlines()}


async def test_print_dumps_every_section_with_decoded_values(
    capsys: pytest.CaptureFixture[str], mock_modbus_unit: MockModbusUnit
) -> None:
    """Every sub-system is printed under its label, with decoded device values."""
    mock_modbus_unit.holding.update(HOLDING)
    device = Series7(mock_modbus_unit)
    await device.async_update()

    query._print(device)
    rows = _rows(capsys.readouterr().out)

    for label, _attr in query.SECTIONS:
        assert label in rows
    assert "entering_water 50.0 °F" in rows  # scaled value carries its unit
    assert "mode heat" in rows  # IntEnum by name
    assert "outputs compressor_1|blower" in rows  # IntFlag by its set bits
    assert "enabled True" in rows  # boolean() register decoded to bool


async def test_print_only_shows_the_zones_the_unit_has(
    capsys: pytest.CaptureFixture[str], mock_modbus_unit: MockModbusUnit
) -> None:
    """The IZ2 board reports two zones, so zones 3-6 are not dumped."""
    mock_modbus_unit.holding.update(HOLDING)
    device = Series7(mock_modbus_unit)
    await device.async_update()
    assert device.config.number_of_zones == 2

    query._print(device)
    rows = _rows(capsys.readouterr().out)

    assert "Zone 1" in rows
    assert "Zone 2" in rows
    assert "Zone 3" not in rows


def test_print_survives_an_unread_device(
    capsys: pytest.CaptureFixture[str], mock_modbus_unit: MockModbusUnit
) -> None:
    """Dumping before any read prints placeholders, not a crash."""
    query._print(Series7(mock_modbus_unit))
    rows = _rows(capsys.readouterr().out)

    assert "Compressor" in rows
    # A field with no value renders as a bare placeholder, without its unit.
    assert "entering_water —" in rows
