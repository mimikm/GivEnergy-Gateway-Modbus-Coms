"""Config flow: type an IP + port, we probe and work out what it is."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import callback

from .client import GivEnergyClient, GivEnergyConnectionError, GivEnergyError
from .const import (
    CONF_SLOW_EVERY,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_EVERY,
    DOMAIN,
    MIN_SCAN_INTERVAL,
)
from .device import GivEnergyDevice

_LOGGER = logging.getLogger(__name__)


async def _probe(host: str, port: int) -> GivEnergyDevice:
    client = GivEnergyClient(host, port)
    device = GivEnergyDevice(client)
    try:
        await device.probe()
    finally:
        await client.close()
    return device


class GivEnergyModbusConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            port = user_input[CONF_PORT]
            try:
                device = await _probe(host, port)
            except GivEnergyConnectionError:
                errors["base"] = "cannot_connect"
            except GivEnergyError as err:
                _LOGGER.warning("Probe of %s:%s failed: %s", host, port, err)
                errors["base"] = "no_response"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error probing %s", host)
                errors["base"] = "unknown"
            else:
                info = device.info
                await self.async_set_unique_id(info.serial or f"{host}:{port}")
                self._abort_if_unique_id_configured(updates={CONF_HOST: host, CONF_PORT: port})
                blocks = ", ".join(sorted(device.blocks_ok))
                _LOGGER.info("Probe OK: %s %s, blocks %s, batteries %s",
                             info.model, info.serial, blocks, list(device.battery_serials.values()))
                return self.async_create_entry(
                    title=f"GivEnergy {info.model} {info.serial}",
                    data={CONF_HOST: host, CONF_PORT: port,
                          CONF_SCAN_INTERVAL: user_input[CONF_SCAN_INTERVAL]},
                )

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=(user_input or {}).get(CONF_HOST, "")): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): vol.All(vol.Coerce(int), vol.Range(1, 65535)),
            vol.Required(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL):
                vol.All(vol.Coerce(int), vol.Range(MIN_SCAN_INTERVAL, 3600)),
        })
        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return GivEnergyOptionsFlow()


class GivEnergyOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cur = {**self.config_entry.data, **self.config_entry.options}
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({
                vol.Required(CONF_SCAN_INTERVAL, default=cur.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)):
                    vol.All(vol.Coerce(int), vol.Range(MIN_SCAN_INTERVAL, 3600)),
                vol.Required(CONF_SLOW_EVERY, default=cur.get(CONF_SLOW_EVERY, DEFAULT_SLOW_EVERY)):
                    vol.All(vol.Coerce(int), vol.Range(1, 60)),
            }),
        )
