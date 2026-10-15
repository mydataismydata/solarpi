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

    def test_charge_count_stamped_on_sample(self):
        from solardash.charge_counter import ChargeCounter

        async def run():
            store = TimeSeriesStore(":memory:")
            client = _FakeClient([{0x0100: 87, 0x0101: 532, 0x0102: 50}])  # + battery current
            counter = ChargeCounter(store, "bank", 100)
            poller = Poller(client, store, clock=lambda: 1, bms_soc_getter=lambda: 42.0,
                            charge_counter=counter)
            await poller.poll_once()
            return store.latest()

        latest = asyncio.run(run())
        self.assertEqual(latest["soc_count"], 42.0)   # a new bank's count starts from the BMS SOC
        self.assertEqual(latest["bms_soc"], 42.0)

    def test_charge_counter_error_does_not_break_poll(self):
        class Boom:
            def update(self, *a, **kw):
                raise RuntimeError("bad sample")

        async def run():
            store = TimeSeriesStore(":memory:")
            client = _FakeClient([{0x0100: 87, 0x0101: 532}])
            poller = Poller(client, store, clock=lambda: 1, charge_counter=Boom())
            result = await poller.poll_once()
            return result, store.latest()

        result, latest = asyncio.run(run())
        self.assertIsNotNone(result)
        self.assertIsNone(latest["soc_count"])

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


class ClockSyncTest(unittest.TestCase):
    """Samples taken before the clock syncs after a boot are held, then stored with the synced time."""

    RAW = {0x0100: 87, 0x0101: 532}  # has core battery voltage -> storable

    def make(self, n, synced, socs=None):
        self.t = {"wall": 1000.0, "mono": 50.0}
        socs = iter(socs or [None] * n)
        self.store = TimeSeriesStore(":memory:")
        self.addCleanup(self.store.close)
        return Poller(_FakeClient([self.RAW] * n), self.store, clock=lambda: self.t["wall"],
                      monotonic=lambda: self.t["mono"], clock_synced=synced,
                      bms_soc_getter=lambda: next(socs))

    def stored(self):
        return [(r["ts"], r["bms_soc"]) for r in self.store.series(["bms_soc"], 0, 10**10)]

    def test_held_until_synced_then_stamped_from_synced_clock(self):
        sync = {"ok": False}
        poller = self.make(3, lambda: sync["ok"], socs=[40.0, 41.0, 42.0])
        asyncio.run(poller.poll_once())                       # wall 1000 is 12 min slow
        self.t.update(wall=1010.0, mono=60.0)
        asyncio.run(poller.poll_once())
        self.assertEqual(self.store.count(), 0)
        self.assertEqual(poller.held, 2)
        sync["ok"] = True
        self.t.update(wall=1780.0, mono=70.0)                 # NTP stepped the clock forward
        asyncio.run(poller.poll_once())
        self.assertEqual(poller.held, 0)
        self.assertEqual(self.stored(), [(1760, 40.0), (1770, 41.0), (1780, 42.0)])
        self.assertEqual(poller.last_ts, 1780)

    def test_never_holds_again_once_synced(self):
        sync = {"ok": True}
        poller = self.make(2, lambda: sync["ok"])
        asyncio.run(poller.poll_once())
        sync["ok"] = False                                    # e.g. timesyncd restarted
        self.t.update(wall=1010.0, mono=60.0)
        asyncio.run(poller.poll_once())
        self.assertEqual([ts for ts, _ in self.stored()], [1000, 1010])

    def test_stops_holding_after_max_wait(self):
        from solardash.poller import MAX_CLOCK_WAIT_S

        poller = self.make(2, lambda: False)
        asyncio.run(poller.poll_once())
        self.t.update(wall=1000.0 + MAX_CLOCK_WAIT_S, mono=50.0 + MAX_CLOCK_WAIT_S)
        asyncio.run(poller.poll_once())
        self.assertEqual([ts for ts, _ in self.stored()], [1000, int(1000 + MAX_CLOCK_WAIT_S)])

    def test_check_error_counts_as_synced(self):
        def boom():
            raise OSError("no /run")

        poller = self.make(1, boom)
        asyncio.run(poller.poll_once())
        self.assertEqual(self.store.count(), 1)

    def test_timesync_synced_reads_timesyncd_flag(self):
        import tempfile

        from solardash.poller import timesync_synced

        with tempfile.TemporaryDirectory() as d:
            run_dir = os.path.join(d, "timesync")
            self.assertTrue(timesync_synced(run_dir))         # no timesyncd: nothing to wait for
            os.mkdir(run_dir)
            self.assertFalse(timesync_synced(run_dir))        # running, not synced yet
            open(os.path.join(run_dir, "synchronized"), "w").close()
            self.assertTrue(timesync_synced(run_dir))

    def test_config_switch(self):
        from unittest import mock

        from solardash.config import Config

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOLAR_WAIT_CLOCK_SYNC", None)
            self.assertTrue(Config.from_env().wait_clock_sync)
            os.environ["SOLAR_WAIT_CLOCK_SYNC"] = "0"
            self.assertFalse(Config.from_env().wait_clock_sync)


if __name__ == "__main__":
    unittest.main(verbosity=2)
