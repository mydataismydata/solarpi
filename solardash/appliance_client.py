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
POWER_COOLDOWN_S = 300  # ignore on/off commands for 5 min after a change (protect the compressor)
APPLIANCE_MODES = ("auto", "cold", "hot", "wind", "wet")  # DP 4 enum (cold=cool, hot=heat, wet=dry)
MODE_REVERSE_COOLDOWN_S = 300  # gate cool/dry <-> heat switches for 5 min (compressor reversal)
_COOLING_MODES = ("cold", "wet")  # cool + dry run the compressor the same way; hot reverses it


def _is_compressor_reverse(from_mode, to_mode) -> bool:
    """True when a mode switch crosses the heat/cool boundary (reverses the compressor)."""
    return (from_mode in _COOLING_MODES and to_mode == "hot") or (from_mode == "hot" and to_mode in _COOLING_MODES)


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
        self.last_power_change: Optional[int] = None  # epoch of the last on/off change (cooldown anchor)
        self.last_mode_reverse: Optional[int] = None  # epoch of the last heat<->cool switch (mode gate)

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

    def power_cooldown_remaining(self) -> int:
        """Seconds left in the post-change lockout (0 = a power command is allowed now).
        Anti short-cycle: the compressor must not be flipped on/off in quick succession."""
        if self.last_power_change is None:
            return 0
        return max(0, POWER_COOLDOWN_S - (int(self.clock()) - self.last_power_change))

    async def set_power(self, on: bool) -> Dict[str, object]:
        """Turn the unit on/off, ENFORCED SERVER-SIDE: any command inside the cooldown window is
        ignored (not sent). On success, stamps the change (starting a fresh cooldown) and re-polls."""
        remaining = self.power_cooldown_remaining()
        if remaining > 0:
            return {"ok": False, "cooldown": True, "retry_after": remaining}
        ok = await self.client.set_power(on)
        if ok:
            self.last_power_change = int(self.clock())
            await self.poll_once()  # refresh the cached snapshot so the UI reflects the new state
        return {"ok": ok, "power": on if ok else None, "cooldown": False}

    def mode_reverse_cooldown_remaining(self) -> int:
        """Seconds left in the cool/dry <-> heat lockout (0 = a reversing switch is allowed now)."""
        if self.last_mode_reverse is None:
            return 0
        return max(0, MODE_REVERSE_COOLDOWN_S - (int(self.clock()) - self.last_mode_reverse))

    async def set_mode(self, mode: str) -> Dict[str, object]:
        """Set the operating mode (writes DP 4). A cool/dry <-> heat switch reverses the compressor,
        so it's gated by a 5-min cooldown (enforced here); same-side switches (cool<->dry) are free."""
        if mode not in APPLIANCE_MODES:
            return {"ok": False, "error": "unknown mode %r" % (mode,)}
        current = self.status.mode if self.status else None
        reversing = _is_compressor_reverse(current, mode)
        if reversing:
            remaining = self.mode_reverse_cooldown_remaining()
            if remaining > 0:
                return {"ok": False, "cooldown": True, "retry_after": remaining, "reason": "mode_reverse"}
        ok = await self.client.set_dp(4, mode)
        if ok:
            if reversing:
                self.last_mode_reverse = int(self.clock())
            await self.poll_once()
        return {"ok": ok, "mode": mode if ok else None}

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
