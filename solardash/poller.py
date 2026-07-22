"""Periodic poll loop: read the inverter, merge with last-good registers, persist.

Resilient to the Solarman dongle's occasional partial replies — a block that drops
out this cycle carries forward its previous value (SrneInverter.merge) rather than
writing a gap, and a read with no live battery voltage is discarded (has_core gate).
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Dict, Optional

from . import inverter
from .client import InverterClient
from .db import TimeSeriesStore

DEFAULT_INTERVAL_S = 10.0
# Don't integrate energy across gaps longer than this (treat as downtime, not real flow).
MAX_ENERGY_GAP_S = 300.0


class Poller:
    def __init__(
        self,
        client: InverterClient,
        store: TimeSeriesStore,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        on_sample: Optional[Callable[[int, inverter.InverterStatus], None]] = None,
        bms_soc_getter: Optional[Callable[[], Optional[float]]] = None,
    ):
        self.client = client
        self.store = store
        self.interval_s = interval_s
        self.clock = clock
        self.on_sample = on_sample
        # Returns the current BMS bank SOC (accurate, coulomb-counted) to stamp on each sample, or
        # None when the BLE bank isn't available. Lets the battery-history chart show the real SOC.
        self.bms_soc_getter = bms_soc_getter
        self._last_raw: Optional[Dict[int, int]] = None
        self.last_ts: Optional[int] = None
        self.last_status: Optional[inverter.InverterStatus] = None
        self.consecutive_failures = 0
        # energy integration state (previous sample's time + powers, for trapezoidal accrual)
        self._e_ts: Optional[int] = None
        self._e_pv = 0.0
        self._e_load = 0.0
        self._e_batt = 0.0

    async def poll_once(self) -> Optional[inverter.InverterStatus]:
        """Do one read+store cycle. Returns the stored status, or None if the read
        was unusable (no reply / no live core data)."""
        reading = await self.client.read()
        if reading is None:
            self.consecutive_failures += 1
            return None

        merged = inverter.merge(self._last_raw, reading.raw)
        if not inverter.has_core(merged):
            self.consecutive_failures += 1
            return None

        self._last_raw = merged
        self.consecutive_failures = 0
        status = inverter.decode(merged)
        ts = int(self.clock())
        self.store.insert(status, ts=ts, bms_soc=self._read_bms_soc())
        self._accrue_energy(ts, status)
        self.last_ts, self.last_status = ts, status
        if self.on_sample:
            self.on_sample(ts, status)
        return status

    def _read_bms_soc(self) -> Optional[float]:
        """BMS bank SOC to record with this sample, or None if unavailable. A getter error must
        never break the poll, so swallow it."""
        if self.bms_soc_getter is None:
            return None
        try:
            return self.bms_soc_getter()
        except Exception:
            return None

    def _accrue_energy(self, ts: int, status: inverter.InverterStatus) -> None:
        pv = status.pv_power or 0.0
        load = float(status.load_total or 0)
        batt = status.battery_power or 0.0
        if self._e_ts is not None:
            dt = ts - self._e_ts
            if 0 < dt <= MAX_ENERGY_GAP_S:
                # trapezoidal: average of previous and current power over the interval
                self.store.accrue(
                    ts, dt, (self._e_pv + pv) / 2, (self._e_load + load) / 2, (self._e_batt + batt) / 2
                )
        self._e_ts, self._e_pv, self._e_load, self._e_batt = ts, pv, load, batt

    async def run(self, stop_event: Optional[asyncio.Event] = None) -> None:
        """Poll forever (or until stop_event is set), sleeping interval_s between cycles."""
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
