"""Offline tests: protocol framing + client + probe against a fake dongle."""
import asyncio
import importlib.util
import struct
import sys
import types
from pathlib import Path

import pytest

# Load the package modules without importing HA-dependent __init__
PKG = Path(__file__).parents[1] / "custom_components" / "givenergy_modbus"
pkg = types.ModuleType("gem")
pkg.__path__ = [str(PKG)]
sys.modules["gem"] = pkg
for name in ("protocol", "client", "registers", "device"):
    spec = importlib.util.spec_from_file_location(f"gem.{name}", PKG / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"gem.{name}"] = mod
    spec.loader.exec_module(mod)

from gem import protocol as P  # noqa: E402
from gem.client import GivEnergyClient  # noqa: E402
from gem.device import GivEnergyDevice  # noqa: E402
from gem import registers as R  # noqa: E402


def test_crc_reference_vector():
    assert P.crc16_modbus(b"123456789") == 0x4B37


def test_request_layout():
    f = P.encode_read_request(0x11, 4, 0, 60)
    assert f[:8] == bytes.fromhex("5959000100 1c0102".replace(" ", ""))
    assert len(f) == 6 + 0x1C
    assert f[18:26] == struct.pack(">Q", 8)
    assert f[26:32] == bytes.fromhex("11040000003c")
    crc = P.crc16_modbus(f[26:32])
    assert f[32:34] == bytes((crc & 0xFF, crc >> 8))


def test_response_roundtrip_and_resync():
    frame = P.encode_read_response(0x11, 4, 180, list(range(60)))
    buf = P.FrameBuffer()
    frames = buf.feed(b"\x00garbage" + frame[:20]) + buf.feed(frame[20:] + P.encode_heartbeat())
    assert len(frames) == 2
    r = P.decode_frame(frames[0])
    assert r.crc_ok and r.base == 180 and r.values[59] == 59 and r.serial_looks_valid
    assert isinstance(P.decode_frame(frames[1]), P.Heartbeat)
    bad = bytearray(frame); bad[40] ^= 0xFF
    assert not P.decode_frame(bytes(bad)).crc_ok


# ------------------------------------------------------------------ fake dongle
class FakeDongle:
    def __init__(self, ir: dict, hr: dict, batteries: dict | None = None, address=0x11,
                 serial="SA2236G365", heartbeat_first=True, push=True):
        self.ir, self.hr = ir, hr
        self.batteries = batteries or {}
        self.address = address
        self.serial = serial
        self.heartbeat_first = heartbeat_first
        self.push = push
        self.writes = []
        self.heartbeat_replies = 0

    async def handle(self, reader, writer):
        if self.heartbeat_first:
            writer.write(P.encode_heartbeat("WO1234G567"))
        buf = P.FrameBuffer()
        while True:
            data = await reader.read(1024)
            if not data:
                break
            for raw in buf.feed(data):
                if raw[7] == 1:
                    self.heartbeat_replies += 1
                    continue
                addr, fc, a, b = struct.unpack(">BBHH", raw[26:32])
                writer.write(self.reply(addr, fc, a, b))
                if self.push and fc == 4 and a == 0:
                    # Real inverters spontaneously push extra blocks
                    writer.write(P.encode_read_response(addr, 4, 120, [7] * 60, self.serial))
                await writer.drain()

    def reply(self, addr, fc, a, b):
        if fc == 6:
            self.writes.append((a, b))
            self.hr[a] = b
            return P.encode_write_response(addr, a, b, self.serial)
        if addr in self.batteries and fc == 4 and a == 60:
            regs = self.batteries[addr]
            return P.encode_read_response(addr, 4, 60, [regs.get(a + i, 0) for i in range(b)], self.serial)
        if addr != self.address:
            return b""  # silence → timeout
        bank = self.ir if fc == 4 else self.hr
        if not any(a <= k < a + b for k in bank):
            return P.encode_read_response(addr, fc, a, [], self.serial, error=True)
        return P.encode_read_response(addr, fc, a, [bank.get(a + i, 0) for i in range(b)], self.serial)


def serial_regs(start, s):
    b = s.encode()
    return {start + i: (b[2 * i] << 8) | b[2 * i + 1] for i in range(5)}


