"""Store + poll-loop tests: schema/insert/latest/series/downsample/prune, the poller
against the live simulator, and the dropped-block carry-forward (partial-read merge).

Run from the project root:  python tests/test_store_poller.py
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.inverter_sim import InverterSimulator
from solardash import inverter
from solardash.client import InverterClient, InverterReading
from solardash.db import TimeSeriesStore
from solardash.poller import Poller


def make_status(**kw):
    return inverter.InverterStatus(**kw)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.store = TimeSeriesStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_insert_and_latest(self):
        self.store.insert(make_status(battery_soc=80, battery_voltage=52.0), ts=1000)
        self.store.insert(make_status(battery_soc=82, battery_voltage=52.4), ts=1010)
        self.assertEqual(self.store.count(), 2)
        latest = self.store.latest()
        self.assertEqual(latest["ts"], 1010)
        self.assertEqual(latest["battery_soc"], 82)
        self.assertEqual(latest["fault_codes"], [])  # round-trips JSON -> list

    def test_derived_persisted(self):
        # battery_power is a derived property; it should be materialised in the row.
        self.store.insert(make_status(battery_voltage=53.0, battery_current=10.0), ts=1)
        self.assertAlmostEqual(self.store.latest()["battery_power"], 530.0)

    def test_faults_roundtrip(self):
        self.store.insert(make_status(fault_codes=[3, 9]), ts=1)
        self.assertEqual(self.store.latest()["fault_codes"], [3, 9])

    def test_bms_soc_recorded_alongside_inverter(self):
        # The BMS bank SOC lands in its own column; the inverter's own battery_soc is untouched.
        self.store.insert(make_status(battery_soc=55), ts=1, bms_soc=42.0)
        latest = self.store.latest()
        self.assertEqual(latest["bms_soc"], 42.0)
        self.assertEqual(latest["battery_soc"], 55)
        self.assertEqual([r["bms_soc"] for r in self.store.series(["bms_soc"], start=1, end=1)], [42.0])

    def test_bms_soc_defaults_null(self):
        self.store.insert(make_status(battery_soc=55), ts=1)  # no BMS supplied
        self.assertIsNone(self.store.latest()["bms_soc"])

    def test_migration_adds_missing_columns(self):
        # A pre-bms_soc DB (table created without the column) must be ALTERed on open, not rejected.
        import shutil
        import sqlite3
        import tempfile
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "old.sqlite")
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE inverter_samples (ts INTEGER NOT NULL, battery_soc INTEGER)")
            conn.execute("INSERT INTO inverter_samples (ts, battery_soc) VALUES (1, 77)")
            conn.commit()
            conn.close()
            store = TimeSeriesStore(path)
            try:
                cols = {r["name"] for r in store._conn.execute("PRAGMA table_info(inverter_samples)")}
                self.assertIn("bms_soc", cols)        # new column backfilled
                self.assertIn("battery_power", cols)  # every COLUMNS entry present after migration
                store.insert(make_status(battery_soc=80), ts=2, bms_soc=41.0)
                self.assertEqual(store.latest()["bms_soc"], 41.0)
            finally:
                store.close()
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_series_range_and_order(self):
        for i in range(5):
            self.store.insert(make_status(battery_voltage=float(i * 100)), ts=2000 + i)
        rows = self.store.series(["battery_voltage"], start=2001, end=2003)
        self.assertEqual([r["ts"] for r in rows], [2001, 2002, 2003])
        self.assertEqual([r["battery_voltage"] for r in rows], [100.0, 200.0, 300.0])

    def test_series_downsample(self):
        for i in range(100):
            self.store.insert(make_status(battery_voltage=float(i)), ts=10_000 + i)
        rows = self.store.series(["battery_voltage"], max_points=10)
        self.assertLessEqual(len(rows), 12)            # ~10 buckets
        self.assertLess(len(rows), 100)                # genuinely downsampled
        ts_vals = [r["ts"] for r in rows]
        self.assertEqual(ts_vals, sorted(ts_vals))     # ascending

    def test_series_rejects_unknown_field(self):
        self.store.insert(make_status(battery_voltage=1.0), ts=1)
        self.assertEqual(self.store.series(["definitely_not_a_column"]), [])

    def test_prune(self):
        for i in range(5):
            self.store.insert(make_status(battery_soc=i), ts=500 + i)
        removed = self.store.prune(older_than_ts=502)
        self.assertEqual(removed, 2)
        self.assertEqual(self.store.count(), 3)


class _FakeClient:
    """Returns preset readings in order (then None), for deterministic poller tests."""

    def __init__(self, raws):
        self._readings = [InverterReading(inverter.decode(r), r) for r in raws]
        self._i = 0

    async def read(self):
        if self._i >= len(self._readings):
            return None
        r = self._readings[self._i]
        self._i += 1
        return r


class PollerTest(unittest.TestCase):
    def test_poll_against_simulator(self):
        async def run():
            sim = InverterSimulator()
            server = await sim.start("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            store = TimeSeriesStore(":memory:")
            try:
                client = InverterClient("127.0.0.1", 1234567890, port=port)
                ticks = iter([1000, 1010, 1020])
                poller = Poller(client, store, clock=lambda: next(ticks))
                await poller.poll_once()
                await poller.poll_once()
                return store.count(), store.latest()
            finally:
                store.close()
                server.close()
                await server.wait_closed()

        count, latest = asyncio.run(run())
        self.assertEqual(count, 2)
        self.assertEqual(latest["battery_soc"], 87)
        self.assertAlmostEqual(latest["battery_voltage"], 53.2)
        self.assertEqual(latest["load_total"], 1180 + 990)

    def test_dropped_block_carries_forward(self):
        async def run():
            store = TimeSeriesStore(":memory:")
            # Cycle 1: full battery+PV1. Cycle 2: PV1 block dropped (only battery returns).
            full = {0x0100: 87, 0x0101: 532, 0x0107: 1480, 0x0108: 92}
            partial = {0x0100: 88, 0x0101: 530}
            client = _FakeClient([full, partial])
            ticks = iter([1, 2])
            poller = Poller(client, store, clock=lambda: next(ticks))
            await poller.poll_once()
            await poller.poll_once()
            return store.latest()

        latest = asyncio.run(run())
        # PV1 voltage/current were absent in cycle 2 but should be carried forward.
        self.assertAlmostEqual(latest["pv1_voltage"], 148.0)
        self.assertAlmostEqual(latest["pv1_current"], 9.2)
        self.assertEqual(latest["battery_soc"], 88)  # fresh value won where present

    def test_unusable_read_not_stored(self):
        async def run():
            store = TimeSeriesStore(":memory:")
            # No core battery voltage -> has_core() fails -> must not be stored.
            client = _FakeClient([{0x0100: 50}])
            poller = Poller(client, store, clock=lambda: 1)
            result = await poller.poll_once()
            return result, store.count()

        result, count = asyncio.run(run())
        self.assertIsNone(result)
        self.assertEqual(count, 0)

    def test_bms_soc_getter_stamped_on_sample(self):
        async def run():
            store = TimeSeriesStore(":memory:")
            client = _FakeClient([{0x0100: 87, 0x0101: 532}])  # has core battery voltage -> stored
            poller = Poller(client, store, clock=lambda: 1, bms_soc_getter=lambda: 42.0)
            await poller.poll_once()
            return store.latest()

        latest = asyncio.run(run())
        self.assertEqual(latest["bms_soc"], 42.0)     # BMS bank SOC recorded on the sample
        self.assertEqual(latest["battery_soc"], 87)   # inverter's own value kept alongside

    def test_bms_soc_getter_error_does_not_break_poll(self):
        async def run():
            store = TimeSeriesStore(":memory:")
            client = _FakeClient([{0x0100: 87, 0x0101: 532}])

            def boom():
                raise RuntimeError("BLE down")

            poller = Poller(client, store, clock=lambda: 1, bms_soc_getter=boom)
            result = await poller.poll_once()
            return result, store.latest()

        result, latest = asyncio.run(run())
        self.assertIsNotNone(result)          # the poll still succeeded
        self.assertIsNone(latest["bms_soc"])  # getter blew up -> NULL, no crash


if __name__ == "__main__":
    unittest.main(verbosity=2)
