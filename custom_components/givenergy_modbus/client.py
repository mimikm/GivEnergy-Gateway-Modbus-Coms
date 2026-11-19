"""Async client for a GivEnergy dongle / Gateway on TCP 8899.

One persistent connection, one request in flight, heartbeats answered in the
background reader so the dongle never drops us. Pure asyncio, no HA imports.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable

from .protocol import (
    FC_READ_HOLDING,
    FC_READ_INPUT,
    FC_WRITE_SINGLE,
    FrameBuffer,
    Heartbeat,
    TransparentResponse,
    decode_frame,
    encode_read_request,
    encode_write_request,
)

_LOGGER = logging.getLogger(__name__)

CONNECT_TIMEOUT = 5.0
REQUEST_GAP = 0.25  # dongles choke on back-to-back frames


class GivEnergyError(Exception):
    """Base error."""


class GivEnergyConnectionError(GivEnergyError):
    """Socket-level failure."""


class GivEnergyTimeout(GivEnergyError):
    """No matching reply in time."""


class GivEnergyErrorResponse(GivEnergyError):
    """Device answered with the error bit set (register block not supported)."""


class GivEnergyClient:
    def __init__(self, host: str, port: int = 8899, timeout: float = 3.0) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.adapter_serial: str | None = None
        self.on_unsolicited: Callable[[TransparentResponse], None] | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._pending: tuple[tuple, asyncio.Future] | None = None
        self._last_tx = 0.0
        self.stats = {"tx": 0, "rx": 0, "heartbeats": 0, "timeouts": 0, "crc_errors": 0, "reconnects": 0}

    # ------------------------------------------------------------ connection
    @property
    def connected(self) -> bool:
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self) -> None:
        if self.connected:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), CONNECT_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise GivEnergyConnectionError(f"Cannot connect to {self.host}:{self.port}: {err}") from err
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"givenergy_reader_{self.host}")
        _LOGGER.debug("Connected to %s:%s", self.host, self.port)

    async def close(self) -> None:
        task, self._reader_task = self._reader_task, None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self._drop_connection()

    async def _drop_connection(self) -> None:
        writer, self._writer, self._reader = self._writer, None, None
        if writer:
            writer.close()
            try:
                await asyncio.wait_for(writer.wait_closed(), 2)
            except Exception:  # noqa: BLE001
                pass
        self._fail_pending(GivEnergyConnectionError("connection closed"))

    def _fail_pending(self, exc: Exception) -> None:
        if self._pending and not self._pending[1].done():
            self._pending[1].set_exception(exc)
        self._pending = None

    async def _read_loop(self) -> None:
        buf = FrameBuffer()
        try:
            while self._reader is not None:
                data = await self._reader.read(1024)
                if not data:
                    raise GivEnergyConnectionError("remote closed connection")
                for raw in buf.feed(data):
                    await self._handle_frame(raw)
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Reader for %s stopped: %s", self.host, err)
            writer, self._writer, self._reader = self._writer, None, None
            if writer:
                writer.close()
            self._fail_pending(GivEnergyConnectionError(str(err)))

    async def _handle_frame(self, raw: bytes) -> None:
        try:
            msg = decode_frame(raw)
        except ValueError:
            return
        if isinstance(msg, Heartbeat):
            self.stats["heartbeats"] += 1
            self.adapter_serial = msg.adapter_serial.strip("\x00 ") or self.adapter_serial
            if self._writer:
                self._writer.write(msg.reply())
                await self._writer.drain()
            return
        if not isinstance(msg, TransparentResponse):
            return
        self.stats["rx"] += 1
        if not msg.crc_ok:
            self.stats["crc_errors"] += 1
            _LOGGER.debug("CRC mismatch on frame %s", raw.hex())
            return
        self.adapter_serial = msg.adapter_serial.strip("\x00 ") or self.adapter_serial
        if self._pending and not self._pending[1].done() and self._matches(self._pending[0], msg):
            self._pending[1].set_result(msg)
            return
        # The inverter pushes data on its own after a request — keep it.
        if self.on_unsolicited and not msg.error:
            try:
                self.on_unsolicited(msg)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("unsolicited handler failed")

    @staticmethod
    def _matches(key: tuple, msg: TransparentResponse) -> bool:
        address, fc, base, count = key
        if msg.function_code != fc:
            return False
        if fc == FC_WRITE_SINGLE:
            return msg.base == base
        if msg.error:
            # Error replies don't always echo the base; accept for our device.
            return msg.device_address == address or msg.base in (0, base)
        return msg.base == base and (msg.count == count or len(msg.values) == count)

    # ------------------------------------------------------------- requests
    async def _transact(self, key: tuple, frame: bytes, timeout: float | None) -> TransparentResponse:
        async with self._lock:
            if not self.connected:
                self.stats["reconnects"] += 1
                await self.connect()
            wait = REQUEST_GAP - (time.monotonic() - self._last_tx)
            if wait > 0:
                await asyncio.sleep(wait)
            fut: asyncio.Future = asyncio.get_running_loop().create_future()
            self._pending = (key, fut)
            try:
                assert self._writer is not None
                self._writer.write(frame)
                await self._writer.drain()
                self._last_tx = time.monotonic()
                self.stats["tx"] += 1
                return await asyncio.wait_for(fut, timeout or self.timeout)
            except asyncio.TimeoutError as err:
                self.stats["timeouts"] += 1
                raise GivEnergyTimeout(f"no reply for {key}") from err
            except (OSError, ConnectionError) as err:
                await self._drop_connection()
                raise GivEnergyConnectionError(str(err)) from err
            finally:
                self._pending = None

    async def read_registers(
        self, device_address: int, input_registers: bool, base: int, count: int,
        timeout: float | None = None, retries: int = 1,
    ) -> TransparentResponse:
        fc = FC_READ_INPUT if input_registers else FC_READ_HOLDING
        frame = encode_read_request(device_address, fc, base, count)
        last: Exception | None = None
        for attempt in range(retries + 1):
            try:
                resp = await self._transact((device_address, fc, base, count), frame, timeout)
            except GivEnergyTimeout as err:
                last = err
                continue
            if resp.error:
                raise GivEnergyErrorResponse(
                    f"{'IR' if input_registers else 'HR'} {base}+{count} @0x{device_address:02X} rejected"
                )
            return resp
        raise last or GivEnergyTimeout("read failed")

    async def write_register(self, device_address: int, register: int, value: int, retries: int = 1) -> int:
        frame = encode_write_request(device_address, register, value)
        last: Exception | None = None
        for _ in range(retries + 1):
            try:
                resp = await self._transact((device_address, FC_WRITE_SINGLE, register, 1), frame, None)
            except GivEnergyTimeout as err:
                last = err
                continue
            if resp.error:
                raise GivEnergyErrorResponse(f"write HR{register}={value} rejected")
            return resp.values[0] if resp.values else value
        raise last or GivEnergyTimeout("write failed")
