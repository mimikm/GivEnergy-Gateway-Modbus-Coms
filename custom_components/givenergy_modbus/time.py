"""Charge/discharge slot times (registers hold HHMM, e.g. 0030 → 30)."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyControl, controls_for


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    add(GivEnergyTime(coordinator, c) for c in controls_for(coordinator, "time"))


class GivEnergyTime(GivEnergyControl, TimeEntity):
    _attr_icon = "mdi:clock-outline"

    @property
    def native_value(self) -> time | None:
        raw = self._raw
        if raw is None:
            return None
        h, m = divmod(raw, 100)
        return time(h, m) if h < 24 and m < 60 else None

    async def async_set_value(self, value: time) -> None:
        await self._write(value.hour * 100 + value.minute)
