"""Read-only Tuya-local client for the mini-split, plus its poll loop.

Client + poller are colocated here (both are small). `tinytuya` is imported lazily inside the
client so the dashboard runs fine without it installed — appliance support simply stays disabled
until SOLAR_APPLIANCE_ENABLED=1 and the package is present.

READ-ONLY by design: we only ever call the device's status() to read datapoints. Nothing here
writes a DP, so the app cannot change the mini-split's state. The DP decode lives in appliance.py.

The unit's Wi-Fi module is known to be flaky on the LAN, so the default cadence is gentle (30 s) and
the loop mirrors BmsPoller: one bad read just increments consecutive_failures and keeps the last
good snapshot, rather than blanking the screen.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional

from . import appliance

DEFAULT_INTERVAL_S = 30.0
DEFAULT_TIMEOUT_S = 5.0


@dataclass
class ApplianceReading:
    status: appliance.ApplianceStatus
    raw_dps: Dict[str, object]


class ApplianceClient:
    """Wraps a tinytuya Device for read-only LAN polling of the mini-split (Tuya local v3.3)."""

    def __init__(
        self,
        ip: str,
        device_id: str,
        local_key: str,
        version: float = 3.3,
        temp_divisor: float = 1.0,
        timeout: float = DEFAULT_TIMEOUT_S,
    ):
        self.ip = ip
        self.device_id = device_id
        self.local_key = local_key
        self.version = version
        self.temp_divisor = temp_divisor
        self.timeout = timeout
        self._device = None  # created lazily in a worker thread
        self._lock = asyncio.Lock()  # serialize the poll read and control writes on the one socket

    def _ensure_device(self):
        if self._device is None:
            import tinytuya  # lazy: only needed when appliance support is enabled

            dev = tinytuya.Device(self.device_id, self.ip, self.local_key, version=self.version)
            dev.set_socketTimeout(self.timeout)
            self._device = dev
        return self._device

    def _read_sync(self) -> Optional[Dict[str, object]]:
        """Blocking read of the device's datapoints. Returns the dps dict, or None on failure."""
        try:
            data = self._ensure_device().status()
        except Exception:
            self._device = None  # force a clean reconnect next cycle
            return None
        # tinytuya returns {'dps': {...}} on success, or {'Error': ..., 'Err': '905'} on failure.
        if not isinstance(data, dict) or data.get("Error") or not isinstance(data.get("dps"), dict):
            self._device = None
            return None
        return data["dps"]

    def _set_sync(self, dp, value) -> bool:
        """Blocking write of a single datapoint. Returns True on success."""
        try:
            res = self._ensure_device().set_value(dp, value)
        except Exception:
            self._device = None
            return False
        if isinstance(res, dict) and res.get("Error"):
            self._device = None
            return False
        return True

    async def read(self) -> Optional[ApplianceReading]:
        # tinytuya's status() is blocking; run it off the event loop. run_in_executor (not
        # asyncio.to_thread) keeps this working on Python 3.7/3.8 too.
        loop = asyncio.get_running_loop()
        async with self._lock:
            dps = await loop.run_in_executor(None, self._read_sync)
        if not dps:
            return None
        status = appliance.decode(dps, temp_divisor=self.temp_divisor)
        return ApplianceReading(status, dps)

    async def set_dp(self, dp, value) -> bool:
        """Write one datapoint — the only place the app controls the unit (read/write path)."""
        loop = asyncio.get_running_loop()
        async with self._lock:
            return await loop.run_in_executor(None, self._set_sync, dp, value)

    async def set_power(self, on: bool) -> bool:
        return await self.set_dp(1, bool(on))  # DP 1 = on/off switch


class AppliancePoller:
    """Polls the mini-split periodically and holds the latest snapshot in memory for the API."""

    def __init__(
        self,
        client: ApplianceClient,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
    ):
        self.client = client
        self.interval_s = interval_s
        self.clock = clock
        self.last_ts: Optional[int] = None
        self.status: Optional[appliance.ApplianceStatus] = None
        self.raw_dps: Optional[Dict[str, object]] = None
        self.consecutive_failures = 0

    async def poll_once(self) -> Optional[appliance.ApplianceStatus]:
        reading = await self.client.read()
        if reading is None:
            self.consecutive_failures += 1
            return None
        self.consecutive_failures = 0
        self.status = reading.status
        self.raw_dps = reading.raw_dps
        self.last_ts = int(self.clock())
        return reading.status

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        while not (stop_event and stop_event.is_set()):
            try:
                await self.poll_once()
            except Exception:  # never let one bad cycle kill the loop
                self.consecutive_failures += 1
            try:
                if stop_event:
                    await asyncio.wait_for(stop_event.wait(), timeout=self.interval_s)
                else:
                    await asyncio.sleep(self.interval_s)
            except asyncio.TimeoutError:
                pass
