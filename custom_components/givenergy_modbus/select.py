"""Battery mode (GivTCP-style composite) and pause mode."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyControl, GivEnergyEntity, controls_for, main_device_info
from .registers import BATTERY_MODES, CONTROL_FAMILIES


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    dev = coordinator.device
    entities: list[SelectEntity] = [GivEnergySelect(coordinator, c) for c in controls_for(coordinator, "select")]
    if dev.info.family in CONTROL_FAMILIES and "HR0" in dev.blocks_ok:
        entities.append(BatteryModeSelect(coordinator))
    add(entities)


class GivEnergySelect(GivEnergyControl, SelectEntity):
    def __init__(self, coordinator, control) -> None:
        super().__init__(coordinator, control)
        self._by_label = {v: k for k, v in control.options.items()}
        self._attr_options = list(control.options.values())

    @property
    def current_option(self) -> str | None:
        raw = self._raw
        return self._control.options.get(raw) if raw is not None else None

    async def async_select_option(self, option: str) -> None:
        await self._write(self._by_label[option])


class BatteryModeSelect(GivEnergyEntity, SelectEntity):
    """Eco / Timed Demand / Timed Export — writes HR27 (eco) and HR59 (discharge)."""

    _attr_icon = "mdi:home-battery"
    _attr_options = list(BATTERY_MODES)

    def __init__(self, coordinator) -> None:
        super().__init__(coordinator, "battery_mode", "Battery mode", main_device_info(coordinator))

    @property
    def current_option(self) -> str | None:
        hr = self.coordinator.device.inverter.hr
        state = (hr.get(27), hr.get(59))
        for label, regs in BATTERY_MODES.items():
            if regs == state:
                return label
        return None

    async def async_select_option(self, option: str) -> None:
        eco, discharge = BATTERY_MODES[option]
        await self.coordinator.async_write(27, eco)
        await self.coordinator.async_write(59, discharge)
