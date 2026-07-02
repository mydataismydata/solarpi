"""Mini-split energy accrual: hourly solar/grid Wh buckets, day roll-up, and the
AppliancePoller's trapezoidal integration over polls.

Run from the project root:  python tests/test_appliance_energy.py   (or: pytest)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash import api
from solardash.appliance_client import AppliancePoller
from solardash.db import TimeSeriesStore

BASE = 1_700_000_000  # a fixed epoch inside one hour


class ApplianceEnergyStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = TimeSeriesStore(":memory:")

    def test_accrue_and_hour_bucket(self):
        # one hour at 1000 W solar + 500 W grid -> 1.0 / 0.5 kWh
        self.store.accrue_appliance(BASE, 3600, 1000.0, 500.0)
        b = self.store.appliance_energy_buckets("hour")
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0]["solar_kwh"], 1.0, places=3)
        self.assertAlmostEqual(b[0]["grid_kwh"], 0.5, places=3)

    def test_negatives_clamped_and_zero_dt_ignored(self):
        self.store.accrue_appliance(BASE, 3600, -50.0, 500.0)  # solar negative -> 0
        self.store.accrue_appliance(BASE, 0, 9999.0, 9999.0)   # dt 0 -> no-op
        b = self.store.appliance_energy_buckets("hour")
        self.assertAlmostEqual(b[0]["solar_kwh"], 0.0, places=3)
        self.assertAlmostEqual(b[0]["grid_kwh"], 0.5, places=3)

    def test_day_rollup_sums_hours(self):
        self.store.accrue_appliance(BASE, 3600, 1000.0, 200.0)
        self.store.accrue_appliance(BASE + 3600, 3600, 1000.0, 300.0)
        day = self.store.appliance_energy_buckets("day")
        self.assertEqual(len(day), 1)
        self.assertAlmostEqual(day[0]["solar_kwh"], 2.0, places=3)
        self.assertAlmostEqual(day[0]["grid_kwh"], 0.5, places=3)

    def test_payload_shape(self):
        self.store.accrue_appliance(BASE, 3600, 1000.0, 500.0)
        p = api.appliance_energy_payload(self.store, "hour")
        self.assertEqual(p["period"], "hour")
        self.assertEqual(len(p["buckets"]), 1)
        self.assertIn("solar_kwh", p["buckets"][0])
        self.assertIn("grid_kwh", p["buckets"][0])

    def test_empty_when_no_data(self):
        self.assertEqual(self.store.appliance_energy_buckets("hour"), [])


class _Status:
    def __init__(self, solar, grid):
        self.solar_power = solar
        self.grid_power = grid


class _CounterStatus:
    """A status that reports cumulative energy counters (Wh) but no instantaneous power, so the
    poller takes the primary counter-delta path."""

    def __init__(self, solar_energy, total_energy):
        self.solar_energy = solar_energy
        self.total_energy = total_energy


class _Reading:
    def __init__(self, status):
        self.status = status
        self.raw_dps = {}


class _FakeClient:
    def __init__(self, readings):
        self.readings = list(readings)
        self.i = 0

    async def read(self):
        r = self.readings[self.i]
        self.i += 1
        return r


class AppliancePollerEnergyTest(unittest.IsolatedAsyncioTestCase):
    async def test_integrates_across_polls(self):
        store = TimeSeriesStore(":memory:")
        now = [1000]
        client = _FakeClient([_Reading(_Status(1000.0, 400.0)), _Reading(_Status(1000.0, 400.0))])
        poller = AppliancePoller(client, store=store, clock=lambda: now[0])

        await poller.poll_once()  # baseline only, nothing accrued yet
        self.assertEqual(store.appliance_energy_buckets("hour"), [])

        now[0] += 180             # 3 min later (within the 300s gap limit)
        await poller.poll_once()  # accrues 180s at 1000 W solar / 400 W grid
        b = store.appliance_energy_buckets("hour")
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0]["solar_kwh"], 1000 * 180 / 3600 / 1000, places=4)  # 0.05
        self.assertAlmostEqual(b[0]["grid_kwh"], 400 * 180 / 3600 / 1000, places=4)    # 0.02

    async def test_long_gap_is_not_accrued(self):
        store = TimeSeriesStore(":memory:")
        now = [1000]
        client = _FakeClient([_Reading(_Status(1000.0, 0.0)), _Reading(_Status(1000.0, 0.0))])
        poller = AppliancePoller(client, store=store, clock=lambda: now[0])
        await poller.poll_once()
        now[0] += 3600            # 1 h gap > 300s -> treated as downtime, not real flow
        await poller.poll_once()
        self.assertEqual(store.appliance_energy_buckets("hour"), [])

    async def test_no_store_is_safe(self):
        client = _FakeClient([_Reading(_Status(1000.0, 0.0)), _Reading(_Status(1000.0, 0.0))])
        poller = AppliancePoller(client, store=None, clock=lambda: 0)
        await poller.poll_once()
        await poller.poll_once()  # must not raise without a store


class AppliancePollerCounterEnergyTest(unittest.IsolatedAsyncioTestCase):
    async def test_counter_deltas_accrue_regardless_of_gap(self):
        store = TimeSeriesStore(":memory:")
        now = [1000]
        # cumulative solar 1000->1100 Wh (+100), total 1500->1900 Wh (+400) => grid = 400-100 = 300
        client = _FakeClient([_Reading(_CounterStatus(1000.0, 1500.0)), _Reading(_CounterStatus(1100.0, 1900.0))])
        poller = AppliancePoller(client, store=store, clock=lambda: now[0])
        await poller.poll_once()  # baseline only
        self.assertEqual(store.appliance_energy_buckets("hour"), [])
        now[0] += 9999            # huge gap: the device counted the energy, so it must still record
        await poller.poll_once()
        b = store.appliance_energy_buckets("hour")
        self.assertEqual(len(b), 1)
        self.assertAlmostEqual(b[0]["solar_kwh"], 0.1, places=3)
        self.assertAlmostEqual(b[0]["grid_kwh"], 0.3, places=3)

    async def test_counter_reset_is_skipped(self):
        store = TimeSeriesStore(":memory:")
        now = [1000]
        # device power-cycled: counters jump backward -> negative delta -> not recorded
        client = _FakeClient([_Reading(_CounterStatus(5000.0, 8000.0)), _Reading(_CounterStatus(10.0, 20.0))])
        poller = AppliancePoller(client, store=store, clock=lambda: now[0])
        await poller.poll_once()
        now[0] += 60
        await poller.poll_once()
        self.assertEqual(store.appliance_energy_buckets("hour"), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
