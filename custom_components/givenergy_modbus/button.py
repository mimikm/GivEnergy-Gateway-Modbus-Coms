"""Re-probe button."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyEntity, section_device_info
from .registers import SECTION_SYSTEM


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    add([ReprobeButton(entry.runtime_data.coordinator, entry.entry_id)])


class ReprobeButton(GivEnergyEntity, ButtonEntity):
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:radar"

    def __init__(self, coordinator, entry_id: str) -> None:
        super().__init__(coordinator, "reprobe", "Re-probe registers", section_device_info(coordinator, SECTION_SYSTEM))
        self._entry_id = entry_id

    async def async_press(self) -> None:
        # Reload = fresh probe → entity set follows what the device now answers
        self.hass.config_entries.async_schedule_reload(self._entry_id)
