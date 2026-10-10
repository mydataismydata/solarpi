"""ChargeCounter tests: amp-hour counting, the full-charge reset, gaps, seeding and persistence.

Run from the project root:  python tests/test_charge_counter.py
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash.charge_counter import CHARGE_FACTOR, ChargeCounter, bank_key
from solardash.db import TimeSeriesStore


def run(counter, t0, seconds, voltage, current, step=10):
    """Feed one sample every `step` s for `seconds` s at a steady voltage/current; return the last value."""
    out = None
    for t in range(t0, t0 + seconds + 1, step):
        out = counter.update(t, voltage, current)
    return out


class ChargeCounterTest(unittest.TestCase):
    def setUp(self):
        self.store = TimeSeriesStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_nothing_to_count_from_without_seed_or_full(self):
        c = ChargeCounter(self.store, "bank", 100)
        self.assertIsNone(c.update(0, 52.0, -10.0))
        self.assertIsNone(run(c, 10, 600, 52.0, -10.0))

    def test_seeds_from_bms_then_counts_discharge(self):
        c = ChargeCounter(self.store, "bank", 100)
        self.assertEqual(c.update(0, 52.0, -10.0, seed=50.0), 50.0)
        # 10 A out for an hour from a 100 Ah bank = 10 points
        self.assertAlmostEqual(run(c, 10, 3590, 52.0, -10.0), 40.0, places=1)

    def test_seed_ignored_once_counting(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 52.0, -10.0, seed=50.0)
        self.assertAlmostEqual(c.update(360, 52.0, -10.0, seed=90.0), 50.0, places=0)

    def test_charge_counts_with_factor(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 53.0, 10.0, seed=50.0)
        self.assertAlmostEqual(run(c, 10, 3590, 53.0, 10.0), 50 + 10 * CHARGE_FACTOR, places=1)

    def test_full_charge_resets_to_100(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 54.0, 30.0, seed=40.0)
        run(c, 10, 50, 55.4, 2.0)                       # 50 s at full: not long enough yet
        self.assertLess(c.soc, 100.0)
        self.assertEqual(run(c, 60, 60, 55.4, 2.0), 100.0)
        self.assertEqual(c.last_full_ts, 120)

    def test_passing_dip_at_55v_does_not_reset(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 54.8, 30.0, seed=60.0)
        c.update(10, 55.1, 1.0)
        c.update(20, 55.1, 1.0)
        c.update(30, 54.9, 25.0)                        # current picks back up: not full
        c.update(40, 55.1, 1.0)
        c.update(70, 55.1, 1.0)                         # only 30 s since the run restarted
        self.assertLess(c.soc, 100.0)

    def test_high_current_at_55v_is_not_full(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 55.2, 30.0, seed=60.0)
        self.assertLess(run(c, 10, 300, 55.2, 30.0), 100.0)

    def test_clamped_to_0_and_100(self):
        c = ChargeCounter(self.store, "bank", 10)
        c.update(0, 52.0, -10.0, seed=5.0)
        self.assertEqual(run(c, 10, 290, 52.0, -10.0), 0.0)
        self.assertEqual(run(c, 310, 900, 53.0, 50.0), 100.0)

    def test_gap_not_integrated(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 52.0, -10.0, seed=50.0)
        self.assertEqual(c.update(3600, 52.0, -10.0), 50.0)   # an hour with no samples: flow unknown

    def test_missing_readings_hold_the_count(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 52.0, -10.0, seed=50.0)
        self.assertEqual(c.update(10, None, None), 50.0)

    def test_count_survives_restart(self):
        c = ChargeCounter(self.store, "bank", 100)
        c.update(0, 52.0, -10.0, seed=50.0)
        run(c, 10, 590, 52.0, -10.0)
        c2 = ChargeCounter(self.store, "bank", 100)
        self.assertAlmostEqual(c2.soc, c.soc, places=1)
        # carries on counting from the saved sample, ignoring a fresh BMS seed
        self.assertLess(c2.update(c.ts + 10, 52.0, -10.0, seed=90.0), c.soc)

    def test_banks_counted_separately(self):
        a = ChargeCounter(self.store, "rack", 640)
        a.update(0, 52.0, -5.0, seed=20.0)
        k = ChargeCounter(self.store, "kong", 375)
        self.assertIsNone(k.soc)
        self.assertEqual(k.update(10, 52.0, -5.0, seed=38.0), 38.0)
        self.assertEqual(ChargeCounter(self.store, "rack", 640).soc, 20.0)

    def test_bank_key(self):
        self.assertEqual(bank_key([("aa:02", "p2"), ("AA:01", "p1")]), "AA:01,AA:02")
        self.assertEqual(bank_key(["a4:c1"]), "A4:C1")
        self.assertEqual(bank_key([]), "default")


if __name__ == "__main__":
    unittest.main()
