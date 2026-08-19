"""End-to-end tests of the object model over the in-memory mock backend."""

from __future__ import annotations

import pytest
from modbus_connection import ClientClosedError, IllegalDataAddressError
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from waterfurnace_modbus import (
    BlowerType,
    EnergyMonitorType,
    FanMode,
    HeatingMode,
    PumpType,
    Series7,
    SystemOutput,
    SystemStatus,
    ZoneCall,
)
from waterfurnace_modbus.ranges import REGISTER_RANGES

from .conftest import HOLDING


async def test_device_info(series7: Series7) -> None:
    await series7.async_update()
    info = series7.info
    assert info.manufacturer == "WaterFurnace"
    assert info.model == "NDV049A111"
    assert info.serial_number == "1234567890"
    assert info.firmware_version == "3.05"
    assert info.program == "ABCVSP"


async def test_sensors(series7: Series7) -> None:
    await series7.async_update()
    s = series7.sensors
    assert s.entering_water == pytest.approx(50.0)
    assert s.leaving_water == pytest.approx(45.0)
    assert s.entering_air == pytest.approx(70.0)
    assert s.outdoor == pytest.approx(-5.0)  # signed
    assert s.relative_humidity == 45


async def test_status_outputs_and_switches(series7: Series7) -> None:
    await series7.async_update()
    status = series7.status
    assert SystemOutput.COMPRESSOR_1 in status.outputs
    assert SystemOutput.BLOWER in status.outputs
    assert SystemOutput.COMPRESSOR_2 not in status.outputs
    assert SystemStatus.Y1 in status.inputs
    assert status.low_pressure_switch_closed is True
    assert status.high_pressure_switch_closed is True
    assert status.emergency_shutdown is False
    assert status.line_voltage == 244
    assert status.locked_out is False
    assert status.fault is None


async def test_status_fault_decode(mock_modbus_unit: MockModbusUnit) -> None:
    """A locked-out high-pressure fault decodes to its code and name."""
    mock_modbus_unit.holding[25] = 0x8000 | 2  # lockout bit + fault code 2
    device = Series7(mock_modbus_unit)
    await device.status.async_update()
    assert device.status.locked_out is True
    assert device.status.fault_code == 2
    assert device.status.fault == "High Pressure"


async def test_compressor_vs_drive(series7: Series7) -> None:
    await series7.async_update()
    c = series7.compressor
    assert c.speed_desired == 6
    assert c.speed_actual == 5
    assert c.discharge_pressure == pytest.approx(350.0)
    assert c.suction_pressure == pytest.approx(120.0)
    assert c.discharge_temperature == pytest.approx(180.0)
    assert c.superheat == pytest.approx(10.0)
    assert c.fan_speed == 80
    assert c.eev_open == 45
    assert c.power == 2400  # uint32
    assert c.stage_1_amps == pytest.approx(12.0)


async def test_blower_and_pump(series7: Series7) -> None:
    await series7.async_update()
    assert series7.blower.speed == 7
    assert series7.blower.high_compressor_speed == 9
    assert series7.blower.amps == pytest.approx(2.5)
    assert series7.pump.output == 65
    assert series7.pump.waterflow == pytest.approx(9.5)
    assert series7.pump.loop_pressure == pytest.approx(55.0)


async def test_dhw(series7: Series7) -> None:
    await series7.async_update()
    assert series7.dhw.enabled is True
    assert series7.dhw.setpoint == pytest.approx(130.0)
    assert series7.dhw.water_temperature == pytest.approx(125.0)


async def test_thermostat_reads(series7: Series7) -> None:
    await series7.async_update()
    t = series7.thermostat
    assert series7.sensors.ambient_air == pytest.approx(72.0)
    assert t.heating_setpoint == pytest.approx(70.0)
    assert t.cooling_setpoint == pytest.approx(75.0)
    assert t.mode is HeatingMode.HEAT  # decoded from bits 8-10
    assert t.fan_mode is FanMode.CONTINUOUS


async def test_energy(series7: Series7) -> None:
    await series7.async_update()
    e = series7.energy
    assert e.compressor_power == 2400
    assert e.total_power == 2780
    assert e.heat_of_extraction == 18000
    assert e.heat_of_rejection == 24000


async def test_independent_component_update(series7: Series7) -> None:
    """A sub-system refreshes on its own, without the rest."""
    await series7.dhw.async_update()
    assert series7.dhw.setpoint == pytest.approx(130.0)
    assert series7.compressor.speed_actual is None  # not updated yet


