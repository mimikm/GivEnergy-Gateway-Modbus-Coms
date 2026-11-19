"""Decide which entities exist and which register blocks each section needs.

Pure Python (no HA imports). Both the platforms and the poller use the same
plan, so a disabled section is neither shown nor polled.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

from .registers import (
    BATTERY_IR,
    CONTROL_FAMILIES,
    CONTROLS,
    FAMILY_GATEWAY,
    GATEWAY_IR,
    INVERTER_HR_INFO,
    INVERTER_IR,
    SECTION_CONTROLS,
    SECTION_SYSTEM,
    SECTIONS,
    Control,
    Field,
    control_applies,
)


@dataclass
class SensorPlan:
    field: Field
    section: str
    bank: object  # "inverter" or battery device address (int)


@dataclass
class Plan:
    sensors: list[SensorPlan] = field(default_factory=list)
    controls: list[Control] = field(default_factory=list)
    battery_mode: bool = False
    # section -> register block keys it needs ("IR0", "HR60", ...)
    blocks: dict[str, set[str]] = field(default_factory=lambda: {s: set() for s in SECTIONS})
    # battery packs are polled with the System section
    poll_batteries: bool = False

    def unique_keys(self) -> set[str]:
        """Entity keys (without serial prefix) the plan creates on the main device."""
        keys = {sp.field.key for sp in self.sensors if sp.bank == "inverter"}
        keys |= {c.key for c in self.controls}
        if self.battery_mode:
            keys.add("battery_mode")
        return keys


def _block_key(device, input_regs: bool, reg: int) -> str | None:
    for b in device.blocks:
        if b.input == input_regs and b.contains(reg) and b.key in device.blocks_ok:
            return b.key
    return None


def _aio_index_ok(key: str, aio_count: int) -> bool:
    m = re.search(r"aio(\d)", key)
    return not m or int(m.group(1)) <= aio_count


def build_plan(device, enabled: set[str]) -> Plan:
    plan = Plan()
    info = device.info

    if info.family == FAMILY_GATEWAY:
        aio_count = max(1, min(3, device.inverter.ir.get(1700, 1) or 1))
        fields = [f for f in GATEWAY_IR if _aio_index_ok(f.key, aio_count)]
    else:
        fields = INVERTER_IR + INVERTER_HR_INFO

    for f in fields:
        keys = [_block_key(device, f.input, r) for r in f.regs]
        if None in keys:
            continue  # device doesn't answer that block
        section = f.group
        if section not in enabled:
            continue
        plan.sensors.append(SensorPlan(f, section, "inverter"))
        plan.blocks[section].update(keys)

    if SECTION_SYSTEM in enabled and device.batteries:
        plan.poll_batteries = True
        for addr in device.batteries:
            for f in BATTERY_IR:
                plan.sensors.append(SensorPlan(f, SECTION_SYSTEM, addr))

    if SECTION_CONTROLS in enabled:
        for c in CONTROLS:
            if control_applies(c, info.family, device.blocks_ok, device.extended_slots):
                key = _block_key(device, False, c.reg)
                if key:
                    plan.controls.append(c)
                    plan.blocks[SECTION_CONTROLS].add(key)
        if info.family in CONTROL_FAMILIES and "HR0" in device.blocks_ok:
            plan.battery_mode = True
            plan.blocks[SECTION_CONTROLS].add("HR0")
    return plan
