"""Smoke test of the HA platforms using tiny stand-ins for HA + voluptuous.

Not a substitute for running in Home Assistant — it catches wiring mistakes
(wrong attribute names, bad imports, entity selection) without HA installed.
"""
import asyncio
import enum
import sys
import types
from datetime import time as dtime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from test_core import FakeDongle, gen1_hybrid, serial_regs  # noqa: E402


def _mod(name, **attrs):
    m = sys.modules.get(name) or types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Generic:
    def __class_getitem__(cls, _):
        return cls


class Coordinator(_Generic):
    def __init__(self, hass, logger, config_entry=None, name=None, update_interval=None):
        self.hass, self.config_entry = hass, config_entry
        self.listeners = 0

    async def async_config_entry_first_refresh(self):
        await self._async_update_data()

    def async_update_listeners(self):
        self.listeners += 1


class CoordinatorEntity(_Generic):
    def __init__(self, coordinator):
        self.coordinator = coordinator


class StrEnum(str, enum.Enum):
    pass


SensorDeviceClass = StrEnum("SensorDeviceClass", {k.upper(): k for k in
    ["power", "voltage", "current", "frequency", "temperature", "battery", "energy", "energy_storage"]})
SensorStateClass = StrEnum("SensorStateClass", {k.upper(): k for k in ["measurement", "total_increasing", "total"]})

_mod("voluptuous", Schema=lambda *a, **k: None, Required=lambda *a, **k: a[0], Optional=lambda *a, **k: a[0],
     All=lambda *a: None, Coerce=lambda t: t, Range=lambda *a: None, In=lambda *a: None)
_mod("homeassistant")
_mod("homeassistant.components")
_mod("homeassistant.components.persistent_notification", async_create=lambda *a, **k: None)
sys.modules["homeassistant.components"].persistent_notification = sys.modules["homeassistant.components.persistent_notification"]
_mod("homeassistant.config_entries", ConfigEntry=_Generic, ConfigFlow=type("CF", (), {"__init_subclass__": classmethod(lambda c, **k: None)}),
     ConfigFlowResult=dict, OptionsFlow=object)
_mod("homeassistant.const", CONF_HOST="host", CONF_PORT="port", CONF_SCAN_INTERVAL="scan_interval",
     EntityCategory=StrEnum("EntityCategory", {"CONFIG": "config", "DIAGNOSTIC": "diagnostic"}))
_mod("homeassistant.core", HomeAssistant=object, ServiceCall=object, ServiceResponse=dict, callback=lambda f: f,
     SupportsResponse=StrEnum("SR", {"OPTIONAL": "optional", "ONLY": "only"}))
_mod("homeassistant.exceptions", ConfigEntryNotReady=Exception, HomeAssistantError=Exception,
     ServiceValidationError=Exception)
_mod("homeassistant.helpers")
_mod("homeassistant.helpers.config_validation", string=str)
_mod("homeassistant.helpers.update_coordinator", DataUpdateCoordinator=Coordinator,
     CoordinatorEntity=CoordinatorEntity, UpdateFailed=Exception)
_mod("homeassistant.helpers.device_registry", DeviceInfo=dict)
_mod("homeassistant.helpers.entity_platform", AddEntitiesCallback=object)
_mod("homeassistant.components.sensor", SensorEntity=object, SensorDeviceClass=SensorDeviceClass,
     SensorStateClass=SensorStateClass)
_mod("homeassistant.components.switch", SwitchEntity=object)
_mod("homeassistant.components.number", NumberEntity=object,
     NumberMode=StrEnum("NumberMode", {"BOX": "box"}))
_mod("homeassistant.components.select", SelectEntity=object)
_mod("homeassistant.components.time", TimeEntity=object)
_mod("homeassistant.components.button", ButtonEntity=object)

sys.path.insert(0, str(Path(__file__).parents[1]))
# test_core registered the pure modules under "gem"; import the real package now
for k in [k for k in sys.modules if k.startswith("gem")]:
    del sys.modules[k]
