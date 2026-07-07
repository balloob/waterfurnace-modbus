"""Domestic hot water (DHW) generation via the AXB board.

Only present on a unit with an AXB expansion board and DHW enabled; on other
units these registers read back as ``0``.
"""

from __future__ import annotations

from modbus_connection.model import integer

from .model import AuroraComponent, temperature


class DHW(AuroraComponent):
    """Domestic hot water: enable, setpoint and current tank temperature."""

    _enabled_raw = integer(400, signed=False, writable=True)
    setpoint = temperature(401, writable=True)  # 100-140 °F
    water_temperature = temperature(1114)

    @property
    def enabled(self) -> bool | None:
        """Whether DHW generation is enabled."""
        raw = self._enabled_raw
        return None if raw is None else bool(raw)

    async def set_enabled(self, enabled: bool) -> None:
        """Enable or disable DHW generation."""
        await self.write("_enabled_raw", 1 if enabled else 0)

    async def set_setpoint(self, fahrenheit: float) -> None:
        """Set the DHW tank setpoint (100-140 °F)."""
        if not 100 <= fahrenheit <= 140:
            raise ValueError("DHW setpoint must be between 100 and 140 °F")
        await self.write("setpoint", fahrenheit)
