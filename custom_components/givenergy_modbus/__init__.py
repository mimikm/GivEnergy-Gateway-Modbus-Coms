"""GivEnergy Gateway Modbus Coms — local integration for GivEnergy inverters, AIO and Gateway."""
from __future__ import annotations

from dataclasses import dataclass
import logging

import voluptuous as vol

from homeassistant.components import persistent_notification
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError, ServiceValidationError
import homeassistant.helpers.config_validation as cv

from .client import GivEnergyClient, GivEnergyError
from .const import (
    CONF_SLOW_EVERY,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SLOW_EVERY,
    DOMAIN,
    PLATFORMS,
    SERVICE_PROBE,
    SERVICE_READ,
    SERVICE_WRITE,
)
from .coordinator import GivEnergyCoordinator
from .device import GivEnergyDevice
from .registers import WRITE_SAFE_REGISTERS

_LOGGER = logging.getLogger(__name__)


@dataclass
class RuntimeData:
    device: GivEnergyDevice
    coordinator: GivEnergyCoordinator


type GivEnergyConfigEntry = ConfigEntry[RuntimeData]


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry) -> bool:
    opts = {**entry.data, **entry.options}
    client = GivEnergyClient(entry.data[CONF_HOST], entry.data.get(CONF_PORT, DEFAULT_PORT))
    device = GivEnergyDevice(client, opts.get(CONF_SLOW_EVERY, DEFAULT_SLOW_EVERY))
    try:
        await device.probe()
    except GivEnergyError as err:
        await client.close()
        raise ConfigEntryNotReady(
            f"{entry.data[CONF_HOST]}: {err}. If GivTCP (or the GivEnergy app in local mode) "
            "is connected to this device, stop it — the dongle only serves one client."
        ) from err

    coordinator = GivEnergyCoordinator(hass, entry, device, opts.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL))
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = RuntimeData(device, coordinator)

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    _register_services(hass)
    return True


async def _async_reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.device.client.close()
    return ok


# ------------------------------------------------------------------- services
def _entry_from_call(hass: HomeAssistant, call: ServiceCall) -> GivEnergyConfigEntry:
    entry = hass.config_entries.async_get_entry(call.data["config_entry"])
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        raise ServiceValidationError("Unknown or not-loaded GivEnergy Gateway Modbus Coms entry")
    return entry


def _register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_PROBE):
        return

    async def probe(call: ServiceCall) -> ServiceResponse:
        entry = _entry_from_call(hass, call)
        device = entry.runtime_data.device
        try:
            result = await device.deep_probe()
        except GivEnergyError as err:
            raise HomeAssistantError(f"Probe failed: {err}") from err
        info = device.info
        lines = [f"**{info.model}** (DTC {info.device_type_code}, serial {info.serial}, fw {info.firmware})",
                 f"Device address {result['address']}", "", "| Block | Result |", "|---|---|"]
        for key, r in result["blocks"].items():
            extra = f" ({r['non_zero']} non-zero)" if r["status"] == "ok" else ""
            lines.append(f"| {key} | {r['status']}{extra} |")
        for addr, r in result["batteries"].items():
            if r["status"] == "ok":
                lines.append(f"| Battery {addr} IR60 | ok, serial `{r.get('serial') or '-'}` |")
        persistent_notification.async_create(
            hass, "\n".join(lines), title="GivEnergy register probe", notification_id=f"{DOMAIN}_probe"
        )
        _LOGGER.info("GivEnergy probe result for %s: %s", entry.title, result)
        return result

    async def read(call: ServiceCall) -> ServiceResponse:
        entry = _entry_from_call(hass, call)
        device = entry.runtime_data.device
        addr = call.data.get("device_address", device.address)
        is_ir = call.data["register_type"] == "input"
        try:
            resp = await device.client.read_registers(addr, is_ir, call.data["base"], call.data["count"])
        except GivEnergyError as err:
            raise HomeAssistantError(str(err)) from err
        return {
            "device_address": addr,
            "inverter_serial": resp.inverter_serial.strip("\x00 "),
            "registers": {str(resp.base + i): v for i, v in enumerate(resp.values)},
        }

    async def write(call: ServiceCall) -> None:
        entry = _entry_from_call(hass, call)
        reg, value = call.data["register"], call.data["value"]
        if reg not in WRITE_SAFE_REGISTERS:
            raise ServiceValidationError(
                f"HR{reg} is not on the allow-list ({', '.join(map(str, sorted(WRITE_SAFE_REGISTERS)))})"
            )
        await entry.runtime_data.coordinator.async_write(reg, value)

    entry_schema = {vol.Required("config_entry"): cv.string}
    hass.services.async_register(
        DOMAIN, SERVICE_PROBE, probe, schema=vol.Schema(entry_schema), supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN, SERVICE_READ, read, supports_response=SupportsResponse.ONLY,
        schema=vol.Schema({
            **entry_schema,
            vol.Required("register_type"): vol.In(["input", "holding"]),
            vol.Required("base"): vol.All(vol.Coerce(int), vol.Range(0, 65535)),
            vol.Optional("count", default=60): vol.All(vol.Coerce(int), vol.Range(1, 60)),
            vol.Optional("device_address"): vol.All(vol.Coerce(int), vol.Range(1, 255)),
        }),
    )
    hass.services.async_register(
        DOMAIN, SERVICE_WRITE, write,
        schema=vol.Schema({
            **entry_schema,
            vol.Required("register"): vol.All(vol.Coerce(int), vol.Range(0, 65535)),
            vol.Required("value"): vol.All(vol.Coerce(int), vol.Range(0, 65535)),
        }),
    )
