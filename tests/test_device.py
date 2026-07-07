"""End-to-end tests of the object model over the in-memory mock backend."""

from __future__ import annotations

import pytest
from modbus_connection.mock import MockModbusConnection, MockModbusUnit

from waterfurnace_modbus import (
    BlowerType,
    EnergyMonitorType,
    FanMode,
    HeatingMode,
    PumpType,
    SystemOutput,
    SystemStatus,
    WaterFurnace,
    ZoneCall,
)
from waterfurnace_modbus.ranges import REGISTER_RANGES

from .conftest import HOLDING


class _CountingUnit:
    """Wraps a ModbusUnit and records read calls; delegates everything else."""

    def __init__(self, inner: MockModbusUnit) -> None:
        self._inner = inner
        self.register_blocks: list[tuple[int, int]] = []
        self.coil_blocks: list[tuple[int, int]] = []

    @property
    def register_reads(self) -> int:
        return len(self.register_blocks)

    @property
    def coil_reads(self) -> int:
        return len(self.coil_blocks)

    async def read_holding_registers(self, address: int, count: int) -> list[int]:
        self.register_blocks.append((address, count))
        return await self._inner.read_holding_registers(address, count)

    async def read_coils(self, address: int, count: int) -> list[bool]:
        self.coil_blocks.append((address, count))
        return await self._inner.read_coils(address, count)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


