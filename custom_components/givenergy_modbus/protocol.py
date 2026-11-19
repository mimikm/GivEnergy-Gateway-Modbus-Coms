"""GivEnergy LAN protocol (port 8899) — framing, CRC and PDU encode/decode.

GivEnergy dongles do NOT speak standard Modbus TCP. Every frame is wrapped:

    0-1   transaction id      0x5959
    2-3   protocol id         0x0001
    4-5   length              bytes after this field
    6     unit id             0x01
    7     main function       0x01 heartbeat / 0x02 transparent
    8-17  data adapter serial 10 bytes ASCII
    18-25 padding             uint64 BE (0x08 in requests, 0x8A in replies)
    26    device (slave) addr
    27    inner function      0x03 HR read, 0x04 IR read, 0x06 HR write (bit7 = error)
    [replies only: 10 byte inverter serial]
    ...   function data
    -2    CRC-16/Modbus over bytes 26..-2, low byte first

This module is pure Python so it can be unit-tested without Home Assistant.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import struct

HEADER_MAGIC = b"\x59\x59\x00\x01"
MAIN_HEARTBEAT = 0x01
MAIN_TRANSPARENT = 0x02

FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_SINGLE = 0x06

MAX_READ_COUNT = 60
REQUEST_ADAPTER_SERIAL = "AB1234G567"  # dongles ignore the value we send
MAX_FRAME_LENGTH = 512


def crc16_modbus(data: bytes) -> int:
    """Standard CRC-16/Modbus (poly 0xA001 reflected, init 0xFFFF)."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def _crc_bytes(data: bytes) -> bytes:
    crc = crc16_modbus(data)
    return bytes((crc & 0xFF, crc >> 8))


def _wrap(main_function: int, payload: bytes) -> bytes:
    return HEADER_MAGIC + struct.pack(">HBB", len(payload) + 2, 0x01, main_function) + payload


def _serial_bytes(serial: str) -> bytes:
    return serial.encode("latin-1")[:10].ljust(10, b"\x00")


def encode_read_request(
    device_address: int, function_code: int, base: int, count: int,
    adapter_serial: str = REQUEST_ADAPTER_SERIAL,
) -> bytes:
    if function_code not in (FC_READ_HOLDING, FC_READ_INPUT):
        raise ValueError("read function must be 3 or 4")
    if not 0 < count <= MAX_READ_COUNT:
        raise ValueError(f"register count must be 1..{MAX_READ_COUNT}")
    inner = struct.pack(">BBHH", device_address, function_code, base, count)
    payload = _serial_bytes(adapter_serial) + struct.pack(">Q", 8) + inner + _crc_bytes(inner)
    return _wrap(MAIN_TRANSPARENT, payload)


def encode_write_request(
    device_address: int, register: int, value: int,
    adapter_serial: str = REQUEST_ADAPTER_SERIAL,
) -> bytes:
    if not 0 <= value <= 0xFFFF:
        raise ValueError("value must fit in uint16")
    inner = struct.pack(">BBHH", device_address, FC_WRITE_SINGLE, register, value)
    payload = _serial_bytes(adapter_serial) + struct.pack(">Q", 8) + inner + _crc_bytes(inner)
    return _wrap(MAIN_TRANSPARENT, payload)


@dataclass
class Heartbeat:
    adapter_serial: str
    adapter_type: int
    raw: bytes

    def reply(self) -> bytes:
        """The dongle drops the link unless the heartbeat is echoed within ~5 s."""
        return self.raw


@dataclass
class TransparentResponse:
    adapter_serial: str
    device_address: int
    function_code: int
    error: bool
    inverter_serial: str
    base: int = 0
    count: int = 0
    values: list[int] = field(default_factory=list)
    crc_ok: bool = True

    @property
    def serial_looks_valid(self) -> bool:
        s = self.inverter_serial.strip("\x00 ")
        return len(s) == 10 and s.isalnum()


@dataclass
class UnknownFrame:
    main_function: int
    raw: bytes


