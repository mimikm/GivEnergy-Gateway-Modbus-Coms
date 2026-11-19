"""SOC targets, reserve and rate limits."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyControl, controls_for


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    add(GivEnergyNumber(coordinator, c) for c in controls_for(coordinator, "number"))


class GivEnergyNumber(GivEnergyControl, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator, control) -> None:
        super().__init__(coordinator, control)
        self._attr_native_min_value = control.min
        self._attr_native_max_value = control.max
        self._attr_native_step = control.step
        self._attr_native_unit_of_measurement = control.unit

    @property
    def native_value(self) -> float | None:
        return self._raw

    async def async_set_native_value(self, value: float) -> None:
        await self._write(round(value))