async def run_with(dongle, coro):
    server = await asyncio.start_server(dongle.handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    client = GivEnergyClient("127.0.0.1", port, timeout=0.5)
    try:
        return await coro(client)
    finally:
        await client.close()
        server.close()


def gen1_hybrid():
    hr = {0: 0x2001, 19: 449, 21: 449, 27: 1, 59: 0, 94: 30, 95: 430, 96: 1, 110: 4, 116: 100,
          **serial_regs(13, "SA2236G365")}
    ir = {0: 1, 5: 2412, 13: 4998, 18: 1500, 20: 800, 30: (-350) & 0xFFFF, 42: 950,
          52: (-1200) & 0xFFFF, 59: 63, 11: 1, 12: 5, 26: 123, 180: 10, 181: 11}
    bat = {100: 63, 82: 0, 83: 52000, 88: 0, 89: 9000, 95: (-2300) & 0xFFFF, 96: 812,
           **serial_regs(110, "BG1234G567"), **{59 + n: 3250 for n in range(1, 17)}}
    return FakeDongle(ir, hr, {0x32: bat})


def test_probe_poll_write_gen1():
    dongle = gen1_hybrid()

    async def go(client):
        dev = GivEnergyDevice(client)
        info = await dev.probe()
        assert info.model == "Hybrid Gen 1" and info.family == "hybrid"
        assert info.serial == "SA2236G365"
        assert {"HR0", "HR60", "IR0", "IR180"} <= dev.blocks_ok
        assert "HR240" not in dev.blocks_ok and not dev.extended_slots
        assert dev.battery_serials == {0x32: "BG1234G567"}
        await dev.poll()
        f = {x.key: x for x in R.INVERTER_IR}
        regs = dev.inverter.ir
        assert f["p_grid"].decode(regs) == -350
        assert f["p_grid_import"].decode(regs) == 350
        assert f["p_grid_export"].decode(regs) == 0
        assert f["p_battery_charge"].decode(regs) == 1200
        assert f["p_pv"].decode(regs) == 2300
        assert f["v_ac1"].decode(regs) == 241.2
        assert f["f_ac1"].decode(regs) == 49.98
        assert f["e_pv_total"].decode(regs) == 6554.1
        assert f["status"].decode(regs) == "Normal"
        b = {x.key: x for x in R.BATTERY_IR}
        bregs = dev.batteries[0x32].ir
        assert b["v_out"].decode(bregs) == 52.0
        assert b["i_battery"].decode(bregs) == -23.0
        assert b["e_remaining"].decode(bregs) == 4.68
        # pushed IR120 block got cached via unsolicited path
        assert dev.inverter.ir.get(120) == 7
        await dev.write(116, 80)
        assert dongle.writes == [(116, 80)] and dev.inverter.hr[116] == 80
        with pytest.raises(ValueError):
            await dev.write(30, 1)  # modbus address — never allowed
        assert dongle.heartbeat_replies == 1

    asyncio.run(run_with(dongle, go))


def test_fallback_address_0x31():
    d = gen1_hybrid()
    d.address = 0x31

    async def go(client):
        dev = GivEnergyDevice(client)
        await dev.probe()
        assert dev.address == 0x31

    asyncio.run(run_with(d, go))


def test_gateway_probe_and_decode():
    hr = {0: 0x7001, 21: 10, **serial_regs(13, "GX2531G244")}
    ir = {1608: 2405, 1616: 500, 1617: 3200, 1618: 1100, 1700: 1, 1701: 1,
          1702: (-2500) & 0xFFFF, 1801: 87, 1816: (-2500) & 0xFFFF,
          1641: 0, 1642: 12345,  # V1 order
          1650: 777, 1651: 0,    # V2 (swapped) order: 777*65536 → implausible as V1
          **serial_regs(1841, "CK2527G181")}
    d = FakeDongle(ir, hr, address=0x11, serial="GX2531G244", push=False)

    async def go(client):
        dev = GivEnergyDevice(client)
        info = await dev.probe()
        assert info.family == "gateway"
        assert {"IR1600", "IR1660", "IR1780", "IR1840"} <= dev.blocks_ok
        f = {x.key: x for x in R.GATEWAY_IR}
        regs = dev.inverter.ir
        assert f["gw_p_aio_total"].decode(regs) == 2500  # discharge positive
        assert f["gw_p_aio_discharge"].decode(regs) == 2500
        assert f["gw_p_aio_charge"].decode(regs) == 0
        assert f["gw_aio1_soc"].decode(regs) == 87
        assert f["gw_e_import_total"].decode(regs) == 1234.5
        assert f["gw_e_aio_charge_total"].decode(regs) == 77.7
        assert f["gw_aio1_serial"].decode(regs) == "CK2527G181"
        res = await dev.deep_probe()
        assert res["blocks"]["IR1600"]["status"] == "ok"
        assert res["blocks"]["HR4080"]["status"] == "rejected"

    asyncio.run(run_with(d, go))


def test_detection_table():
    mk = lambda dtc, arm=0: R.detect_device({0: dtc, 21: arm})
    assert mk(0x2001, 302).model == "Hybrid Gen 3"
    assert mk(0x2001, 912).model == "Hybrid Gen 2"
    assert mk(0x8001).family == "aio"
    assert mk(0x3001).family == "ac"
    assert mk(0x7002).family == "gateway"
    assert mk(0x4001).phases == 3
