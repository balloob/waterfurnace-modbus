"""One failing block must not take the rest of the poll with it.

The Aurora spreads a poll over ~30 block reads in a dozen register ranges; a
board that is slow or refuses one of them should cost the values that live in
that block, not every value on the unit. Sub-systems are therefore read one at a
time, and :meth:`Series7.async_update` reports what got through.
"""

from __future__ import annotations

import pytest
from modbus_connection import (
    IllegalDataAddressError,
    ModbusConnectionError,
    ModbusTimeoutError,
)
from modbus_connection.mock import MockModbusUnit

from waterfurnace_modbus import Series7

from .conftest import HOLDING


async def test_a_failed_component_leaves_the_rest_fresh(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """status answers first, so a later slow block costs only its own sub-system."""
    await series7.async_update()
    before = series7.sensors.entering_air

    mock_modbus_unit.holding[740] = 720  # entering air changes on the device
    mock_modbus_unit.holding[16] = 230  # so does line voltage
    mock_modbus_unit.fail_read(740, ModbusTimeoutError("slow sensor block"))
    report = await series7.async_update()

    assert not report.complete
    assert set(report.failed) == {"sensors"}
    assert isinstance(report.failed["sensors"], ModbusTimeoutError)
    assert "status" in report.updated
    assert series7.sensors.entering_air == before
    assert series7.status.line_voltage == 230


async def test_listeners_fire_at_the_end_and_only_for_fresh_components(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    await series7.async_update()
    seen: list[int] = []
    series7.sensors.add_update_listener(
        lambda: seen.append(len(mock_modbus_unit.read_events))
    )
    series7.thermostat.add_update_listener(lambda: seen.append(-1))

    mock_modbus_unit.fail_read(745, ModbusTimeoutError("slow thermostat block"))
    mock_modbus_unit.read_events.clear()
    await series7.async_update()

    # One notification, after every sub-system was tried; none for the failure.
    # sensors is read second of eleven, so an early notify would show a smaller
    # count than the poll's final one.
    assert seen == [len(mock_modbus_unit.read_events)]


async def test_a_failed_zone_is_reported_by_its_position(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Zones are their own poll units, keyed ``zone_<n>`` like the entities."""
    await series7.async_update()

    mock_modbus_unit.holding[31007] = 720  # zone 1's ambient changes
    mock_modbus_unit.holding[31010] = 660  # so does zone 2's, but its read fails
    mock_modbus_unit.fail_read(31010, ModbusTimeoutError("slow zone 2"))
    report = await series7.async_update()

    assert set(report.failed) == {"zone_2"}
    assert series7.zones[0].ambient_temperature == pytest.approx(72.0)
    assert series7.zones[1].ambient_temperature == pytest.approx(68.0)  # kept


async def test_the_axb_sharers_still_fail_one_at_a_time(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Blower, pump, compressor and energy overlap in 1105-1165 but stay separate.

    Their blocks overlap, so pooling them into one ``ComponentGroup`` would read
    the span once and save three requests. It is not worth it: a group's read
    plan is all-or-nothing, and each of the four also reads registers nowhere
    near the AXB — the compressor's whole VS-drive telemetry above 3000, the
    blower's ECM presets at 340, the pump's limits at 321. Pooling would let the
    optional AXB energy-monitor block take all of that down with it.
    """
    await series7.async_update()

    # A VS-drive failure is compressor-only; the AXB sharers keep refreshing.
    mock_modbus_unit.fail_read(3322, ModbusTimeoutError("slow VS drive"))
    report = await series7.async_update()
    assert set(report.failed) == {"compressor"}
    assert {"blower", "pump", "energy"} <= report.updated
    assert series7.pump.waterflow == pytest.approx(9.5)

    # And the reverse: the AXB block the energy monitor owns is not the drive's.
    mock_modbus_unit.fail_read(3322, None)
    mock_modbus_unit.fail_read(1146, ModbusTimeoutError("no energy monitor"))
    report = await series7.async_update()
    assert "compressor" in report.updated
    assert series7.compressor.discharge_pressure == pytest.approx(350.0)


async def test_a_silent_unit_raises_on_the_first_timeout(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Nothing answered, so the remaining ten sub-systems would only time out too.

    A poll is 32 block reads over an RS-485 line; walking all eleven sub-systems
    into a 10 s timeout each costs 110 s against a 30 s poll interval, so the
    probe failing has to end the poll.
    """
    await series7.async_update()
    mock_modbus_unit.fail_requests(ModbusTimeoutError("unit is silent"))
    mock_modbus_unit.read_events.clear()

    with pytest.raises(ModbusTimeoutError):
        await series7.async_update()

    assert len(mock_modbus_unit.read_events) == 1  # status, and nothing after it


async def test_a_timeout_after_something_answered_is_still_contained(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """A refusal proves the unit is there, so a later timeout is only slow."""
    await series7.async_update()
    mock_modbus_unit.fail_read(16, IllegalDataAddressError())  # status refuses
    mock_modbus_unit.fail_read(740, ModbusTimeoutError("slow sensor block"))

    report = await series7.async_update()

    assert set(report.failed) == {"status", "sensors"}
    assert "compressor" in report.updated


async def test_a_dead_link_raises_instead_of_reporting(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    await series7.async_update()
    mock_modbus_unit.fail_requests(ModbusConnectionError("link down"))
    with pytest.raises(ModbusConnectionError):
        await series7.async_update()


async def test_every_component_refreshes_on_a_healthy_device(
    series7: Series7,
) -> None:
    report = await series7.async_update()

    assert report.complete
    assert report.failed == {}
    assert {"status", "sensors", "compressor", "zone_1", "zone_2"} <= report.updated
    assert "info" not in report.updated  # read once in setup
    assert "zone_3" not in report.updated  # this unit has two zones


async def test_a_failed_setup_raises_and_the_next_update_retries(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Setup reads what the poll is built from, so there is nothing partial yet."""
    mock_modbus_unit.holding.update(HOLDING)
    mock_modbus_unit.fail_read(2, ModbusTimeoutError("no identity"))
    device = Series7(mock_modbus_unit)

    with pytest.raises(ModbusTimeoutError):
        await device.async_update()

    mock_modbus_unit.fail_read(2, None)
    report = await device.async_update()
    assert report.complete
    assert device.info.model == "NDV049A111"


async def test_a_unit_that_refuses_the_dealer_block_still_sets_up(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Dealer details are commissioning trivia, not part of what the device is."""
    mock_modbus_unit.fail_read(31400, IllegalDataAddressError())

    report = await series7.async_update()

    assert report.complete
    assert series7.dealer.name is None
    assert series7.info.model == "NDV049A111"


async def test_a_slow_dealer_block_is_retried_rather_than_written_off(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """A timeout says nothing about whether the unit serves those addresses."""
    mock_modbus_unit.fail_read(31400, ModbusTimeoutError("slow dealer block"))

    with pytest.raises(ModbusTimeoutError):
        await series7.async_update()

    mock_modbus_unit.fail_read(31400, None)
    await series7.async_update()
    assert series7.dealer.name == "ACME HVAC"