def decode_frame(frame: bytes) -> Heartbeat | TransparentResponse | UnknownFrame:
    """Decode one complete frame (header included)."""
    if len(frame) < 8 or frame[:4] != HEADER_MAGIC:
        raise ValueError("not a GivEnergy frame")
    main_fn = frame[7]
    if main_fn == MAIN_HEARTBEAT:
        serial = frame[8:18].decode("latin-1", "replace")
        a_type = frame[18] if len(frame) > 18 else 0
        return Heartbeat(serial, a_type, bytes(frame))
    if main_fn != MAIN_TRANSPARENT or len(frame) < 28:
        return UnknownFrame(main_fn, bytes(frame))

    adapter_serial = frame[8:18].decode("latin-1", "replace")
    device_address = frame[26]
    fc_raw = frame[27]
    error = bool(fc_raw & 0x80)
    fc = fc_raw & 0x7F
    crc_ok = frame[-2:] == _crc_bytes(frame[26:-2])

    body = frame[28:-2]
    inv_serial = body[:10].decode("latin-1", "replace") if len(body) >= 10 else ""
    rest = body[10:]
    resp = TransparentResponse(adapter_serial, device_address, fc, error, inv_serial, crc_ok=crc_ok)
    if len(rest) >= 4:
        resp.base, resp.count = struct.unpack(">HH", rest[:4])
    if error:
        return resp
    if fc in (FC_READ_HOLDING, FC_READ_INPUT):
        n = min(resp.count, MAX_READ_COUNT, (len(rest) - 4) // 2)
        resp.values = list(struct.unpack(f">{n}H", rest[4 : 4 + 2 * n])) if n > 0 else []
    elif fc == FC_WRITE_SINGLE:
        # For writes base=register, count=value written
        resp.values = [resp.count]
    return resp


class FrameBuffer:
    """Accumulates TCP bytes and yields complete frames, resyncing on garbage."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self._buf.extend(data)
        frames: list[bytes] = []
        while True:
            idx = self._buf.find(HEADER_MAGIC)
            if idx < 0:
                # keep last 3 bytes in case the magic is split across reads
                del self._buf[: max(0, len(self._buf) - 3)]
                return frames
            if idx:
                del self._buf[:idx]
            if len(self._buf) < 6:
                return frames
            length = struct.unpack(">H", self._buf[4:6])[0]
            if length < 2 or length > MAX_FRAME_LENGTH:
                del self._buf[:1]  # bogus length, resync
                continue
            total = 6 + length
            if len(self._buf) < total:
                return frames
            frames.append(bytes(self._buf[:total]))
            del self._buf[:total]


# ---------------------------------------------------------------- test helpers
def encode_read_response(
    device_address: int, function_code: int, base: int, values: list[int],
    inverter_serial: str = "SA1234G567", adapter_serial: str = "WF1234G567",
    error: bool = False,
) -> bytes:
    """Build a reply the way a dongle does (used by the fake-dongle tests)."""
    fc = function_code | (0x80 if error else 0)
    inner = struct.pack(">BB", device_address, fc) + _serial_bytes(inverter_serial)
    inner += struct.pack(">HH", base, len(values) if not error else 0)
    if not error:
        inner += b"".join(struct.pack(">H", v & 0xFFFF) for v in values)
    payload = _serial_bytes(adapter_serial) + struct.pack(">Q", 0x8A) + inner + _crc_bytes(inner)
    return _wrap(MAIN_TRANSPARENT, payload)


def encode_write_response(
    device_address: int, register: int, value: int,
    inverter_serial: str = "SA1234G567", adapter_serial: str = "WF1234G567",
) -> bytes:
    inner = struct.pack(">BB", device_address, FC_WRITE_SINGLE) + _serial_bytes(inverter_serial)
    inner += struct.pack(">HH", register, value)
    payload = _serial_bytes(adapter_serial) + struct.pack(">Q", 0x8A) + inner + _crc_bytes(inner)
    return _wrap(MAIN_TRANSPARENT, payload)


def encode_heartbeat(adapter_serial: str = "WF1234G567", adapter_type: int = 1) -> bytes:
    return _wrap(MAIN_HEARTBEAT, _serial_bytes(adapter_serial) + bytes((adapter_type,)))
