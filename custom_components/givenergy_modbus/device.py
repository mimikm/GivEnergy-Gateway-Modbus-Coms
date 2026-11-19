"""Probe + poll logic for one GivEnergy endpoint (no HA imports)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .client import (
    GivEnergyClient,
    GivEnergyConnectionError,
    GivEnergyError,
    GivEnergyErrorResponse,
)
from .protocol import FC_READ_HOLDING, FC_READ_INPUT, TransparentResponse
from .registers import (
    AC_CONFIG_BLOCK,
    BATTERY_ADDRESSES,
    BATTERY_BLOCK,
    DEEP_PROBE,
    EXTENDED_SLOT_BLOCK,
    FAMILY_AC,
    FAMILY_AIO,
    FAMILY_GATEWAY,
    FAMILY_HYBRID,
    FAST_BLOCKS,
    GATEWAY_BLOCKS,
    HR,
    INVERTER_ADDRESSES,
    SLOW_BLOCKS,
    WRITE_SAFE_REGISTERS,
    Block,
    DeviceInfo,
    decode_serial,
    detect_device,
    has_extended_slots,
    is_valid_serial,
)

_LOGGER = logging.getLogger(__name__)


class ProbeFailed(GivEnergyError):
    """Nothing usable answered."""


@dataclass
class RegisterBank:
    ir: dict[int, int] = field(default_factory=dict)
    hr: dict[int, int] = field(default_factory=dict)

    def store(self, input_regs: bool, base: int, values: list[int]) -> None:
        target = self.ir if input_regs else self.hr
        for i, v in enumerate(values):
            target[base + i] = v


class GivEnergyDevice:
    def __init__(self, client: GivEnergyClient, slow_every: int = 5) -> None:
        self.client = client
        self.slow_every = max(1, slow_every)
        self.address: int = INVERTER_ADDRESSES[0]
        self.info: DeviceInfo | None = None
        self.blocks: list[Block] = []
        self.blocks_ok: set[str] = set()
        self.extended_slots = False
        self.inverter = RegisterBank()
        self.batteries: dict[int, RegisterBank] = {}
        self.battery_serials: dict[int, str] = {}
        self.block_failures: dict[str, int] = {}
        self._cycle = 0
        self._force_slow = True
        client.on_unsolicited = self._on_unsolicited

    # ------------------------------------------------------------------ util
    def _validate(self, resp: TransparentResponse) -> None:
        # Flaky dongles sometimes send an all-zero frame with a blank serial.
        if not resp.serial_looks_valid and resp.values and not any(resp.values):
            raise GivEnergyError("blank response (no serial, all zero)")

    async def _read(self, addr: int, block: Block, timeout=None, retries=1) -> list[int]:
        resp = await self.client.read_registers(addr, block.input, block.base, block.count, timeout, retries)
        self._validate(resp)
        return resp.values

    def _on_unsolicited(self, msg: TransparentResponse) -> None:
        if msg.function_code not in (FC_READ_HOLDING, FC_READ_INPUT) or not msg.values:
            return
        if not msg.serial_looks_valid:
            return
        is_ir = msg.function_code == FC_READ_INPUT
        if msg.device_address == self.address:
            self.inverter.store(is_ir, msg.base, msg.values)
        elif msg.device_address in self.batteries and is_ir:
            self.batteries[msg.device_address].store(True, msg.base, msg.values)

    # ----------------------------------------------------------------- probe
    async def probe(self) -> DeviceInfo:
        await self.client.connect()
        hr0: list[int] | None = None
        last_err: Exception | None = None
        for addr in INVERTER_ADDRESSES:
            try:
                hr0 = await self._read(addr, HR(0), timeout=3.0, retries=2)
                self.address = addr
                break
            except GivEnergyConnectionError:
                raise
            except GivEnergyError as err:
                last_err = err
                _LOGGER.debug("HR0 at 0x%02X failed: %s", addr, err)
        if hr0 is None:
            raise ProbeFailed(f"no device answered HR 0-59 ({last_err})")

        self.inverter = RegisterBank()
        self.inverter.store(False, 0, hr0)
        self.info = info = detect_device(self.inverter.hr)
        _LOGGER.info(
            "GivEnergy %s: DTC %s → %s (%s), serial %s, fw %s",
            self.client.host, info.device_type_code, info.model, info.family, info.serial, info.firmware,
        )

        candidates: list[Block] = []
        if info.family == FAMILY_GATEWAY:
            candidates += GATEWAY_BLOCKS
        candidates += FAST_BLOCKS + SLOW_BLOCKS[1:] + [EXTENDED_SLOT_BLOCK, AC_CONFIG_BLOCK]

        self.blocks = [HR(0)]
        self.blocks_ok = {"HR0"}
        for block in candidates:
            try:
                vals = await self._read(self.address, block, timeout=2.5, retries=1)
            except GivEnergyConnectionError:
                raise
            except GivEnergyError as err:
                _LOGGER.debug("Probe %s: not available (%s)", block.key, err)
                continue
            self.inverter.store(block.input, block.base, vals)
            self.blocks.append(block)
            self.blocks_ok.add(block.key)

        self.extended_slots = has_extended_slots(info, self.blocks_ok)

        self.batteries.clear()
        self.battery_serials.clear()
        if info.family in (FAMILY_HYBRID, FAMILY_AC):
            for addr in BATTERY_ADDRESSES:
                try:
                    vals = await self._read(addr, BATTERY_BLOCK, timeout=1.5, retries=1)
                except GivEnergyConnectionError:
                    raise
                except GivEnergyError:
                    break
                bank = RegisterBank()
                bank.store(True, BATTERY_BLOCK.base, vals)
                serial = decode_serial(bank.ir, 110)
                if not is_valid_serial(serial):
                    break  # packs are numbered contiguously
                self.batteries[addr] = bank
                self.battery_serials[addr] = serial

        if not any(b.input for b in self.blocks):
            raise ProbeFailed("device answered config registers but no live data blocks")
        self._force_slow = False
        self._cycle = 0
        return info

    # ------------------------------------------------------------------ poll
    async def poll(self) -> None:
        self._cycle += 1
        slow = self._force_slow or self._cycle % self.slow_every == 0
        attempted = ok = 0
        for block in self.blocks:
            if not block.input and not slow:
                continue
            attempted += 1
            try:
                vals = await self._read(self.address, block)
            except GivEnergyConnectionError:
                raise
            except GivEnergyError as err:
                self.block_failures[block.key] = self.block_failures.get(block.key, 0) + 1
                _LOGGER.debug("Poll %s failed: %s", block.key, err)
                continue
            self.inverter.store(block.input, block.base, vals)
            ok += 1
        for addr, bank in self.batteries.items():
            try:
                vals = await self._read(addr, BATTERY_BLOCK, timeout=2.0, retries=0)
                bank.store(True, BATTERY_BLOCK.base, vals)
            except GivEnergyConnectionError:
                raise
            except GivEnergyError as err:
                _LOGGER.debug("Battery 0x%02X poll failed: %s", addr, err)
        if attempted and not ok:
            raise GivEnergyError("no register block could be read this cycle")
        if slow:
            self._force_slow = False

    def request_slow_refresh(self) -> None:
        self._force_slow = True

    # ----------------------------------------------------------------- write
    async def write(self, register: int, value: int) -> None:
        if register not in WRITE_SAFE_REGISTERS:
            raise ValueError(f"HR{register} is not on the write allow-list")
        await self.client.write_register(self.address, register, int(value))
        self.inverter.hr[register] = int(value)
        # Read the block back so HA shows what the inverter actually accepted
        for block in self.blocks:
            if not block.input and block.contains(register):
                try:
                    vals = await self._read(self.address, block)
                    self.inverter.store(False, block.base, vals)
                except GivEnergyError as err:
                    _LOGGER.debug("Read-back after write failed: %s", err)
                break

    # ------------------------------------------------------------ deep probe
    async def deep_probe(self) -> dict:
        """Sweep every known register range and report what answers."""
        result: dict = {"address": f"0x{self.address:02X}", "blocks": {}, "batteries": {}}
        for block in DEEP_PROBE["inverter"]:
            result["blocks"][block.key] = await self._probe_one(self.address, block)
        for addr, block in DEEP_PROBE["batteries"]:
            r = await self._probe_one(addr, block, timeout=1.2)
            if r["status"] == "ok":
                bank = RegisterBank()
                bank.store(True, block.base, r["values"])
                r["serial"] = decode_serial(bank.ir, 110)
            result["batteries"][f"0x{addr:02X}"] = r
        return result

    async def _probe_one(self, addr: int, block: Block, timeout: float = 2.0) -> dict:
        try:
            vals = await self._read(addr, block, timeout=timeout, retries=0)
        except GivEnergyConnectionError:
            raise
        except GivEnergyErrorResponse:
            return {"status": "rejected"}
        except GivEnergyError as err:
            return {"status": "no reply", "detail": str(err)}
        return {"status": "ok", "base": block.base, "values": vals, "non_zero": sum(1 for v in vals if v)}
