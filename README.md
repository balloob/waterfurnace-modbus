# waterfurnace-modbus

A standalone Python library that reads a **WaterFurnace Aurora** geothermal heat
pump — modelled for the variable-speed **Series 7** — over Modbus, exposed as a
normal, object-oriented Python API.

Addresses, scales and data types follow the reverse-engineered Aurora point list
from the [ccutrer/waterfurnace_aurora](https://github.com/ccutrer/waterfurnace_aurora)
project (captured from AID Tool ↔ ABC traffic) and are **verified in tests**
against an in-memory mock of the controller.

## Design

- It **consumes the connection abstraction**, not a backend: the API takes a
  [`modbus_connection.ModbusUnit`](../modbus-connection) and reads/writes through
  it. You choose the backend (tmodbus, pymodbus, …).
- Since `modbus-connection` 4.0 a connection **manages its own link**: it connects
  on the first request and re-establishes itself after a drop, over the same unit
  handle. Nothing has to rebuild the `Series7` object when the link goes down —
  and a Home Assistant integration should **not** reload its config entry either.
- A `Series7` is a tree of independently-updatable **sub-systems**, each a
  `Component` that knows its own registers:

  | Attribute | What |
  | --- | --- |
  | `info` | model, ABC program, firmware version, serial → `DeviceInformation` |
  | `config` | installed-hardware config: zone count, blower/pump/flow/brine type, energy-monitor package |
  | `peripherals` | which control boards are installed (AXB, IZ2, AOC, MOC, EEV2, AWL) + versions |
  | `status` | active outputs, thermostat inputs, pressure switches, faults, lockout, aux-heat stage |
  | `sensors` | device-wide air/water temperatures and humidity |
  | `compressor` | the variable-speed drive: speed, pressures, temperatures, power, EEV, subcool, drive faults |
  | `blower` | ECM blower speed and its configurable speed presets |
  | `pump` | variable-speed loop pump output, flow, loop pressure |
  | `dhw` | domestic hot water: enable, setpoint, tank temperature |
  | `thermostat` | heating/cooling setpoints, operating mode, fan mode |
  | `humidistat` | auto humidification / dehumidification modes and targets |
  | `energy` | per-load electrical power and geothermal heat transfer |
  | `dealer` | installer contact details (diagnostic) |
  | `zones` | up to six IntelliZone 2 (IZ2) zones, each its own `Zone` (mode, setpoints, damper, call) |

- Each sub-system can refresh on its own and has its **own update listeners**, so
  a single Home Assistant entity can subscribe to just the part it shows.
- Everything lives in the holding-register space (FC03); this device has no
  coils — booleans are packed as bits inside status registers.
- Units of measurement live in each property's docstring, not in the value.

## Use

```python
import asyncio
from modbus_connection import ModbusTcpParams
from modbus_connection.tmodbus import ModbusConnection
from waterfurnace_modbus import Series7, HeatingMode


async def main() -> None:
    # An RTU-over-TCP gateway. Constructing performs no I/O; the first read
    # connects, and a later drop re-connects on its own.
    conn = ModbusConnection(ModbusTcpParams(host="192.168.1.50", framer="rtu"))
    try:
        heat_pump = Series7(conn.for_unit(1))            # unit 1 = the Aurora ABC
        await heat_pump.async_update()

        print("Model:", heat_pump.info.model)
        print("Entering water:", heat_pump.sensors.entering_water, "°F")
        print("Compressor speed:", heat_pump.compressor.speed_actual, "/ 12")
        print("Heating setpoint:", heat_pump.thermostat.heating_setpoint, "°F")
        print("Loop pressure:", heat_pump.pump.loop_pressure, "psi")
        print("Total power:", heat_pump.energy.total_power, "W")
        print("Fault:", heat_pump.status.fault)

        for i in range(heat_pump.config.number_of_zones or 0):
            zone = heat_pump.zones[i]
            print(f"Zone {i + 1}:", zone.mode, zone.ambient_temperature, "°F")

        # Writes (the read/write address split and scaling are handled for you)
        await heat_pump.thermostat.set_heating_setpoint(70.0)
        await heat_pump.thermostat.set_mode(HeatingMode.HEAT)
        await heat_pump.dhw.set_setpoint(130.0)
    finally:
        await conn.close()


asyncio.run(main())
```

`close()` is the owner's permanent end of the connection — a later request raises
`ClientClosedError`. A long-lived poller opens one connection and keeps it; it
never closes between polls to force a reconnect.

### Updating just one sub-system

```python
await heat_pump.compressor.async_update()          # only reads the VS-drive registers
unsub = heat_pump.compressor.add_update_listener(refresh_my_entity)
```

## Command-line tool

`script/query.py` connects to a heat pump, reads it once, and prints every
value — handy for checking a real unit without Home Assistant. It needs a
concrete backend, so install the `cli` extra (`pip install waterfurnace-modbus[cli]`,
or run via `uv run --extra cli`):

```bash
# Network gateway (RTU-over-TCP by default — how Aurora RS-485 gateways work):
uv run --extra cli python script/query.py 192.168.1.50 --unit 1

# Serial / USB (defaults to the Aurora 19200 8E1 line):
uv run --extra cli python script/query.py /dev/ttyUSB0 --transport serial --unit 1
```

The connection arguments come from `modbus_connection.cli_helper`, narrowed to
the two transports the Aurora offers. Use `--port`, `--framer {rtu,socket}`,
`--timeout`, or `--baudrate`/`--parity`/… (serial) to override defaults; `--help`
lists them all. Output is grouped by sub-system.

## Connection notes

The Aurora ABC speaks **Modbus RTU on RS-485 at 19200 8E1, slave address 1**, and
caps a single read at 100 registers (the library never asks for more). Over a
network it is almost always reached through a transparent serial (RTU-over-TCP)
gateway, so the CLI's `tcp` transport defaults to the `rtu` framer.

## Scope and selective polling

The map is comprehensive: identity, installed-hardware config, board presence,
status/faults, temperatures, the variable-speed compressor (incl. drive fault
flags and subcool) and pump, the ECM blower, DHW, the communicating-thermostat
setpoints/modes, humidistat, energy monitoring, dealer info, and up to six IZ2
zones. Only registers whose encoding is unknown even in the upstream map, and the
raw fault-history buffer, are left out.

`Series7.async_update()` refreshes **everything** — convenient for the CLI,
but more than a Home Assistant integration needs each poll. Because every
sub-system is an independent `Component`, an integration typically reads `config`
and `peripherals` **once** to discover the hardware, then polls only the
components it surfaces (or builds its own `ComponentGroup` over that subset):

```python
from modbus_connection.model import ComponentGroup

hot = ComponentGroup(unit, [hp.status, hp.sensors, hp.compressor, *hp.zones])
await hot.async_update()        # fast-changing values, polled often
await hp.config.async_update()  # once at startup
```

Not every unit has every board: `config.number_of_zones` is `0` without IZ2, and
the `peripherals.has_*` flags tell you which sub-systems are worth polling.

## Develop / test

```bash
uv sync
uv run pytest
```

The suite exercises decoding, the read/write address split, writes and listeners
against the in-memory mock backend that ships with `modbus-connection` (its
`mock_modbus_unit` pytest fixture) — no real Modbus server or backend is needed.

Formatting/linting is [ruff](https://docs.astral.sh/ruff/); install the commit
hook with [prek](https://github.com/j178/prek):

```bash
uvx prek install
uvx prek run --all-files
```