async def test_setup_runs_once_and_the_poll_leaves_it_alone(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Identity and installed-hardware registers are read in setup, never polled.

    They cannot change while the unit runs, so the poll covers only what can.
    """
    await series7.async_update()  # sets up, then polls
    assert series7.info.model == "NDV049A111"
    assert series7.dealer.name == "ACME HVAC"
    setup_and_poll = list(mock_modbus_unit.read_events)

    mock_modbus_unit.read_events.clear()
    report = await series7.async_update()
    poll = mock_modbus_unit.read_events

    assert report.complete
    assert not {"info", "config", "peripherals", "dealer"} & report.updated
    assert sum(block.count for block in poll) < sum(
        block.count for block in setup_and_poll
    )
    # The dealer's 73 registers sit in a range of their own, so the poll drops
    # them entirely. Setup registers that share a range with a polled field are
    # still swept over by the block that reads it — over-read, not re-decoded.
    assert any(block.address >= 31400 for block in setup_and_poll)
    assert not any(block.address >= 31400 for block in poll)
    # The values setup read are still there after a poll that did not re-read them.
    assert series7.info.model == "NDV049A111"
    assert series7.dealer.name == "ACME HVAC"


async def test_only_the_zones_the_unit_has_are_polled(series7: Series7) -> None:
    """``live_zones`` is sized from the IZ2 zone count, and only those are read."""
    await series7.async_update()

    assert series7.config.number_of_zones == 2
    assert series7.live_zones == series7.zones[:2]
    assert series7.live_zones[1].ambient_temperature == pytest.approx(68.0)
    # The other four Zone objects exist but are never read.
    assert all(zone.ambient_temperature is None for zone in series7.zones[2:])


async def test_setup_can_run_explicitly_before_any_poll(series7: Series7) -> None:
    """A consumer that wants setup failures apart from poll failures runs it itself."""
    await series7.async_setup()

    assert series7.info.model == "NDV049A111"
    assert series7.live_zones == series7.zones[:2]
    assert series7.status.outputs is None  # nothing polled yet

    await series7.async_update()
    assert series7.status.outputs is not None


async def test_read_raw_covers_the_setup_only_blocks_too(series7: Series7) -> None:
    """A dump an issue report can use needs the identity blocks, not just the poll."""
    raw = await series7.async_read_raw()

    assert set(raw) == {"holding"}
    holding = raw["holding"]
    assert holding[2] == 305  # identity: read at setup, never polled
    assert holding[483] == 2  # config: read at setup, never polled
    assert 31400 in holding  # dealer
    assert holding[16] == 244  # status: polled
    assert holding[31010] == 680  # zone 2: polled
    assert 31013 not in holding  # zone 3 is not live, so it is not read


async def test_read_raw_leaves_out_a_dealer_block_the_unit_refuses(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Setup writes the dealer block off; the dump must not read it back in."""
    mock_modbus_unit.fail_read(31400, IllegalDataAddressError())
    await series7.async_setup()

    raw = await series7.async_read_raw()

    assert 31400 not in raw["holding"]
    assert raw["holding"][2] == 305


async def test_read_raw_refreshes_the_fields_without_notifying(
    series7: Series7,
) -> None:
    """A diagnostics download is not a poll, so nothing subscribed hears it."""
    await series7.async_setup()
    calls: list[int] = []
    series7.sensors.add_update_listener(lambda: calls.append(1))

    await series7.async_read_raw()

    assert calls == []
    assert series7.sensors.entering_water == pytest.approx(50.0)  # still refreshed


async def test_full_update_consolidates_reads(mock_modbus_unit: MockModbusUnit) -> None:
    """A full device update collapses fields into range-aware block reads."""
    mock_modbus_unit.holding.update(HOLDING)
    device = Series7(mock_modbus_unit)
    field_count = sum(len(c.declared_fields) for c in device.components)

    await device.async_update()

    blocks = mock_modbus_unit.read_events
    # Each sub-system plans its own reads, but adjacent fields still merge —
    # meaningfully fewer blocks than fields, and no coil reads (this device has
    # none).
    assert len(blocks) < field_count
    assert all(block.register_type == "holding" for block in blocks)
    # No block exceeds the ABC's 100-register read cap.
    assert all(block.count <= 100 for block in blocks)


async def test_full_update_never_reads_across_an_unreadable_gap(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Every block stays inside the controller's readable ranges (no NAK risk)."""
    await Series7(mock_modbus_unit).async_update()

    def readable(address: int) -> bool:
        return any(low <= address <= high for low, high in REGISTER_RANGES)

    for block in mock_modbus_unit.read_events:
        last = block.address + block.count - 1
        assert all(readable(address) for address in range(block.address, last + 1)), (
            f"block {block.address}..{last} crosses an unreadable gap"
        )


async def test_every_field_lands_in_a_readable_range(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Every field the map declares sits wholly inside one readable range.

    ``resolved_fields`` reports where each field lands on the device, so this
    checks the register map itself — including each zone's strided placement and
    the write-side command registers — without reading anything.
    """
    for component in Series7(mock_modbus_unit).components:
        for name, resolved in component.resolved_fields.items():
            last = resolved.address + resolved.count - 1
            assert resolved.space == "holding"  # the ABC has nothing else
            assert any(
                low <= resolved.address and last <= high
                for low, high in REGISTER_RANGES
            ), f"{type(component).__name__}.{name} at {resolved.address}..{last}"


async def test_every_sub_system_plans_on_its_own(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """No sub-system declares a field that straddles a readable-range boundary.

    The poll plans each sub-system from its own fields, so this is what it does
    — extended to the components setup reads and to the zones this unit lacks.
    """
    device = Series7(mock_modbus_unit)
    for component in device.components:
        await component.async_update()


async def test_update_survives_a_dropped_connection() -> None:
    """A dropped link heals on the next update — the device is not rebuilt."""
    connection = MockModbusConnection()
    unit = connection.for_unit(1)
    unit.holding.update(HOLDING)
    device = Series7(unit)
    await device.async_update()
    assert device.sensors.entering_water == pytest.approx(50.0)

    lost: list[int] = []
    connection.on_connection_lost(lambda: lost.append(1))
    connection.simulate_connection_lost()
    assert lost == [1]

    # The same device and sub-system handles keep working; the request reconnects.
    await device.async_update()
    assert device.sensors.entering_water == pytest.approx(50.0)


async def test_update_after_close_raises() -> None:
    """Closing is the owner's permanent end of the connection, not a drop."""
    connection = MockModbusConnection()
    unit = connection.for_unit(1)
    unit.holding.update(HOLDING)
    device = Series7(unit)
    await connection.close()

    with pytest.raises(ClientClosedError):
        await device.async_update()


async def test_disconnect_recycles_the_link_silently() -> None:
    """disconnect() drops a stuck link; the next update reconnects.

    Unlike close() it is not permanent, and unlike a real drop it does not
    fire on_connection_lost — the owner tore the link down deliberately.
    """
    connection = MockModbusConnection()
    unit = connection.for_unit(1)
    unit.holding.update(HOLDING)
    device = Series7(unit)
    await device.async_update()

    lost: list[int] = []
    connection.on_connection_lost(lambda: lost.append(1))
    await connection.disconnect()
    assert lost == []  # a deliberate teardown is not a lost connection

    await device.async_update()
    assert device.sensors.entering_water == pytest.approx(50.0)


async def test_update_listener(series7: Series7) -> None:
    calls: list[int] = []
    unsubscribe = series7.compressor.add_update_listener(lambda: calls.append(1))
    await series7.compressor.async_update()
    await series7.compressor.async_update()
    assert len(calls) == 2
    unsubscribe()
    await series7.compressor.async_update()
    assert len(calls) == 2  # no longer notified


async def test_write_setpoint_uses_write_register(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """The heating setpoint reads from 745 but writes to 12619."""
    await series7.thermostat.set_heating_setpoint(68.0)
    # Written to the command register (12619), not the read register (745).
    assert (await mock_modbus_unit.read_holding_registers(12619, 1))[0] == 680


async def test_write_mode_uses_command_register(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Setting the mode writes the plain code to 12606."""
    await series7.thermostat.set_mode(HeatingMode.COOL)
    assert (await mock_modbus_unit.read_holding_registers(12606, 1))[0] == int(
        HeatingMode.COOL
    )


async def test_write_dhw_setpoint_roundtrip(series7: Series7) -> None:
    await series7.async_update()
    await series7.dhw.set_setpoint(125.0)
    await series7.dhw.async_update()
    assert series7.dhw.setpoint == pytest.approx(125.0)


async def test_write_dhw_enabled_roundtrip(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """The enable flag encodes to the 0/1 register the Aurora expects."""
    await series7.dhw.set_enabled(False)
    assert (await mock_modbus_unit.read_holding_registers(400, 1))[0] == 0
    await series7.dhw.async_update()
    assert series7.dhw.enabled is False

    await series7.dhw.set_enabled(True)
    assert (await mock_modbus_unit.read_holding_registers(400, 1))[0] == 1
    await series7.dhw.async_update()
    assert series7.dhw.enabled is True


async def test_write_rejects_out_of_range(series7: Series7) -> None:
    with pytest.raises(ValueError):
        await series7.thermostat.set_heating_setpoint(200.0)
    with pytest.raises(ValueError):
        await series7.blower.set_blower_only_speed(99)
    with pytest.raises(ValueError):
        await series7.dhw.set_setpoint(200.0)


async def test_write_rejects_readonly(series7: Series7) -> None:
    with pytest.raises(AttributeError):
        await series7.compressor.write("speed_actual", 3)


async def test_configuration(series7: Series7) -> None:
    await series7.async_update()
    config = series7.config
    assert config.number_of_zones == 2
    assert config.blower_type is BlowerType.ECM_208_230
    assert config.pump_type is PumpType.VS_PUMP
    assert config.energy_monitor is EnergyMonitorType.ENERGY_MONITOR
    assert config.loop_pressure_trip == pytest.approx(3.0)
    assert config.brine_type == "Antifreeze"


async def test_peripherals(series7: Series7) -> None:
    await series7.async_update()
    p = series7.peripherals
    assert p.has_thermostat is True
    assert p.has_axb is True
    assert p.has_iz2 is True
    assert p.has_aoc is False  # status 3 = removed
    assert p.has_eev2 is False
    assert p.axb_version == pytest.approx(2.0)
    assert p.moc_version == pytest.approx(1.5)


async def test_humidistat(series7: Series7) -> None:
    await series7.async_update()
    h = series7.humidistat
    assert h.auto_dehumidification is True
    assert h.auto_humidification is False
    assert h.humidification_target == 40
    assert h.dehumidification_target == 55
    assert series7.status.active_dehumidification is True


async def test_compressor_vs_flags_and_subcool(series7: Series7) -> None:
    await series7.async_update()
    c = series7.compressor
    assert not c.derate  # empty IntFlag is falsy
    assert not c.safe_mode
    assert not c.alarm
    assert c.internal_error is False
    assert c.saturated_condenser_temperature == pytest.approx(140.0)
    assert c.subcool_heating == pytest.approx(8.0)


async def test_status_aux_heat_and_last_lockout(series7: Series7) -> None:
    await series7.async_update()
    # outputs = compressor_1 + blower, no aux-heat bits set
    assert series7.status.aux_heat_stage == 0
    assert series7.status.last_lockout is None


async def test_dealer(series7: Series7) -> None:
    await series7.async_update()
    assert series7.dealer.name == "ACME HVAC"


async def test_iz2_zone_decode(series7: Series7) -> None:
    """Zone config words decode to the right per-zone values (dual stride)."""
    await series7.async_update()
    z1 = series7.zones[0]
    assert z1.ambient_temperature == pytest.approx(71.0)
    assert z1.mode is HeatingMode.HEAT
    assert z1.call is ZoneCall.HEAT_1
    assert z1.damper_open is True
    assert z1.cooling_target == 74
    assert z1.heating_target == 70  # high bit from config1, low bits from config2
    assert z1.fan_mode is FanMode.AUTO
    assert z1.economy_priority is False
    assert z1.size == 45

    z2 = series7.zones[1]  # reads its own +3 registers
    assert z2.ambient_temperature == pytest.approx(68.0)
    assert z2.mode is HeatingMode.COOL
    assert z2.call is ZoneCall.COOL_1
    assert z2.heating_target == 66
    assert z2.economy_priority is True
    assert z2.size == 25


async def test_zone_write_uses_strided_registers(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Zone 2's setpoint write lands at 21203 + 9 (its strided command register)."""
    await series7.zones[1].set_heating_setpoint(67.0)
    assert (await mock_modbus_unit.read_holding_registers(21203 + 9, 1))[0] == 670


async def test_a_missing_sensor_reads_as_no_value(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """The controller's -999.9 "not present" reading decodes to None, not a value."""
    mock_modbus_unit.holding[1119] = 0xD8F1  # loop pressure, no transducer fitted
    mock_modbus_unit.holding[1114] = 0xD8F1  # DHW water temperature, no sensor

    await series7.async_update()

    assert series7.pump.loop_pressure is None  # not 5553.7 psi
    assert series7.dhw.water_temperature is None  # not -999.9 °F


async def test_a_real_pressure_still_decodes(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Only the sentinel is suppressed; a fitted transducer reads normally."""
    mock_modbus_unit.holding[1119] = 550

    await series7.async_update()

    assert series7.pump.loop_pressure == pytest.approx(55.0)


async def test_outdoor_temperature_without_awl_reads_as_no_value(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """The register reads 0 unless AWL-communicating, which is not 0 °F."""
    mock_modbus_unit.holding[742] = 0

    await series7.async_update()

    assert series7.sensors.outdoor is None


async def test_water_delta_t_is_unsigned(series7: Series7) -> None:
    """The size is the work done; the direction is the reversing valve's."""
    await series7.async_update()

    assert series7.water_delta_t == pytest.approx(5.0)  # 50.0 in, 45.0 out


async def test_cop_in_heating_adds_the_compressor_work(series7: Series7) -> None:
    """Heating delivers what came out of the ground plus the work that moved it."""
    await series7.async_update()

    # 18000 Btuh extracted + 2780 W * 3.412 = 9485 Btuh of work.
    work = 2780 * 3.412
    assert series7.cop == pytest.approx((18000 + work) / work)


async def test_cop_is_zero_with_the_compressor_off(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Nothing is being moved, which is a real answer rather than no answer."""
    mock_modbus_unit.holding[30] = 0x08  # blower only

    await series7.async_update()

    assert series7.cop == 0.0


async def test_cop_ignores_an_implausible_figure(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """A COP far out of range is a bad register, not a heat pump."""
    mock_modbus_unit.holding[1152] = [0, 1]  # 1 W in, 18000 Btuh out

    await series7.async_update()

    assert series7.cop is None


async def test_approach_temperature_follows_the_reversing_valve(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """The condenser is the water coax in cooling and the air coil in heating."""
    mock_modbus_unit.holding[30] = 0x05  # compressor 1 + reversing valve

    await series7.async_update()

    assert series7.approach_temperature == pytest.approx(90.0)  # 140.0 - 50.0 EWT


async def test_a_unit_without_an_axb_reads_return_air_elsewhere(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Return air moves register with the AXB board, and supply air needs one."""
    mock_modbus_unit.holding.update(HOLDING)
    mock_modbus_unit.holding[806] = 0  # AXB not installed
    mock_modbus_unit.holding[567] = 680  # legacy return-air register -> 68.0
    device = Series7(mock_modbus_unit)

    await device.async_update()

    assert device.sensors.entering_air == pytest.approx(68.0)
    assert device.sensors.leaving_air is None


async def test_clearing_the_fault_history_writes_the_magic_word(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """A command, not a setting: nothing reads back as "cleared"."""
    await series7.async_update()

    await series7.status.async_clear_fault_history()

    assert mock_modbus_unit.holding[47] == 0x5555


async def test_readings_and_settings_poll_apart(
    series7: Series7, mock_modbus_unit: MockModbusUnit
) -> None:
    """Neither segment reads a block the other one owns."""
    await series7.async_update()

    mock_modbus_unit.read_events.clear()
    readings = await series7.async_update_readings()
    reading_blocks = {(e.address, e.count) for e in mock_modbus_unit.read_events}

    mock_modbus_unit.read_events.clear()
    settings = await series7.async_update_settings()
    setting_blocks = {(e.address, e.count) for e in mock_modbus_unit.read_events}

    assert "sensors" in readings.updated
    assert "thermostat" in settings.updated
    assert "thermostat" not in readings.updated
    assert not reading_blocks & setting_blocks


async def test_a_unit_that_cannot_dehumidify_does_not_poll_the_humidistat(
    mock_modbus_unit: MockModbusUnit,
) -> None:
    """Its targets would be settings nothing on the unit honours."""
    mock_modbus_unit.holding.update(HOLDING)
    mock_modbus_unit.holding[12309] = 0  # neither auto mode enabled
    mock_modbus_unit.holding[827] = 0  # and no Symphony link to drive them
    device = Series7(mock_modbus_unit)

    report = await device.async_update()

    assert "humidistat" not in report.updated
