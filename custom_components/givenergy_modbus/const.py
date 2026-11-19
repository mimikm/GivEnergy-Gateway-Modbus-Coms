"""Constants for GivEnergy Gateway Modbus Coms."""
from __future__ import annotations

from dataclasses import dataclass

from .registers import (
    FAMILY_AC,
    FAMILY_AIO,
    FAMILY_GATEWAY,
    FAMILY_HYBRID,
    SECTION_CONTROLS,
    SECTION_ENERGY,
    SECTION_POWER,
    SECTION_SYSTEM,
    SECTIONS,
)

DOMAIN = "givenergy_modbus"
DEFAULT_PORT = 8899
MIN_INTERVAL = 5
MAX_INTERVAL = 3600

CONF_DEVICE_NAME = "device_name"

DEFAULT_INTERVALS = {
    SECTION_POWER: 10,
    SECTION_ENERGY: 60,
    SECTION_CONTROLS: 60,
    SECTION_SYSTEM: 30,
}

PLATFORMS = ["sensor", "switch", "number", "select", "time", "button"]

SERVICE_PROBE = "probe_registers"
SERVICE_READ = "read_registers"
SERVICE_WRITE = "write_register"


def conf_enabled(section: str) -> str:
    return f"{section}_enabled"


def conf_interval(section: str) -> str:
    return f"{section}_interval"


@dataclass
class SectionSetting:
    enabled: bool
    interval: int


def section_settings(opts: dict) -> dict[str, SectionSetting]:
    """Read per-section settings; falls back to v0.1 'scan_interval' for Power."""
    out = {}
    for s in SECTIONS:
        default = DEFAULT_INTERVALS[s]
        if s == SECTION_POWER and "scan_interval" in opts:
            default = opts["scan_interval"]
        out[s] = SectionSetting(
            bool(opts.get(conf_enabled(s), True)),
            int(opts.get(conf_interval(s), default)),
        )
    return out


_SHORT_MODEL = {
    FAMILY_GATEWAY: "Gateway",
    FAMILY_AIO: "AIO",
    FAMILY_AC: "AC",
    FAMILY_HYBRID: "Hybrid",
}


def default_device_name(info) -> str:
    return f"GivEnergy {_SHORT_MODEL.get(info.family, info.model)}"
