"""Enable/disable flags (HR 20, 27, 59, 96)."""
from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyControl, controls_for


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    add(GivEnergySwitch(coordinator, c) for c in controls_for(coordinator, "switch"))


class GivEnergySwitch(GivEnergyControl, SwitchEntity):
    @property
    def is_on(self) -> bool | None:
        raw = self._raw
        return None if raw is None else raw == 1

    async def async_turn_on(self, **kwargs) -> None:
        await self._write(1)

    async def async_turn_off(self, **kwargs) -> None:
        await self._write(0)
