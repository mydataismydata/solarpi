"""Periodic poll loop: read the inverter, merge with last-good registers, persist.

Resilient to the Solarman dongle's occasional partial replies — a block that drops
out this cycle carries forward its previous value (SrneInverter.merge) rather than
writing a gap, and a read with no live battery voltage is discarded (has_core gate).

The Pi has no hardware clock. After a power cut it boots with the time saved before the cut and
only catches up when NTP answers (once Starlink is back up), so a sample stored in between gets a
timestamp minutes or hours early. Until the clock has synced, samples are held in memory and then
stored, stamped from the synced clock less their age.
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Callable, Dict, List, Optional, Tuple

from . import inverter
from .charge_counter import ChargeCounter
from .client import InverterClient
from .db import TimeSeriesStore

DEFAULT_INTERVAL_S = 10.0
# Don't integrate energy across gaps longer than this (treat as downtime, not real flow).
MAX_ENERGY_GAP_S = 300.0
# systemd-timesyncd keeps this directory while it runs and creates `synchronized` in it at its
# first sync after boot.
TIMESYNC_DIR = "/run/systemd/timesync"
# Stop holding samples after this long and store them as stamped. Starlink sleeps 23:00-04:00, so
# a power cut at night can leave the clock unsynced for 5 hours.
MAX_CLOCK_WAIT_S = 8 * 3600.0


def timesync_synced(run_dir: str = TIMESYNC_DIR) -> bool:
    """Whether the clock has synced since boot. True when systemd-timesyncd isn't running (a dev
    machine, or another time daemon): there's nothing to wait for."""
    return not os.path.isdir(run_dir) or os.path.exists(os.path.join(run_dir, "synchronized"))


class Poller:
    def __init__(
        self,
        client: InverterClient,
        store: TimeSeriesStore,
        interval_s: float = DEFAULT_INTERVAL_S,
        clock: Callable[[], float] = time.time,
        on_sample: Optional[Callable[[int, inverter.InverterStatus], None]] = None,
        bms_soc_getter: Optional[Callable[[], Optional[float]]] = None,
        charge_counter: Optional[ChargeCounter] = None,
        clock_synced: Optional[Callable[[], bool]] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.client = client
        self.store = store
        self.interval_s = interval_s
        self.clock = clock
        self.on_sample = on_sample
        # Returns the current BMS bank SOC (accurate, coulomb-counted) to stamp on each sample, or
        # None when the BLE bank isn't available. Lets the power-history chart plot the real SOC.
        self.bms_soc_getter = bms_soc_getter
        # Counts the bank's amp-hours from the last full charge (the BMS SOC drifts; see
        # charge_counter.py). Stamped on each sample as soc_count; seeded from the BMS SOC if new.
        self.charge_counter = charge_counter
        # Says whether the wall clock can be trusted yet (None = always). Until it can, samples wait
        # in _held with their monotonic time, BMS SOC included, and nothing is counted or stored.
        self.clock_synced = clock_synced
        self.monotonic = monotonic
        self._held: List[Tuple[float, inverter.InverterStatus, Optional[float]]] = []
        self._wait_since: Optional[float] = None
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
        """Do one read+store cycle. Returns the status (stored, or held until the clock syncs),
        or None if the read was unusable (no reply / no live core data)."""
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
        bms_soc = self._read_bms_soc()
        if self._clock_pending():
            self._held.append((self.monotonic(), status, bms_soc))
            return status
        self._store_held()
        self._store(int(self.clock()), status, bms_soc)
        return status

    @property
    def held(self) -> int:
        """Samples waiting for the clock to sync."""
        return len(self._held)

    def _clock_pending(self) -> bool:
        """True while samples must be held: the clock hasn't synced since boot, and it hasn't been
        MAX_CLOCK_WAIT_S since the first held sample. Once it has synced (or that wait runs out),
        it never holds again in this process."""
        if self.clock_synced is None:
            return False
        try:
            synced = self.clock_synced()
        except Exception:
            synced = True
        if not synced:
            if self._wait_since is None:
                self._wait_since = self.monotonic()
            if self.monotonic() - self._wait_since < MAX_CLOCK_WAIT_S:
                return True
        self.clock_synced = None
        return False

    def _store_held(self) -> None:
        """Store the held samples in order, each stamped with the synced clock less its age."""
        if not self._held:
            return
        now, now_mono = self.clock(), self.monotonic()
        held, self._held = self._held, []
        for mono, status, bms_soc in held:
            self._store(int(now - (now_mono - mono)), status, bms_soc)

    def _store(self, ts: int, status: inverter.InverterStatus, bms_soc: Optional[float]) -> None:
        self.store.insert(status, ts=ts, bms_soc=bms_soc, soc_count=self._count_charge(ts, status, bms_soc))
        self._accrue_energy(ts, status)
        self.last_ts, self.last_status = ts, status
        if self.on_sample:
            self.on_sample(ts, status)

    def _read_bms_soc(self) -> Optional[float]:
        """BMS bank SOC to record with this sample, or None if unavailable. A getter error must
        never break the poll, so swallow it."""
        if self.bms_soc_getter is None:
            return None
        try:
            return self.bms_soc_getter()
        except Exception:
            return None

    def _count_charge(self, ts: int, status: inverter.InverterStatus, bms_soc: Optional[float]) -> Optional[float]:
        """Advance the charge count with this sample. Like the BMS getter, it must never break the poll."""
        if self.charge_counter is None:
            return None
        try:
            return self.charge_counter.update(ts, status.battery_voltage, status.battery_current, seed=bms_soc)
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
