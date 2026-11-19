"""Sensors built from whatever register blocks the probe found."""
from __future__ import annotations

import re

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import GivEnergyConfigEntry
from .coordinator import GivEnergyCoordinator
from .entity import GivEnergyEntity, battery_device_info, main_device_info, reg_available
from .registers import (
    BATTERY_IR,
    FAMILY_GATEWAY,
    GATEWAY_IR,
    INVERTER_HR_INFO,
    INVERTER_IR,
    Field,
)

_DIAGNOSTIC_KEYS = {"fault_code", "warning_code", "battery_capacity_ah", "gw_fault", "gw_first_inverter_serial",
                    "gw_aio1_serial", "num_cycles", "cap_design", "t_bms_mosfet"}


def _fields_for(coordinator: GivEnergyCoordinator, fields: list[Field]) -> list[Field]:
    out = []
    for f in fields:
        if all(reg_available(coordinator, f.input, r) for r in f.regs):
            out.append(f)
    return out


async def async_setup_entry(hass: HomeAssistant, entry: GivEnergyConfigEntry, add: AddEntitiesCallback) -> None:
    coordinator = entry.runtime_data.coordinator
    dev = coordinator.device
    main = main_device_info(coordinator)
    entities: list[SensorEntity] = []

    if dev.info.family == FAMILY_GATEWAY:
        aio_count = max(1, min(3, dev.inverter.ir.get(1700, 1) or 1))
        for f in _fields_for(coordinator, GATEWAY_IR):
            m = re.search(r"aio(\d)", f.key)
            if m and int(m.group(1)) > aio_count:
                continue  # skip AIO 2/3 when only one AIO is paralleled
            entities.append(GivEnergySensor(coordinator, f, main, bank="inverter"))
    else:
        for f in _fields_for(coordinator, INVERTER_IR + INVERTER_HR_INFO):
            entities.append(GivEnergySensor(coordinator, f, main, bank="inverter"))

    for addr, serial in dev.battery_serials.items():
        info = battery_device_info(coordinator, addr)
        for f in BATTERY_IR:
            entities.append(GivEnergySensor(coordinator, f, info, bank=addr, unique_prefix=serial))

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
