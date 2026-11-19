"""Data update coordinator."""
from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import GivEnergyConnectionError, GivEnergyError
from .const import DOMAIN
from .device import GivEnergyDevice

_LOGGER = logging.getLogger(__name__)


class GivEnergyCoordinator(DataUpdateCoordinator[None]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: GivEnergyDevice, interval: int) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{entry.data['host']}",
            update_interval=timedelta(seconds=interval),
        )
        self.device = device

    async def _async_update_data(self) -> None:
        try:
            await self.device.poll()
        except GivEnergyConnectionError as err:
            # Drop the socket; the next poll reconnects on its own.
            await self.device.client.close()
            raise UpdateFailed(f"Connection lost: {err}") from err
        except GivEnergyError as err:
            raise UpdateFailed(str(err)) from err

    async def async_write(self, register: int, value: int) -> None:
        try:
            await self.device.write(register, value)
        except GivEnergyError as err:
            from homeassistant.exceptions import HomeAssistantError

            raise HomeAssistantError(f"Write HR{register}={value} failed: {err}") from err
        self.async_update_listeners()
