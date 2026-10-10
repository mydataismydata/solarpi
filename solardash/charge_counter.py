"""Bank state of charge counted in amp-hours from the last full charge.

The main rack's JBD BMSes are poor at this. Their summed SOC creeps up by 1-2 points a day and only
snaps back when the bank reaches full (it went ~25 points high over 25 days without a full charge in
Sep 2026), and near empty some packs stop counting down altogether. The inverter's battery current,
summed over time, balances to within ~1 % between full charges, so the dashboard counts that instead:
reset to 100 % whenever the bank is full (voltage at or above full_v with the charge current tapered
below full_a for a minute), then add each poll's amp-hours against the bank's capacity.

The count is kept per bank (keyed by its BMS addresses) and saved to the store, so a restart or a
swap to the backup bank and back resumes where it left off. A bank with no saved count starts from
its BMS reading.
"""
from __future__ import annotations

from typing import Optional

# Charge amp-hours read ~0.7 % low against discharge on the inverter's current sensor (fit on the
# rack's full-to-full cycles, Aug-Oct 2026: e.g. 1-26 Sep, 4,098 Ah in vs 4,137 Ah out).
CHARGE_FACTOR = 1.007
FULL_V = 55.0       # bank volts: the rack's BMSes cut charging at ~55.5 V, then it sits above 55 V
FULL_A = 5.0        # amps: charge current tapered to near zero
FULL_HOLD_S = 60    # both must hold this long, so a passing cloud at 55 V doesn't reset the count
MAX_GAP_S = 300     # don't integrate across longer gaps (Pi or inverter down: flow unknown)
SAVE_EVERY_S = 60   # persist the count at most this often (plus on every full-charge reset)


def bank_key(addresses) -> str:
    """Stable id for a bank: its BMS MACs, sorted. 'default' when no BMS is configured."""
    macs = sorted(str(a[0] if isinstance(a, (tuple, list)) else a).upper() for a in (addresses or []))
    return ",".join(macs) or "default"


class ChargeCounter:
    def __init__(self, store, key: str, capacity_ah: float, full_v: float = FULL_V,
                 full_a: float = FULL_A, charge_factor: float = CHARGE_FACTOR):
        self.store = store
        self.key = key
        self.capacity_ah = float(capacity_ah)
        self.full_v = full_v
        self.full_a = full_a
        self.charge_factor = charge_factor
        saved = store.load_charge_state(key) if store is not None else None
        self.soc: Optional[float] = saved["soc"] if saved else None
        self.ts: Optional[int] = saved["ts"] if saved else None
        self.last_full_ts: Optional[int] = saved["last_full_ts"] if saved else None
        self._i: Optional[float] = None        # previous sample's current (A), for trapezoidal accrual
        self._full_since: Optional[int] = None
        self._saved_ts: Optional[int] = self.ts

    def update(self, ts: int, voltage: Optional[float], current: Optional[float],
               seed: Optional[float] = None) -> Optional[float]:
        """Fold in one inverter sample (current: + charge / - discharge) and return the bank SOC (%),
        or None while there's nothing to count from (no saved count, no full charge and no seed)."""
        if voltage is None or current is None:
            return self.value()
        if self.soc is None and seed is not None:
            self.soc = float(seed)
        elif self.soc is not None and self.ts is not None and 0 < ts - self.ts <= MAX_GAP_S:
            prev = current if self._i is None else self._i
            ah = (prev + current) / 2 * (ts - self.ts) / 3600
            if ah > 0:
                ah *= self.charge_factor
            self.soc = min(100.0, max(0.0, self.soc + ah / self.capacity_ah * 100))

        reset = False
        if voltage >= self.full_v and abs(current) <= self.full_a:
            if self._full_since is None:
                self._full_since = ts
            if ts - self._full_since >= FULL_HOLD_S:
                reset = self.soc != 100.0
                self.soc = 100.0
                self.last_full_ts = ts
        else:
            self._full_since = None

        self.ts, self._i = ts, current
        if self.soc is not None and (reset or self._saved_ts is None or ts - self._saved_ts >= SAVE_EVERY_S):
            self.save()
        return self.value()

    def value(self) -> Optional[float]:
        return None if self.soc is None else round(self.soc, 1)

    def save(self) -> None:
        if self.store is None or self.soc is None or self.ts is None:
            return
        self.store.save_charge_state(self.key, self.ts, self.soc, self.last_full_ts)
        self._saved_ts = self.ts
