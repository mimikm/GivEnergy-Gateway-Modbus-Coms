"""Base entities and the per-section device layout.

Device tree for one config entry (name prefix chosen by the user):

    GivEnergy Gateway              ← main device: System section + Re-probe button
    ├── GivEnergy Gateway Power
    ├── GivEnergy Gateway Energy
    ├── GivEnergy Gateway Controls
    └── GivEnergy Gateway Battery 1 (LV packs, System section)
"""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyCoordinator
from .registers import SECTION_LABELS, SECTION_SYSTEM


def section_identifier(serial: str, section: str) -> str:
    return serial if section == SECTION_SYSTEM else f"{serial}_{section}"


def section_device_info(coordinator: GivEnergyCoordinator, section: str) -> DeviceInfo:
    info = coordinator.device.info
    base = coordinator.display_name
    if section == SECTION_SYSTEM:
        return DeviceInfo(
            identifiers={(DOMAIN, info.serial)},
            manufacturer="GivEnergy",
            model=info.model,
            model_id=info.device_type_code,
            name=base,
            serial_number=info.serial,
            sw_version=info.firmware,
        )
    label = SECTION_LABELS[section]
    return DeviceInfo(
        identifiers={(DOMAIN, section_identifier(info.serial, section))},
        manufacturer="GivEnergy",
        model=f"{info.model} · {label}",
        name=f"{base} {label}",
        via_device=(DOMAIN, info.serial),
    )


def battery_device_info(coordinator: GivEnergyCoordinator, addr: int) -> DeviceInfo:
    serial = coordinator.device.battery_serials[addr]
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="GivEnergy",
        model="LV battery",
        name=f"{coordinator.display_name} Battery {addr - 0x31}",
        serial_number=serial,
        via_device=(DOMAIN, coordinator.device.info.serial),
    )


class GivEnergyEntity(CoordinatorEntity[GivEnergyCoordinator]):
    _attr_has_entity_name = True

    def __init__(self, coordinator: GivEnergyCoordinator, key: str, name: str, device_info: DeviceInfo,
                 unique_prefix: str | None = None) -> None:
        super().__init__(coordinator)
        prefix = unique_prefix or coordinator.device.info.serial
        self._attr_unique_id = f"{prefix}_{key}"
        self._attr_name = name
        self._attr_device_info = device_info
        self._key = key


def controls_for(coordinator: GivEnergyCoordinator, platform: str):
    return [c for c in coordinator.plan.controls if c.platform == platform]


class GivEnergyControl(GivEnergyEntity):
    """Entity bound to one holding register, on the Controls device."""

    def __init__(self, coordinator: GivEnergyCoordinator, control) -> None:
        from .registers import SECTION_CONTROLS

        super().__init__(coordinator, control.key, control.name, section_device_info(coordinator, SECTION_CONTROLS))
        self._control = control
        if control.icon:
            self._attr_icon = control.icon

    @property
    def _raw(self) -> int | None:
        return self.coordinator.device.inverter.hr.get(self._control.reg)

    async def _write(self, value: int) -> None:
        await self.coordinator.async_write(self._control.reg, int(value))
