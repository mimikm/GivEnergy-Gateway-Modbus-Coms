"""Sensors, placed on the Power / Energy / System devices per the plan."""
from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .entity import GivEnergyEntity, battery_device_info, section_device_info
from .registers import Field

_DIAGNOSTIC_KEYS = {"fault_code", "warning_code", "battery_capacity_ah", "gw_fault", "gw_first_inverter_serial",
                    "gw_aio1_serial", "num_cycles", "cap_design", "t_bms_mosfet"}


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    entities: list[SensorEntity] = []
    for sp in coordinator.plan.sensors:
        if sp.bank == "inverter":
            info = section_device_info(coordinator, sp.section)
            entities.append(GivEnergySensor(coordinator, sp.field, info, "inverter"))
        else:
            serial = coordinator.device.battery_serials[sp.bank]
            info = battery_device_info(coordinator, sp.bank)
            entities.append(GivEnergySensor(coordinator, sp.field, info, sp.bank, unique_prefix=serial))
    add(entities)


class GivEnergySensor(GivEnergyEntity, SensorEntity):
    def __init__(self, coordinator, field: Field, device_info, bank, unique_prefix=None) -> None:
        super().__init__(coordinator, field.key, field.name, device_info, unique_prefix)
        self._field = field
        self._bank = bank
        self._attr_native_unit_of_measurement = field.unit
        self._attr_device_class = SensorDeviceClass(field.device_class) if field.device_class else None
        self._attr_state_class = SensorStateClass(field.state_class) if field.state_class else None
        self._attr_entity_registry_enabled_default = field.enabled
        if field.key in _DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if field.options is not None:
            self._attr_state_class = None

    def _regs(self) -> dict[int, int]:
        dev = self.coordinator.device
        bank = dev.inverter if self._bank == "inverter" else dev.batteries.get(self._bank)
        if bank is None:
            return {}
        return bank.ir if self._field.input else bank.hr

    @property
    def native_value(self):
        try:
            return self._field.decode(self._regs())
        except (KeyError, TypeError, ValueError):
            return None
