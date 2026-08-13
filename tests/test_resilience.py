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
    await series7.async_update()
    before = series7.status.line_voltage

    mock_modbus_unit.holding[16] = 230  # line voltage changes on the device
    mock_modbus_unit.holding[740] = 720  # so does entering air
    mock_modbus_unit.fail_read(16, ModbusTimeoutError("slow status block"))
    report = await series7.async_update()

    assert not report.complete
    assert set(report.failed) == {"status"}
    assert isinstance(report.failed["status"], ModbusTimeoutError)
    assert "sensors" in report.updated
    assert series7.status.line_voltage == before
    assert series7.sensors.entering_air == pytest.approx(72.0)


async def test_listeners_fire_at_the_end_and_only_for_fresh_components(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    await series7.async_update()
    seen: list[int] = []
    series7.sensors.add_update_listener(
        lambda: seen.append(len(mock_modbus_unit.read_events))
    )
    series7.status.add_update_listener(lambda: seen.append(-1))

    mock_modbus_unit.fail_read(16, ModbusTimeoutError("slow status block"))
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