import custom_components.givenergy_modbus as integ  # noqa: E402
from custom_components.givenergy_modbus import (  # noqa: E402
    button, diagnostics, number, select, sensor, switch, time as time_platform,
)
from custom_components.givenergy_modbus.client import GivEnergyClient  # noqa: E402
from custom_components.givenergy_modbus.coordinator import GivEnergyCoordinator  # noqa: E402
from custom_components.givenergy_modbus.device import GivEnergyDevice  # noqa: E402


class Entry:
    entry_id = "abc"
    domain = "givenergy_modbus"
    data = {"host": "127.0.0.1"}
    options = {}


async def build(dongle):
    server = await asyncio.start_server(dongle.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = GivEnergyClient("127.0.0.1", port, timeout=0.5)
    dev = GivEnergyDevice(client)
    await dev.probe()
    entry = Entry()
    coord = GivEnergyCoordinator(None, entry, dev, 15)
    await coord.async_config_entry_first_refresh()
    entry.runtime_data = integ.RuntimeData(dev, coord)
    ents = {}
    for platform in (sensor, switch, number, select, time_platform, button):
        out = []
        await platform.async_setup_entry(None, entry, lambda e: out.extend(e))
        ents[platform.__name__.rsplit(".", 1)[1]] = {e._attr_unique_id.split("_", 1)[1]: e for e in out}
    return server, client, dev, coord, entry, ents


def test_hybrid_entities():
    dongle = gen1_hybrid()

    async def go():
        server, client, dev, coord, entry, ents = await build(dongle)
        try:
            s = ents["sensor"]
            assert s["p_grid_import"].native_value == 350
            assert s["battery_soc"].native_value == 63
            assert s["status"].native_value == "Normal"
            assert s["v_out"].native_value == 52.0  # battery pack (prefix = pack serial)
            assert "gw_p_grid" not in s
            assert ents["switch"]["enable_charge"].is_on is True
            sel = ents["select"]
            assert "battery_pause_mode" not in sel  # Gen1 has no HR300 block
            assert sel["battery_mode"].current_option == "Eco"
            t = ents["time"]
            assert t["charge_slot_1_start"].native_value == dtime(0, 30)
            assert t["charge_slot_2_start"]._control.reg == 31  # legacy map on Gen1
            await t["charge_slot_1_end"].async_set_value(dtime(5, 0))
            await sel["battery_mode"].async_select_option("Timed Export")
            await ents["number"]["charge_target_soc"].async_set_native_value(85)
            assert dongle.writes == [(95, 500), (27, 0), (59, 1), (116, 85)]
            assert sel["battery_mode"].current_option == "Timed Export"
            d = await diagnostics.async_get_config_entry_diagnostics(None, entry)
            assert d["device"]["model"] == "Hybrid Gen 1" and d["holding_registers"]["116"] == 85
        finally:
            await client.close()
            server.close()

    asyncio.run(go())


def test_aio_extended_slots():
    hr = {0: 0x8001, 21: 312, 27: 1, 59: 1, 96: 0, 243: 2300, 318: 2, 313: 100,
          **serial_regs(13, "CK2527G181")}
    ir = {0: 1, 52: 3000, 59: 40, 180: 1}
    dongle = FakeDongle(ir, hr, serial="CK2527G181", push=False)

    async def go():
        server, client, dev, coord, entry, ents = await build(dongle)
        try:
            assert dev.info.family == "aio" and dev.extended_slots
            assert ents["sensor"]["p_battery_discharge"].native_value == 3000
            assert ents["select"]["battery_pause_mode"].current_option == "Pause discharge"
            assert ents["select"]["battery_mode"].current_option == "Timed Demand"
            assert ents["time"]["charge_slot_2_start"]._control.reg == 243
            assert ents["time"]["charge_slot_2_start"].native_value == dtime(23, 0)
            assert "charge_power_percent" in ents["number"]
            assert dev.battery_serials == {}
        finally:
            await client.close()
            server.close()

    asyncio.run(go())
