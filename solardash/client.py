"""Async TCP client for the SRNE inverter via its Solarman WiFi dongle (port 8899).

One read() opens a socket, walks SrneInverter.BLOCKS, and returns decoded + raw
registers. Async port of the Android app's InverterClient.kt, including its
slave-address fallback (1 then 0xFF) and per-block tolerance of dropped replies.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from . import inverter
from .codec import (
    modbus_parse_holding,
    modbus_parse_write_echo,
    modbus_read_request,
    modbus_write_single,
    v5_decode,
    v5_encode,
)

CONNECT_TIMEOUT_S = 5.0
READ_TIMEOUT_S = 5.0

# SRNE Modbus V2.07 "Device Control Area": writing 0xDF00 powers the inverter's AC output
# on (1) or off (0). This is the only register the dashboard ever writes.
OUTPUT_REGISTER = 0xDF00
OUTPUT_ON = 1
OUTPUT_OFF = 0
# Short anti-double-fire lockout between output commands (seconds).
OUTPUT_COOLDOWN_S = 5


@dataclass
class InverterReading:
    """Decoded status plus the raw registers (handy for verifying the decode)."""

    status: inverter.InverterStatus
    raw: Dict[int, int]


class InverterClient:
    def __init__(
        self,
        ip: str,
        serial: int,
        port: int = 8899,
        connect_timeout: float = CONNECT_TIMEOUT_S,
        read_timeout: float = READ_TIMEOUT_S,
    ):
        self.ip = ip
        self.serial = serial
        self.port = port
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._seq = 0
        # Serialize all socket use on the dongle (port 8899 tolerates only one conversation at a
        # time), so a control write never collides with the poll loop's read.
        self._lock = asyncio.Lock()

    def _next_seq(self) -> int:
        self._seq = (self._seq + 1) & 0xFF
        return self._seq

    async def read(self) -> Optional[InverterReading]:
        """Open a socket, read all register blocks, return decoded+raw or None on failure."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.port), self.connect_timeout
                )
            except (OSError, asyncio.TimeoutError):
                return None
            try:
                # SRNE Modbus address is usually 1; some firmware uses the universal 0xFF.
                for slave in (1, 0xFF):
                    raw: Dict[int, int] = {}
                    for start, count in inverter.BLOCKS:
                        regs = await self._read_block(reader, writer, start, count, slave)
                        if regs is not None:
                            for i, value in enumerate(regs):
                                raw[start + i] = value
                    if raw:
                        return InverterReading(inverter.decode(raw), raw)
                return None
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def write_register(self, reg: int, value: int) -> bool:
        """Write a single holding register (Modbus fn 0x06) and confirm the inverter's echo.
        Returns True only if the unit mirrors back the exact register+value we wrote."""
        async with self._lock:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(self.ip, self.port), self.connect_timeout
                )
            except (OSError, asyncio.TimeoutError):
                return False
            try:
                for slave in (1, 0xFF):
                    request = v5_encode(self.serial, self._next_seq(), modbus_write_single(slave, reg, value))
                    writer.write(request)
                    await writer.drain()
                    frame = await self._read_v5_frame(reader)
                    if frame is None:
                        continue
                    modbus = v5_decode(frame)
                    if modbus is None:
                        continue
                    if modbus_parse_write_echo(modbus, slave, reg) == value:
                        return True
                return False
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass

    async def _read_block(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, start: int, count: int, slave: int
    ) -> Optional[List[int]]:
        request = v5_encode(self.serial, self._next_seq(), modbus_read_request(slave, start, count))
        writer.write(request)
        await writer.drain()
        frame = await self._read_v5_frame(reader)
        if frame is None:
            return None
        modbus = v5_decode(frame)
        if modbus is None:
            return None
        return modbus_parse_holding(modbus, slave)

    async def _read_v5_frame(self, reader: asyncio.StreamReader) -> Optional[bytes]:
        """Read exactly one V5 frame: the 3-byte header gives total = 13 + payloadLen."""
        try:
            head = await asyncio.wait_for(reader.readexactly(3), self.read_timeout)
            if head[0] != 0xA5:
                return None
            payload_len = head[1] | (head[2] << 8)
            remaining = 13 + payload_len - 3
            rest = await asyncio.wait_for(reader.readexactly(remaining), self.read_timeout)
            return head + rest
        except (asyncio.IncompleteReadError, asyncio.TimeoutError, ConnectionError):
            return None


class InverterControl:
    """Turns the inverter's AC output on/off (SRNE 0xDF00), with a short anti-double-fire cooldown.
    The one place the dashboard commands the inverter — opt-in, and disabled unless the server enables
    it (SOLAR_INVERTER_CONTROL)."""

    def __init__(
        self,
        client: InverterClient,
        cooldown_s: float = OUTPUT_COOLDOWN_S,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.cooldown_s = cooldown_s
        self.clock = clock
        self.last_change: Optional[int] = None

    def cooldown_remaining(self) -> int:
        """Seconds left in the post-command lockout (0 = a command is allowed now)."""
        if self.last_change is None:
            return 0
        return max(0, int(self.cooldown_s - (int(self.clock()) - self.last_change)))

    async def set_output(self, on: bool) -> Dict[str, object]:
        """Command the AC output on/off. Echo-validated; only a confirmed write counts."""
        remaining = self.cooldown_remaining()
        if remaining > 0:
            return {"ok": False, "cooldown": True, "retry_after": remaining}
        ok = await self.client.write_register(OUTPUT_REGISTER, OUTPUT_ON if on else OUTPUT_OFF)
        if ok:
            self.last_change = int(self.clock())
        return {"ok": ok, "on": on if ok else None, "cooldown": False}
