"""Diagnostics download — full register dump for mapping new devices."""
from __future__ import annotations

from homeassistant.core import HomeAssistant

from . import GivEnergyConfigEntry


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: GivEnergyConfigEntry) -> dict:
    dev = entry.runtime_data.device
    coord = entry.runtime_data.coordinator
    info = dev.info
    return {
        "host": entry.data.get("host"),
        "device": {
            "device_type_code": info.device_type_code,
            "family": info.family,
            "model": info.model,
            "serial": info.serial,
            "firmware": info.firmware,
            "device_address": f"0x{dev.address:02X}",
            "extended_slots": dev.extended_slots,
            "adapter_serial": dev.client.adapter_serial,
        },
        "blocks_ok": sorted(dev.blocks_ok),
        "sections": {
            s: {"interval": coord.intervals.get(s), "blocks": sorted(coord.plan.blocks.get(s, set()))}
            for s in ("power", "energy", "controls", "system")
        },
        "entity_count": len(coord.plan.sensors) + len(coord.plan.controls) + int(coord.plan.battery_mode),
        "block_failures": dev.block_failures,
        "client_stats": dev.client.stats,
        "input_registers": {str(k): v for k, v in sorted(dev.inverter.ir.items())},
        "holding_registers": {str(k): v for k, v in sorted(dev.inverter.hr.items())},
        "batteries": {
            f"0x{a:02X}": {"serial": dev.battery_serials.get(a),
                           "input_registers": {str(k): v for k, v in sorted(b.ir.items())}}
            for a, b in dev.batteries.items()
        },
    }
