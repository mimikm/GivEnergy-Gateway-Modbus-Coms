"""Base entity + device registry helpers."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GivEnergyCoordinator


def main_device_info(coordinator: GivEnergyCoordinator) -> DeviceInfo:
    info = coordinator.device.info
    return DeviceInfo(
        identifiers={(DOMAIN, info.serial)},
        manufacturer="GivEnergy",
        model=info.model,
        model_id=info.device_type_code,
        name=f"GivEnergy {info.model} {info.serial}",
        serial_number=info.serial,
        sw_version=info.firmware,
        configuration_url=None,
    )


def battery_device_info(coordinator: GivEnergyCoordinator, addr: int) -> DeviceInfo:
    serial = coordinator.device.battery_serials[addr]
    n = addr - 0x31
    return DeviceInfo(
        identifiers={(DOMAIN, serial)},
        manufacturer="GivEnergy",
        model="LV battery",
        name=f"GivEnergy battery {n} {serial}",
        serial_number=serial,
        via_device=(DOMAIN, coordinator.device.info.serial),
    )


def reg_available(coordinator: GivEnergyCoordinator, input_regs: bool, reg: int) -> bool:
    dev = coordinator.device
    return any(b.input == input_regs and b.contains(reg) and b.key in dev.blocks_ok for b in dev.blocks)


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
    from .registers import CONTROLS, control_applies

    dev = coordinator.device
    return [
        c for c in CONTROLS
        if c.platform == platform and control_applies(c, dev.info.family, dev.blocks_ok, dev.extended_slots)
    ]


class GivEnergyControl(GivEnergyEntity):
    """Entity bound to one holding register."""

    def __init__(self, coordinator: GivEnergyCoordinator, control) -> None:
        super().__init__(coordinator, control.key, control.name, main_device_info(coordinator))
        self._control = control
        if control.icon:
            self._attr_icon = control.icon

    @property
    def _raw(self) -> int | None:
        return self.coordinator.device.inverter.hr.get(self._control.reg)

    async def _write(self, value: int) -> None:
        await self.coordinator.async_write(self._control.reg, int(value))
