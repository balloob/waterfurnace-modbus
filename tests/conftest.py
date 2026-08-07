"""Fixtures: a Series7 over modbus-connection's in-memory mock backend.

The mock backend (and its ``mock_modbus_unit`` fixture) ship with
``modbus-connection`` as an auto-registered pytest plugin, so there is no real
server, socket, or backend here — just an address-keyed store the test loads with
Aurora-shaped register values. Everything on this device is a holding register;
there are no coils.
"""

from __future__ import annotations

import pytest
from modbus_connection.mock import MockModbusUnit

from waterfurnace_modbus import Series7


def _ascii(text: str) -> list[int]:
    """Pack ASCII into 16-bit registers, two chars per register (hi, lo)."""
    if len(text) % 2:
        text += "\0"
    return [(ord(text[i]) << 8) | ord(text[i + 1]) for i in range(0, len(text), 2)]


# Raw holding-register words keyed by their address; decoded view inline.
HOLDING: dict[int, int | list[int]] = {
    # -- device identity --
    2: 305,  # ABC program version -> 3.05
    88: _ascii("ABCVSP"),  # ABC program (VS drive)
    92: _ascii("NDV049A111"),  # model number
    105: _ascii("1234567890"),  # serial number
    # -- configuration / capabilities --
    483: 2,  # number of IZ2 zones
    404: 1,  # blower type -> ECM_208_230
    413: 3,  # pump type -> VS_PUMP
    403: 2,  # flow meter type -> ONE_INCH
    416: 0,  # phase -> SINGLE
    412: 2,  # energy monitor -> ENERGY_MONITOR
    419: 30,  # loop pressure trip -> 3.0 psi
    402: 485,  # brine type -> Antifreeze
    # -- peripherals (status word, then version) --
    800: 1,  # thermostat active
    801: 300,  # -> 3.0
    806: 1,  # AXB active
    807: 200,  # -> 2.0
    812: 1,  # IZ2 active
    813: 200,  # -> 2.0
    815: 3,  # AOC removed
    818: 1,  # MOC active
    819: 150,  # -> 1.5
    824: 3,  # EEV2 removed
    827: 1,  # AWL active
    828: 300,  # -> 3.0
    # -- status --
    16: 244,  # line voltage -> 244 V
    112: 240,  # line voltage setting -> 240 V
    25: 0,  # last fault -> none
    26: 0,  # last lockout -> none
    30: 0x09,  # outputs: compressor_1 (0x01) + blower (0x08)
    31: 0x191,  # status: y1 (0x01) + g (0x10) + lps closed (0x80) + hps closed (0x100)
    # -- sensors --
    740: 700,  # entering air -> 70.0
    900: 950,  # leaving air -> 95.0
    1111: 500,  # entering water -> 50.0
    1110: 450,  # leaving water -> 45.0
    742: 65486,  # outdoor -> -5.0 (signed)
    741: 45,  # relative humidity -> 45 %
    20: 850,  # air coil (FP2) -> 85.0
    19: 900,  # cooling liquid line (FP1) -> 90.0
    # -- compressor (VS drive) --
    3000: 6,  # speed desired
    3001: 5,  # speed actual
    3322: 3500,  # discharge pressure -> 350.0 psi
    3323: 1200,  # suction pressure -> 120.0 psi
    3325: 1800,  # discharge temperature -> 180.0
    3326: 900,  # ambient temperature -> 90.0
    3327: 1100,  # drive temperature -> 110.0
    3330: 500,  # entering water temperature -> 50.0
    3331: 244,  # line voltage -> 244 V
    3522: 1200,  # inverter temperature -> 120.0
    3903: 400,  # suction temperature -> 40.0
    3905: 350,  # saturated evaporator temperature -> 35.0
    3906: 100,  # superheat -> 10.0
    3524: 80,  # fan speed -> 80 %
    3808: 45,  # EEV open -> 45 %
    3422: [0, 2400],  # drive power -> 2400 W (uint32)
    1107: 120,  # compressor stage 1 amps -> 12.0 A
    1108: 0,  # compressor stage 2 amps -> 0.0 A
    1134: 1400,  # saturated condenser temp -> 140.0
    1135: 80,  # subcool (heating) -> 8.0
    1136: 60,  # subcool (cooling) -> 6.0
    3223: 0,  # VS drive derate -> none
    3225: 0,  # VS drive safe mode -> none
    3226: 0,  # VS drive alarm 1 -> none
    3227: 0,  # VS drive alarm 2 -> none
    # -- blower --
    344: 7,  # current ECM speed
    340: 4,  # blower-only speed
    341: 5,  # low compressor speed
    342: 9,  # high compressor speed
    347: 11,  # aux-heat speed
    1105: 25,  # blower amps -> 2.5 A
    1148: [0, 300],  # blower power -> 300 W (uint32)
    # -- pump --
    325: 65,  # output -> 65 %
    321: 20,  # minimum speed -> 20 %
    322: 100,  # maximum speed -> 100 %
    1117: 95,  # waterflow -> 9.5 gpm
    1119: 550,  # loop pressure -> 55.0 psi
    1164: [0, 80],  # pump power -> 80 W (uint32)
    # -- domestic hot water --
    400: 1,  # DHW enabled
    401: 1300,  # DHW setpoint -> 130.0
    1114: 1250,  # tank temperature -> 125.0
    # -- thermostat --
    502: 720,  # ambient -> 72.0
    745: 700,  # heating setpoint -> 70.0
    746: 750,  # cooling setpoint -> 75.0
    12006: 0x300,  # mode HEAT (3) packed in bits 8-10
    12005: 0x80,  # fan CONTINUOUS bit
    # -- humidistat --
    362: 1,  # active dehumidification running
    12309: 0x4000,  # auto dehumidification enabled
    12310: (40 << 8) | 55,  # humidify target 40 %, dehumidify target 55 %
    # -- dealer --
    31400: _ascii("ACME HVAC"),  # dealer name
    # -- IZ2 zone 1 (fan auto, cool 74, heat 70, mode heat, call h1, damper open,
    #    comfort, size 45) --
    31007: 710,  # ambient -> 71.0
    31008: 0x4D,  # config 1: cooling target 74 + heating carry bit
    31009: 0x1314,  # config 2: mode heat, call h1, damper open, heating target hi
    31200: 0x10,  # config 3: comfort, size 45
    # -- IZ2 zone 2 (cool 72, heat 66, mode cool, call c1, damper closed,
    #    economy, size 25) --
    31010: 680,  # ambient -> 68.0
    31011: 0x48,  # config 1: cooling target 72
    31012: 0xF20A,  # config 2: mode cool, call c1, heating target hi
    31203: 0x28,  # config 3: economy, size 25
    # -- energy --
    1146: [0, 2400],  # compressor power -> 2400 W
    1150: [0, 0],  # aux power -> 0 W
    1152: [0, 2780],  # total power -> 2780 W
    1154: [0, 18000],  # heat of extraction -> 18000 Btuh (int32)
    1156: [0, 24000],  # heat of rejection -> 24000 Btuh (int32)
}


@pytest.fixture
def series7(mock_modbus_unit: MockModbusUnit) -> Series7:
    """A Series7 over the mock unit, preloaded with device values."""
    mock_modbus_unit.holding.update(HOLDING)
    return Series7(mock_modbus_unit)


@pytest.fixture
def unit(mock_modbus_unit: MockModbusUnit) -> MockModbusUnit:
    """The mock unit the ``series7`` fixture reads and writes through.

    Request it alongside ``series7`` to assert on the register store a write
    landed in, rather than reaching for the unit a component holds.
    """
    return mock_modbus_unit
