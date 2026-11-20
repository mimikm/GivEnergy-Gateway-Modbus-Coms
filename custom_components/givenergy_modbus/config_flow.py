"""Config flow: IP + port → probe → name and pick which sections to poll."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import callback

from .client import GivEnergyClient, GivEnergyConnectionError, GivEnergyError
from .const import (
    CONF_DEVICE_NAME,
    CONF_FULL_SCAN,
    CONF_REPROBE,
    DEFAULT_PORT,
    DOMAIN,
    MAX_INTERVAL,
    MIN_INTERVAL,
    conf_enabled,
    conf_interval,
    default_device_name,
    section_settings,
)
from .device import GivEnergyDevice
from .registers import SECTIONS

_LOGGER = logging.getLogger(__name__)


async def _probe(host: str, port: int) -> GivEnergyDevice:
    client = GivEnergyClient(host, port)
    device = GivEnergyDevice(client)
    try:
        await device.probe()
    finally:
        await client.close()
    return device


def _sections_schema(current: dict, default_name: str) -> vol.Schema:
    settings = section_settings(current)
    fields: dict = {vol.Required(CONF_DEVICE_NAME, default=current.get(CONF_DEVICE_NAME) or default_name): str}
    for s in SECTIONS:
        fields[vol.Required(conf_enabled(s), default=settings[s].enabled)] = bool
        fields[vol.Required(conf_interval(s), default=settings[s].interval)] = vol.All(
            vol.Coerce(int), vol.Range(MIN_INTERVAL, MAX_INTERVAL)
        )
    return vol.Schema(fields)


class GivEnergyModbusConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._host: str = ""
        self._port: int = DEFAULT_PORT
        self._device: GivEnergyDevice | None = None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._port = user_input[CONF_PORT]
            try:
                self._device = await _probe(self._host, self._port)
            except GivEnergyConnectionError:
                errors["base"] = "cannot_connect"
            except GivEnergyError as err:
                _LOGGER.warning("Probe of %s:%s failed: %s", self._host, self._port, err)
                errors["base"] = "no_response"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error probing %s", self._host)
                errors["base"] = "unknown"
            else:
                info = self._device.info
                await self.async_set_unique_id(info.serial or f"{self._host}:{self._port}")
                self._abort_if_unique_id_configured(updates={CONF_HOST: self._host, CONF_PORT: self._port})
                return await self.async_step_sections()

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_sections(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        assert self._device is not None
        info = self._device.info
        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_DEVICE_NAME],
                data={CONF_HOST: self._host, CONF_PORT: self._port},
                options=user_input,
            )
        return self.async_show_form(
            step_id="sections",
            data_schema=_sections_schema({}, default_device_name(info)),
            description_placeholders={
                "model": info.model,
                "serial": info.serial,
                "batteries": str(len(self._device.battery_serials)),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GivEnergyOptionsFlow()


class GivEnergyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            settings = dict(user_input)
            reprobe = settings.pop(CONF_REPROBE, False)
            full_scan = settings.pop(CONF_FULL_SCAN, False)
            if full_scan:
                from . import async_full_register_scan

                try:
                    await async_full_register_scan(self.hass, entry)
                except (GivEnergyError, AttributeError) as err:
                    _LOGGER.warning("Full register scan failed: %s", err)
                    errors["base"] = "scan_failed"
            if not errors:
                unchanged = settings == {k: entry.options.get(k) for k in settings} and \
                    set(entry.options) <= set(settings)
                if reprobe and unchanged:
                    # Options listener only reloads on change; a reload is what re-reads the device.
                    self.hass.config_entries.async_schedule_reload(entry.entry_id)
                return self.async_create_entry(data=settings)
        cur = {**entry.data, **entry.options}
        schema = _sections_schema(cur, entry.title).extend({
            vol.Optional(CONF_REPROBE, default=False): bool,
            vol.Optional(CONF_FULL_SCAN, default=False): bool,
        })
        return self.async_show_form(step_id="init", data_schema=schema, errors=errors)
