"""Register maps, model detection and value decoding (no HA imports).

Sources: dewet22/givenergy-modbus register model (single-phase inverter,
LV battery, Gateway V1/V2) and its poll manifest, cross-checked against the
GivTCP async fork. Anything marked "unverified" should be checked against a
`probe_registers` dump from real hardware.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

INVERTER_ADDRESSES = (0x11, 0x31)  # 0x11 canonical; some Gen1/AC answer on 0x31
BATTERY_ADDRESSES = tuple(range(0x32, 0x38))  # LV packs 1..6

FAMILY_HYBRID = "hybrid"
FAMILY_AC = "ac"
FAMILY_AIO = "aio"
FAMILY_GATEWAY = "gateway"
FAMILY_EMS = "ems"
FAMILY_3PH = "three_phase"
FAMILY_UNKNOWN = "unknown"

INVERTER_FAMILIES = {FAMILY_HYBRID, FAMILY_AC, FAMILY_AIO, FAMILY_3PH, FAMILY_UNKNOWN}
CONTROL_FAMILIES = {FAMILY_HYBRID, FAMILY_AC, FAMILY_AIO, FAMILY_GATEWAY}


@dataclass(frozen=True)
class Block:
    input: bool  # True = IR (fc 4), False = HR (fc 3)
    base: int
    count: int

    @property
    def key(self) -> str:
        return f"{'IR' if self.input else 'HR'}{self.base}"

    def contains(self, reg: int) -> bool:
        return self.base <= reg < self.base + self.count


IR = lambda b, c=60: Block(True, b, c)  # noqa: E731
HR = lambda b, c=60: Block(False, b, c)  # noqa: E731

# Polled every cycle for any inverter-like device
FAST_BLOCKS = [IR(0), IR(180)]
# Config (holding) blocks — polled every Nth cycle and after writes
SLOW_BLOCKS = [HR(0), HR(60), HR(120)]
EXTENDED_SLOT_BLOCK = HR(240)
AC_CONFIG_BLOCK = HR(300)
GATEWAY_BLOCKS = [IR(1600), IR(1660), IR(1720), IR(1780), IR(1840, 20)]
BATTERY_BLOCK = IR(60)

# Wide sweep used by the "probe_registers" service
DEEP_PROBE = {
    "inverter": [IR(b) for b in (0, 60, 120, 180, 240, 300)]
    + [IR(b) for b in (1000, 1060, 1120, 1180, 1240, 1300)]
    + [IR(b) for b in (1600, 1660, 1720, 1780)] + [IR(1840, 20), IR(2040, 55)]
    + [HR(b) for b in (0, 60, 120, 180, 240, 300)]
    + [HR(499, 12), HR(540), HR(1000), HR(1060), HR(2040, 36), HR(4080), HR(20000, 52)],
    "batteries": [(addr, BATTERY_BLOCK) for addr in BATTERY_ADDRESSES]
    + [(addr, BATTERY_BLOCK) for addr in range(0x50, 0x54)]
    + [(0x70, BATTERY_BLOCK), (0xA0, BATTERY_BLOCK)],
}


# ------------------------------------------------------------------ detection
@dataclass
class DeviceInfo:
    device_type_code: str
    family: str
    model: str
    serial: str
    arm_fw: int
    dsp_fw: int
    phases: int

    @property
    def firmware(self) -> str:
        return f"D0.{self.dsp_fw}-A0.{self.arm_fw}"


_TWO_DIGIT = {
    "21": (FAMILY_HYBRID, "Polar"),
    "41": (FAMILY_3PH, "AIO Commercial"),
    "51": (FAMILY_EMS, "EMS Commercial"),
    "81": (FAMILY_HYBRID, "Hybrid HV Gen 3"),
    "82": (FAMILY_AIO, "All-in-One Hybrid"),
    "83": (FAMILY_HYBRID, "Hybrid Gen 4"),
}
_ONE_DIGIT = {
    "2": (FAMILY_HYBRID, "Hybrid"),
    "3": (FAMILY_AC, "AC Coupled"),
    "4": (FAMILY_3PH, "Hybrid 3-phase"),
    "5": (FAMILY_EMS, "EMS"),
    "6": (FAMILY_3PH, "AC 3-phase"),
    "7": (FAMILY_GATEWAY, "Gateway"),
    "8": (FAMILY_AIO, "All-in-One"),
}


def decode_serial(regs: dict[int, int], start: int) -> str:
    out = bytearray()
    for r in range(start, start + 5):
        v = regs.get(r, 0)
        out += bytes((v >> 8, v & 0xFF))
    return out.decode("latin-1").strip("\x00 ")


def is_valid_serial(s: str) -> bool:
    return len(s) == 10 and s.isalnum() and s != "0" * 10


def detect_device(hr: dict[int, int]) -> DeviceInfo:
    raw = hr.get(0, 0)
    dtc = f"{raw:04x}"
    arm = hr.get(21, 0)
    dsp = hr.get(19, 0)
    if dtc[:2] == "20":
        gen = arm // 100
        model = "Hybrid Gen 3" if gen == 3 else "Hybrid Gen 2" if gen in (8, 9) else "Hybrid Gen 1"
        family = FAMILY_HYBRID
    elif dtc[:2] in _TWO_DIGIT:
        family, model = _TWO_DIGIT[dtc[:2]]
    elif dtc[0] in _ONE_DIGIT:
        family, model = _ONE_DIGIT[dtc[0]]
    else:
        family, model = FAMILY_UNKNOWN, f"Unknown (DTC {dtc})"
    phases = 3 if dtc[0] in ("4", "6") else 1
    return DeviceInfo(dtc, family, model, decode_serial(hr, 13), arm, dsp, phases)


def has_extended_slots(info: DeviceInfo, blocks_ok: set[str]) -> bool:
    if EXTENDED_SLOT_BLOCK.key not in blocks_ok:
        return False
    if info.family in (FAMILY_AIO, FAMILY_GATEWAY):
        return True
    return info.family == FAMILY_HYBRID and ("Gen 3" in info.model or "Gen 4" in info.model)


# ------------------------------------------------------------------- decoding
def u32(regs, hi, lo):
    if hi not in regs or lo not in regs:
        return None
    return (regs[hi] << 16) | regs[lo]


def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


@dataclass(frozen=True)
class Field:
    key: str
    name: str
    regs: tuple[int, ...]
    input: bool = True
    scale: float = 1
    signed: bool = False
    negate: bool = False
    unit: str | None = None
    device_class: str | None = None
    state_class: str | None = "measurement"
    families: frozenset | None = None  # None = any inverter-like
    enabled: bool = True
    options: dict | None = None
    valid: tuple[float, float] | None = None
    custom: Callable[[dict[int, int]], object] | None = None
    precision: int | None = None

    def decode(self, regs: dict[int, int]):
        if self.custom:
            return self.custom(regs)
        if len(self.regs) == 2:
            raw = u32(regs, *self.regs)
        else:
            raw = regs.get(self.regs[0])
        if raw is None:
            return None
        if self.signed:
            raw = s16(raw)
        if self.options is not None:
            return self.options.get(raw, f"Unknown ({raw})")
        val = raw * self.scale
        if self.negate:
            val = -val
        if self.valid and not (self.valid[0] <= val <= self.valid[1]):
            return None
        if self.scale != 1:
            val = round(val, 3)
        return val


W, V, A, HZ, C, KWH, PCT, AH = "W", "V", "A", "Hz", "°C", "kWh", "%", "Ah"
TOTAL = "total_increasing"

STATUS = {0: "Waiting", 1: "Normal", 2: "Warning", 3: "Fault", 4: "Firmware update"}
GATEWAY_WORK_MODE = {0: "Unknown", 1: "Grid tied", 2: "Off grid", 3: "Bypass"}  # unverified


def _sum(*fields: tuple[int, bool]):
    def f(regs):
        vals = []
        for reg, signed in fields:
            if reg not in regs:
                return None
            vals.append(s16(regs[reg]) if signed else regs[reg])
        return sum(vals)
    return f


def _pos(reg: int, sign: int):
    """Positive part of a signed power register (sign=+1) or of its negation (-1)."""
    def f(regs):
        if reg not in regs:
            return None
        return max(0, s16(regs[reg]) * sign)
    return f


def _hhmm_sensor(reg):
    def f(regs):
        v = regs.get(reg)
        return hhmm_to_str(v) if v is not None else None
    return f


def hhmm_to_str(v: int) -> str | None:
    h, m = divmod(v, 100)
    return f"{h:02d}:{m:02d}" if h < 24 and m < 60 else None


# ----- single-phase inverter / AIO input registers (device address 0x11)
INVERTER_IR: list[Field] = [
    Field("status", "Status", (0,), options=STATUS, state_class=None),
    Field("v_pv1", "PV1 voltage", (1,), scale=0.1, unit=V, device_class="voltage", valid=(0, 1000)),
    Field("v_pv2", "PV2 voltage", (2,), scale=0.1, unit=V, device_class="voltage", valid=(0, 1000)),
    Field("v_ac1", "Grid voltage", (5,), scale=0.1, unit=V, device_class="voltage", valid=(0, 500)),
    Field("i_pv1", "PV1 current", (8,), scale=0.1, unit=A, device_class="current", enabled=False),
    Field("i_pv2", "PV2 current", (9,), scale=0.1, unit=A, device_class="current", enabled=False),
    Field("i_ac1", "Inverter current", (10,), scale=0.1, unit=A, device_class="current", enabled=False),
    Field("f_ac1", "Grid frequency", (13,), scale=0.01, unit=HZ, device_class="frequency", valid=(40, 70)),
    Field("p_pv1", "PV1 power", (18,), unit=W, device_class="power", valid=(0, 50000)),
    Field("p_pv2", "PV2 power", (20,), unit=W, device_class="power", valid=(0, 50000)),
    Field("p_pv", "PV power", (18, 20), unit=W, device_class="power", custom=_sum((18, False), (20, False))),
    Field("p_grid", "Grid power", (30,), signed=True, unit=W, device_class="power"),  # +export / -import
    Field("p_grid_import", "Grid import power", (30,), unit=W, device_class="power", custom=_pos(30, -1)),
    Field("p_grid_export", "Grid export power", (30,), unit=W, device_class="power", custom=_pos(30, 1)),
    Field("p_inverter", "Inverter output power", (24,), signed=True, unit=W, device_class="power"),
    Field("p_eps", "EPS power", (31,), unit=W, device_class="power", valid=(0, 50000)),
    Field("p_load", "Load power", (42,), unit=W, device_class="power", valid=(0, 50000)),
    Field("p_battery", "Battery power", (52,), signed=True, unit=W, device_class="power"),  # +discharge
    Field("p_battery_charge", "Battery charge power", (52,), unit=W, device_class="power", custom=_pos(52, -1)),
    Field("p_battery_discharge", "Battery discharge power", (52,), unit=W, device_class="power", custom=_pos(52, 1)),
    Field("battery_soc", "Battery SOC", (59,), unit=PCT, device_class="battery", valid=(0, 100)),
    Field("v_battery", "Battery voltage", (50,), scale=0.01, unit=V, device_class="voltage", valid=(0, 600)),
    Field("i_battery", "Battery current", (51,), signed=True, scale=0.01, unit=A, device_class="current"),
    Field("v_eps", "EPS voltage", (53,), scale=0.1, unit=V, device_class="voltage", enabled=False),
    Field("f_eps", "EPS frequency", (54,), scale=0.01, unit=HZ, device_class="frequency", enabled=False),
    Field("t_heatsink", "Inverter temperature", (41,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-40, 100)),
    Field("t_charger", "Charger temperature", (55,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-40, 100)),
    Field("t_battery", "Battery temperature", (56,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-40, 100)),
    Field("fault_code", "Fault code", (39,), state_class=None, custom=lambda r: f"{r[39]:04X}" if 39 in r else None),
    Field("warning_code", "Warning code", (40,), state_class=None, custom=lambda r: f"{r[40]:04X}" if 40 in r else None),
    # Energy — today
    Field("e_pv1_today", "PV1 energy today", (17,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_pv2_today", "PV2 energy today", (19,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_pv_today", "PV energy today", (17, 19), unit=KWH, device_class="energy", state_class=TOTAL,
          custom=lambda r: round((r[17] + r[19]) / 10, 1) if 17 in r and 19 in r else None),
    Field("e_export_today", "Grid export today", (25,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_import_today", "Grid import today", (26,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_ac_charge_today", "AC charge today", (35,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_battery_charge_today", "Battery charge today", (36,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_battery_discharge_today", "Battery discharge today", (37,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_inverter_out_today", "Inverter output today", (44,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    # Energy — lifetime (uint32 hi, lo)
    Field("e_pv_total", "PV energy total", (11, 12), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_export_total", "Grid export total", (21, 22), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_import_total", "Grid import total", (32, 33), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_inverter_in_total", "Inverter input total", (27, 28), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL, enabled=False),
    Field("e_inverter_out_total", "Inverter output total", (45, 46), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_battery_throughput", "Battery throughput total", (6, 7), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL),
    Field("e_battery_discharge_total", "Battery discharge total", (180,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL, enabled=False),
    Field("e_battery_charge_total", "Battery charge total", (181,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL, enabled=False),
]

# ----- holding registers shown read-only as diagnostics
INVERTER_HR_INFO: list[Field] = [
    Field("battery_capacity_ah", "Battery capacity", (55,), input=False, unit=AH, state_class=None),
    Field("charge_slot_1_text", "Charge slot 1", (94,), input=False, state_class=None, enabled=False,
          custom=lambda r: f"{hhmm_to_str(r[94])}-{hhmm_to_str(r[95])}" if 94 in r and 95 in r else None),
    Field("discharge_slot_1_text", "Discharge slot 1", (56,), input=False, state_class=None, enabled=False,
          custom=lambda r: f"{hhmm_to_str(r[56])}-{hhmm_to_str(r[57])}" if 56 in r and 57 in r else None),
]

# ----- LV battery pack (device 0x32..0x37, IR 60-119)
BATTERY_IR: list[Field] = [
    Field("soc", "SOC", (100,), unit=PCT, device_class="battery", valid=(0, 100)),
    Field("v_out", "Voltage", (82, 83), scale=0.001, unit=V, device_class="voltage", valid=(16, 80)),
    Field("i_battery", "Current", (95,), signed=True, scale=0.01, unit=A, device_class="current", valid=(-300, 300)),
    Field("t_max", "Max temperature", (103,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-60, 150)),
    Field("t_min", "Min temperature", (104,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-60, 150)),
    Field("t_bms_mosfet", "BMS temperature", (81,), signed=True, scale=0.1, unit=C, device_class="temperature", valid=(-60, 150), enabled=False),
    Field("num_cycles", "Cycles", (96,), state_class=TOTAL),
    Field("cap_remaining", "Remaining capacity", (88, 89), scale=0.01, unit=AH),
    Field("cap_calibrated", "Full capacity", (84, 85), scale=0.01, unit=AH),
    Field("cap_design", "Design capacity", (86, 87), scale=0.01, unit=AH, enabled=False),
    Field("e_remaining", "Remaining energy", (88, 89), unit=KWH, device_class="energy_storage",
          custom=lambda r: round(u32(r, 88, 89) * 0.01 * u32(r, 82, 83) * 0.001 / 1000, 2)
          if u32(r, 88, 89) is not None and u32(r, 82, 83) is not None else None),
    *[
        Field(f"v_cell_{n:02d}", f"Cell {n} voltage", (59 + n,), scale=0.001, unit=V, device_class="voltage",
              valid=(1, 5), enabled=False)
        for n in range(1, 17)
    ],
]


# ----- Gateway (IR 1600-1859 at 0x11)
def _gw_total(lo_first_pair: tuple[int, int]):
    """Gateway V1 stores totals hi,lo; V2 firmware swaps them. Pick the sane one."""
    a, b = lo_first_pair

    def f(regs):
        v1, v2 = u32(regs, a, b), u32(regs, b, a)
        if v1 is None:
            return None
        # A home lifetime total stays under 1 GWh (10M raw); a swapped word pair explodes.
        val = v1 if v1 < 10_000_000 or v2 is None or v2 >= v1 else v2
        return round(val / 10, 1)
    return f


def _gw_energy(key, name, reg, enabled=True):
    return Field(key, name, (reg,), scale=0.1, unit=KWH, device_class="energy", state_class=TOTAL, enabled=enabled)


def _gw_total_field(key, name, hi, enabled=True):
    return Field(key, name, (hi, hi + 1), unit=KWH, device_class="energy", state_class=TOTAL,
                 custom=_gw_total((hi, hi + 1)), enabled=enabled)


GATEWAY_IR: list[Field] = [
    Field("gw_work_mode", "Work mode", (1604,), options=GATEWAY_WORK_MODE, state_class=None, enabled=False),
    Field("gw_v_grid", "Grid voltage", (1608,), signed=True, scale=0.1, unit=V, device_class="voltage", valid=(0, 500)),
    Field("gw_i_grid", "Grid current", (1609,), signed=True, scale=0.1, unit=A, device_class="current"),
    Field("gw_v_load", "Load voltage", (1610,), scale=0.1, unit=V, device_class="voltage", valid=(0, 500), enabled=False),
    Field("gw_i_load", "Load current", (1611,), scale=0.1, unit=A, device_class="current", enabled=False),
    Field("gw_i_pv", "PV current", (1612,), signed=True, scale=0.1, unit=A, device_class="current", enabled=False),
    Field("gw_p_grid", "Grid power", (1616,), signed=True, unit=W, device_class="power"),
    Field("gw_p_pv", "PV power", (1617,), unit=W, device_class="power", valid=(0, 50000)),
    Field("gw_p_load", "Load power", (1618,), unit=W, device_class="power", valid=(0, 50000)),
    Field("gw_p_liberty", "Liberty power", (1619,), signed=True, negate=True, unit=W, device_class="power"),
    Field("gw_fault", "Fault codes", (1622, 1623), state_class=None, enabled=False,
          custom=lambda r: f"{u32(r, 1622, 1623):08X}" if u32(r, 1622, 1623) is not None else None),
    Field("gw_v_grid_relay", "Grid relay voltage", (1624,), scale=0.1, unit=V, device_class="voltage", enabled=False),
    Field("gw_v_inverter_relay", "Inverter relay voltage", (1625,), scale=0.1, unit=V, device_class="voltage", enabled=False),
    _gw_energy("gw_e_import_today", "Grid import today", 1640),
    _gw_total_field("gw_e_import_total", "Grid import total", 1641),
    _gw_energy("gw_e_pv_today", "PV energy today", 1643),
    _gw_total_field("gw_e_pv_total", "PV energy total", 1644),
    _gw_energy("gw_e_export_today", "Grid export today", 1646),
    _gw_total_field("gw_e_export_total", "Grid export total", 1647),
    _gw_energy("gw_e_aio_charge_today", "AIO charge today", 1649),
    _gw_total_field("gw_e_aio_charge_total", "AIO charge total", 1650),
    _gw_energy("gw_e_aio_discharge_today", "AIO discharge today", 1652),
    _gw_total_field("gw_e_aio_discharge_total", "AIO discharge total", 1653),
    _gw_energy("gw_e_load_today", "Load energy today", 1655),
    _gw_total_field("gw_e_load_total", "Load energy total", 1656),
    Field("gw_aio_count", "AIO count", (1700,), state_class=None),
    Field("gw_aio_online", "AIO online", (1701,), state_class=None),
    Field("gw_p_aio_total", "AIO power", (1702,), signed=True, negate=True, unit=W, device_class="power"),  # +discharge
    Field("gw_p_aio_charge", "AIO charge power", (1702,), unit=W, device_class="power", custom=_pos(1702, 1)),
    Field("gw_p_aio_discharge", "AIO discharge power", (1702,), unit=W, device_class="power", custom=_pos(1702, -1)),
    Field("gw_aio_state", "AIO state", (1703,), state_class=None, enabled=False),
    _gw_energy("gw_e_battery_charge_today", "Battery charge today", 1795),
    _gw_total_field("gw_e_battery_charge_total", "Battery charge total", 1796),
    _gw_energy("gw_e_battery_discharge_today", "Battery discharge today", 1798),
    _gw_total_field("gw_e_battery_discharge_total", "Battery discharge total", 1799),
    *[
        f
        for n in (1, 2, 3)
        for f in (
            Field(f"gw_aio{n}_soc", f"AIO {n} SOC", (1800 + n,), unit=PCT, device_class="battery", valid=(0, 100)),
            Field(f"gw_p_aio{n}", f"AIO {n} power", (1815 + n,), signed=True, negate=True, unit=W, device_class="power"),
            _gw_energy(f"gw_e_aio{n}_charge_today", f"AIO {n} charge today", 1702 + 3 * n, enabled=False),
            _gw_energy(f"gw_e_aio{n}_discharge_today", f"AIO {n} discharge today", 1747 + 3 * n, enabled=False),
        )
    ],
    Field("gw_first_inverter_serial", "First inverter serial", (1627,), state_class=None, enabled=False,
          custom=lambda r: decode_serial(r, 1627) if 1627 in r else None),
    Field("gw_aio1_serial", "AIO 1 serial", (1841,), state_class=None, enabled=False,
          custom=lambda r: decode_serial(r, 1841) if 1841 in r else None),
]

# Gateway AIO slots that are actually populated are decided at runtime from
# gw_aio_count; fields for AIO n > count are skipped.


# ------------------------------------------------------------------ controls
@dataclass(frozen=True)
class Control:
    key: str
    name: str
    platform: str  # switch | number | select | time
    reg: int
    min: float = 0
    max: float = 100
    step: float = 1
    unit: str | None = None
    options: dict | None = None  # value -> label for selects
    block: str = "HR0"  # block that must have probed OK
    families: frozenset = frozenset(CONTROL_FAMILIES)
    extended: bool | None = None  # True = only 10-slot devices, False = only legacy
    icon: str | None = None


_ACAIO = frozenset({FAMILY_AC, FAMILY_AIO, FAMILY_GATEWAY})

CONTROLS: list[Control] = [
    # switches
    Control("enable_charge", "AC charge enable", "switch", 96, block="HR60", icon="mdi:battery-charging"),
    Control("enable_discharge", "Discharge enable", "switch", 59, icon="mdi:battery-minus"),
    Control("enable_charge_target", "Charge target enable", "switch", 20, icon="mdi:target"),
    Control("eco_mode", "Eco mode", "switch", 27, icon="mdi:leaf"),
    # numbers
    Control("charge_target_soc", "Charge target SOC", "number", 116, 4, 100, unit=PCT, block="HR60"),
    Control("charge_target_soc_1", "Charge slot 1 target SOC", "number", 242, 4, 100, unit=PCT,
            block="HR240", extended=True),
    Control("battery_soc_reserve", "Battery SOC reserve", "number", 110, 4, 100, unit=PCT, block="HR60"),
    Control("discharge_min_reserve", "Battery power cutoff SOC", "number", 114, 4, 100, unit=PCT, block="HR60"),
    Control("battery_charge_limit", "Battery charge rate limit", "number", 111, 0, 50, block="HR60"),
    Control("battery_discharge_limit", "Battery discharge rate limit", "number", 112, 0, 50, block="HR60"),
    Control("charge_power_percent", "Charge power percent", "number", 313, 1, 100, unit=PCT,
            block="HR300", families=_ACAIO),
    Control("discharge_power_percent", "Discharge power percent", "number", 314, 1, 100, unit=PCT,
            block="HR300", families=_ACAIO),
    # selects
    Control("battery_pause_mode", "Battery pause mode", "select", 318, block="HR300", families=_ACAIO,
            options={0: "Disabled", 1: "Pause charge", 2: "Pause discharge", 3: "Pause both"}),
    # time slots (HHMM)
    Control("charge_slot_1_start", "Charge slot 1 start", "time", 94, block="HR60"),
    Control("charge_slot_1_end", "Charge slot 1 end", "time", 95, block="HR60"),
    Control("discharge_slot_1_start", "Discharge slot 1 start", "time", 56),
    Control("discharge_slot_1_end", "Discharge slot 1 end", "time", 57),
    Control("charge_slot_2_start", "Charge slot 2 start", "time", 31, extended=False),
    Control("charge_slot_2_end", "Charge slot 2 end", "time", 32, extended=False),
    Control("discharge_slot_2_start", "Discharge slot 2 start", "time", 44, extended=False),
    Control("discharge_slot_2_end", "Discharge slot 2 end", "time", 45, extended=False),
    Control("charge_slot_2_start", "Charge slot 2 start", "time", 243, block="HR240", extended=True),
    Control("charge_slot_2_end", "Charge slot 2 end", "time", 244, block="HR240", extended=True),
    Control("discharge_slot_2_start", "Discharge slot 2 start", "time", 273, block="HR240", extended=True),
    Control("discharge_slot_2_end", "Discharge slot 2 end", "time", 274, block="HR240", extended=True),
]

# Composite GivTCP-style mode: (eco HR27, discharge HR59)
BATTERY_MODES = {
    "Eco": (1, 0),
    "Timed Demand": (1, 1),
    "Timed Export": (0, 1),
}

WRITE_SAFE_REGISTERS = frozenset({c.reg for c in CONTROLS})


def control_applies(c: Control, family: str, blocks_ok: set[str], extended: bool) -> bool:
    if family not in c.families or c.block not in blocks_ok:
        return False
    if c.extended is True and not extended:
        return False
    if c.extended is False and extended:
        return False
    return True