async def test_device_info(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    info = waterfurnace.info
    assert info.manufacturer == "WaterFurnace"
    assert info.model == "NDV049A111"
    assert info.serial_number == "1234567890"
    assert info.firmware_version == "3.05"
    assert info.program == "ABCVSP"


async def test_sensors(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    s = waterfurnace.sensors
    assert s.entering_water == pytest.approx(50.0)
    assert s.leaving_water == pytest.approx(45.0)
    assert s.entering_air == pytest.approx(70.0)
    assert s.outdoor == pytest.approx(-5.0)  # signed
    assert s.relative_humidity == 45


async def test_status_outputs_and_switches(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    status = waterfurnace.status
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


async def test_status_fault_decode() -> None:
    """A locked-out high-pressure fault decodes to its code and name."""
    inner = MockModbusConnection().for_unit(1)
    inner.holding[25] = 0x8000 | 2  # lockout bit + fault code 2
    device = WaterFurnace(inner)
    await device.status.async_update()
    assert device.status.locked_out is True
    assert device.status.fault_code == 2
    assert device.status.fault == "High Pressure"


async def test_compressor_vs_drive(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    c = waterfurnace.compressor
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


async def test_blower_and_pump(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    assert waterfurnace.blower.speed == 7
    assert waterfurnace.blower.high_compressor_speed == 9
    assert waterfurnace.blower.amps == pytest.approx(2.5)
    assert waterfurnace.pump.output == 65
    assert waterfurnace.pump.waterflow == pytest.approx(9.5)
    assert waterfurnace.pump.loop_pressure == pytest.approx(55.0)


async def test_dhw(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    assert waterfurnace.dhw.enabled is True
    assert waterfurnace.dhw.setpoint == pytest.approx(130.0)
    assert waterfurnace.dhw.water_temperature == pytest.approx(125.0)


async def test_thermostat_reads(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    t = waterfurnace.thermostat
    assert t.ambient_temperature == pytest.approx(72.0)
    assert t.heating_setpoint == pytest.approx(70.0)
    assert t.cooling_setpoint == pytest.approx(75.0)
    assert t.mode is HeatingMode.HEAT  # decoded from bits 8-10
    assert t.fan_mode is FanMode.CONTINUOUS


async def test_energy(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    e = waterfurnace.energy
    assert e.compressor_power == 2400
    assert e.total_power == 2780
    assert e.heat_of_extraction == 18000
    assert e.heat_of_rejection == 24000


async def test_independent_component_update(waterfurnace: WaterFurnace) -> None:
    """A sub-system refreshes on its own, without the rest."""
    await waterfurnace.dhw.async_update()
    assert waterfurnace.dhw.setpoint == pytest.approx(130.0)
    assert waterfurnace.compressor.speed_actual is None  # not updated yet


async def test_full_update_consolidates_reads() -> None:
    """A full device update pools all sub-systems into a few block reads."""
    inner = MockModbusConnection().for_unit(1)
    inner.holding.update(HOLDING)
    unit = _CountingUnit(inner)
    device = WaterFurnace(unit)  # type: ignore[arg-type]

    field_count = sum(len(c._register_fields) for c in device.components)
    await device.async_update()

    # Fields collapse into range-aware block reads — meaningfully fewer than the
    # field count, and no coil reads (this device has none).
    assert unit.register_reads < field_count
    assert unit.coil_reads == 0


async def test_full_update_never_reads_across_an_unreadable_gap() -> None:
    """Every block stays inside the controller's readable ranges (no NAK risk)."""
    inner = MockModbusConnection().for_unit(1)
    unit = _CountingUnit(inner)
    device = WaterFurnace(unit)  # type: ignore[arg-type]
    await device.async_update()

    def readable(address: int) -> bool:
        return any(low <= address <= high for low, high in REGISTER_RANGES)

    for start, count in unit.register_blocks:
        assert all(readable(start + i) for i in range(count)), (
            f"block {start}..{start + count - 1} crosses an unreadable gap"
        )
    # No block exceeds the ABC's 100-register read cap.
    assert all(count <= 100 for _start, count in unit.register_blocks)


async def test_update_listener(waterfurnace: WaterFurnace) -> None:
    calls: list[int] = []
    unsubscribe = waterfurnace.compressor.add_update_listener(lambda: calls.append(1))
    await waterfurnace.compressor.async_update()
    await waterfurnace.compressor.async_update()
    assert len(calls) == 2
    unsubscribe()
    await waterfurnace.compressor.async_update()
    assert len(calls) == 2  # no longer notified


async def test_write_setpoint_uses_write_register(waterfurnace: WaterFurnace) -> None:
    """The heating setpoint reads from 745 but writes to 12619."""
    unit = waterfurnace.thermostat._unit
    await waterfurnace.thermostat.set_heating_setpoint(68.0)
    # Written to the command register (12619), not the read register (745).
    assert (await unit.read_holding_registers(12619, 1))[0] == 680


async def test_write_mode_uses_command_register(waterfurnace: WaterFurnace) -> None:
    """Setting the mode writes the plain code to 12606."""
    unit = waterfurnace.thermostat._unit
    await waterfurnace.thermostat.set_mode(HeatingMode.COOL)
    assert (await unit.read_holding_registers(12606, 1))[0] == int(HeatingMode.COOL)


async def test_write_dhw_setpoint_roundtrip(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    await waterfurnace.dhw.set_setpoint(125.0)
    await waterfurnace.dhw.async_update()
    assert waterfurnace.dhw.setpoint == pytest.approx(125.0)


async def test_write_rejects_out_of_range(waterfurnace: WaterFurnace) -> None:
    with pytest.raises(ValueError):
        await waterfurnace.thermostat.set_heating_setpoint(200.0)
    with pytest.raises(ValueError):
        await waterfurnace.blower.set_blower_only_speed(99)
    with pytest.raises(ValueError):
        await waterfurnace.dhw.set_setpoint(200.0)


async def test_write_rejects_readonly(waterfurnace: WaterFurnace) -> None:
    with pytest.raises(AttributeError):
        await waterfurnace.compressor.write("speed_actual", 3)


async def test_configuration(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    config = waterfurnace.config
    assert config.number_of_zones == 2
    assert config.blower_type is BlowerType.ECM_208_230
    assert config.pump_type is PumpType.VS_PUMP
    assert config.energy_monitor is EnergyMonitorType.ENERGY_MONITOR
    assert config.loop_pressure_trip == pytest.approx(3.0)
    assert config.brine_type == "Antifreeze"


async def test_peripherals(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    p = waterfurnace.peripherals
    assert p.has_thermostat is True
    assert p.has_axb is True
    assert p.has_iz2 is True
    assert p.has_aoc is False  # status 3 = removed
    assert p.has_eev2 is False
    assert p.axb_version == pytest.approx(2.0)
    assert p.moc_version == pytest.approx(1.5)


async def test_humidistat(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    h = waterfurnace.humidistat
    assert h.auto_dehumidification is True
    assert h.auto_humidification is False
    assert h.humidification_target == 40
    assert h.dehumidification_target == 55
    assert h.active_dehumidification is True


async def test_compressor_vs_flags_and_subcool(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    c = waterfurnace.compressor
    assert not c.derate  # empty IntFlag is falsy
    assert not c.safe_mode
    assert not c.alarm
    assert c.internal_error is False
    assert c.saturated_condenser_temperature == pytest.approx(140.0)
    assert c.subcool_heating == pytest.approx(8.0)


async def test_status_aux_heat_and_last_lockout(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    # outputs = compressor_1 + blower, no aux-heat bits set
    assert waterfurnace.status.aux_heat_stage == 0
    assert waterfurnace.status.last_lockout is None


async def test_dealer(waterfurnace: WaterFurnace) -> None:
    await waterfurnace.async_update()
    assert waterfurnace.dealer.name == "ACME HVAC"


async def test_iz2_zone_decode(waterfurnace: WaterFurnace) -> None:
    """Zone config words decode to the right per-zone values (dual stride)."""
    await waterfurnace.async_update()
    z1 = waterfurnace.zones[0]
    assert z1.ambient_temperature == pytest.approx(71.0)
    assert z1.mode is HeatingMode.HEAT
    assert z1.call is ZoneCall.HEAT_1
    assert z1.damper_open is True
    assert z1.cooling_target == 74
    assert z1.heating_target == 70  # high bit from config1, low bits from config2
    assert z1.fan_mode is FanMode.AUTO
    assert z1.economy_priority is False
    assert z1.size == 45

    z2 = waterfurnace.zones[1]  # reads its own +3 registers
    assert z2.ambient_temperature == pytest.approx(68.0)
    assert z2.mode is HeatingMode.COOL
    assert z2.call is ZoneCall.COOL_1
    assert z2.heating_target == 66
    assert z2.economy_priority is True
    assert z2.size == 25


async def test_zone_write_uses_strided_registers(waterfurnace: WaterFurnace) -> None:
    """Zone 2's setpoint write lands at 21203 + 9 (its strided command register)."""
    unit = waterfurnace.zones[1]._unit
    await waterfurnace.zones[1].set_heating_setpoint(67.0)
    assert (await unit.read_holding_registers(21203 + 9, 1))[0] == 670
