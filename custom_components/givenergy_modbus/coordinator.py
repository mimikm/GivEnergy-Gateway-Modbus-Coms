"""Coordinator that polls each section on its own interval."""
from __future__ import annotations

from datetime import timedelta
from functools import reduce
from math import gcd
import logging
import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import GivEnergyConnectionError, GivEnergyError
from .const import DOMAIN, MIN_INTERVAL, SectionSetting
from .device import GivEnergyDevice
from .plan import Plan
from .registers import SECTION_SYSTEM

_LOGGER = logging.getLogger(__name__)


class GivEnergyCoordinator(DataUpdateCoordinator[None]):
    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, device: GivEnergyDevice, plan: Plan,
        settings: dict[str, SectionSetting], display_name: str,
    ) -> None:
        self.device = device
        self.plan = plan
        self.display_name = display_name
        self.intervals = {s: cfg.interval for s, cfg in settings.items() if cfg.enabled}
        # Tick at the GCD of the enabled intervals so every section lands on time
        tick = max(MIN_INTERVAL, reduce(gcd, self.intervals.values())) if self.intervals else 300
        self._last: dict[str, float] = {}
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"{DOMAIN}_{entry.data['host']}",
            update_interval=timedelta(seconds=tick),
        )

    def _due(self) -> list[str]:
        now = time.monotonic()
        return [s for s, iv in self.intervals.items() if now - self._last.get(s, -1e9) >= iv - 1]

    async def _async_update_data(self) -> None:
        due = self._due()
        if not due:
            return
        keys: set[str] = set()
        for s in due:
            keys |= self.plan.blocks.get(s, set())
        batteries = self.plan.poll_batteries and SECTION_SYSTEM in due
        try:
            if keys or batteries:
                await self.device.poll(keys, batteries)
        except GivEnergyConnectionError as err:
            await self.device.client.close()  # next poll reconnects
            raise UpdateFailed(f"Connection lost: {err}") from err
        except GivEnergyError as err:
            raise UpdateFailed(str(err)) from err
        now = time.monotonic()
        for s in due:
            self._last[s] = now
        _LOGGER.debug("Polled sections %s (%d blocks)", due, len(keys))

    async def async_write(self, register: int, value: int) -> None:
        try:
            await self.device.write(register, value)
        except GivEnergyError as err:
            raise HomeAssistantError(f"Write HR{register}={value} failed: {err}") from err
        self.async_update_listeners()
