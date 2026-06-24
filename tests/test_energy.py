"""Energy accrual + roll-up tests, and the poller's power->energy integration."""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sim.inverter_sim import InverterSimulator
from solardash import api, inverter
from solardash.client import InverterClient
from solardash.db import TimeSeriesStore
from solardash.poller import Poller


class EnergyStoreTest(unittest.TestCase):
    def setUp(self):
        self.store = TimeSeriesStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_accrue_full_hours_lifetime(self):
        # 1 h at 1000 W PV / 500 W load / +200 W battery -> 1.0 / 0.5 / 0.2 kWh
        self.store.accrue(ts=3600, dt_s=3600, pv_w=1000, load_w=500, batt_w=200)
        # next hour, discharging 300 W
        self.store.accrue(ts=7200, dt_s=3600, pv_w=2000, load_w=800, batt_w=-300)
        lt = self.store.energy_lifetime()
        self.assertAlmostEqual(lt["pv_kwh"], 3.0, places=3)
        self.assertAlmostEqual(lt["load_kwh"], 1.3, places=3)
        self.assertAlmostEqual(lt["charge_kwh"], 0.2, places=3)
        self.assertAlmostEqual(lt["discharge_kwh"], 0.3, places=3)
        self.assertEqual(lt["since"], 3600)

    def test_accrue_same_hour_accumulates(self):
        self.store.accrue(ts=3600, dt_s=1800, pv_w=1000, load_w=0, batt_w=0)
        self.store.accrue(ts=4500, dt_s=1800, pv_w=1000, load_w=0, batt_w=0)  # same hour bucket
        buckets = self.store.energy_buckets("hour")
        self.assertEqual(len(buckets), 1)
        self.assertAlmostEqual(buckets[0]["pv_kwh"], 1.0, places=3)  # 0.5 + 0.5

    def test_hour_buckets_split(self):
        self.store.accrue(ts=3600, dt_s=3600, pv_w=1000, load_w=0, batt_w=0)
        self.store.accrue(ts=7200, dt_s=3600, pv_w=2000, load_w=0, batt_w=0)
        buckets = self.store.energy_buckets("hour")
        self.assertEqual(len(buckets), 2)
        self.assertEqual([b["start_ts"] for b in buckets], [3600, 7200])
        self.assertAlmostEqual(buckets[0]["pv_kwh"], 1.0, places=3)
        self.assertAlmostEqual(buckets[1]["pv_kwh"], 2.0, places=3)

    def test_zero_and_negative_dt_ignored(self):
        self.store.accrue(ts=3600, dt_s=0, pv_w=1000, load_w=0, batt_w=0)
        self.store.accrue(ts=3600, dt_s=-5, pv_w=1000, load_w=0, batt_w=0)
        self.assertEqual(self.store.energy_lifetime()["pv_kwh"], 0.0)

    def test_lifetime_tracks_power_peaks(self):
        # Peaks come from insert()ed samples (instantaneous power), and hold the all-time max
        # across samples — not the latest, and independent of the energy accumulators.
        self.store.insert(inverter.InverterStatus(pv1_voltage=100.0, pv1_current=10.0, load_power=1500), ts=10)
        self.store.insert(inverter.InverterStatus(pv1_voltage=100.0, pv1_current=20.0, load_power=900), ts=20)
        lt = self.store.energy_lifetime()
        self.assertAlmostEqual(lt["pv_peak_w"], 2000.0, places=1)    # max PV (2nd sample) wins
        self.assertAlmostEqual(lt["load_peak_w"], 1500.0, places=1)  # max load (1st sample) wins

    def test_api_payloads(self):
        self.store.accrue(ts=3600, dt_s=3600, pv_w=1500, load_w=1000, batt_w=0)
        e = api.energy_payload(self.store, period="hour")
        self.assertEqual(e["period"], "hour")
        self.assertEqual(len(e["buckets"]), 1)
        self.assertAlmostEqual(e["buckets"][0]["pv_kwh"], 1.5, places=3)
        self.assertEqual(api.energy_payload(self.store, period="bogus")["period"], "day")
        self.assertAlmostEqual(api.lifetime_payload(self.store)["load_kwh"], 1.0, places=3)


class PollerEnergyTest(unittest.TestCase):
    def test_poller_integrates_energy(self):
        async def run():
            sim = InverterSimulator()
            server = await sim.start("127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            store = TimeSeriesStore(":memory:")
            try:
                client = InverterClient("127.0.0.1", 1234567890, port=port)
                ticks = iter([1000, 1010])  # 10 s apart
                poller = Poller(client, store, clock=lambda: next(ticks))
                await poller.poll_once()  # no prior sample -> no accrual
                await poller.poll_once()  # accrues one 10 s interval
                return store.energy_lifetime()
            finally:
                store.close()
                server.close()
                await server.wait_closed()

        lt = asyncio.run(run())
        # PV ~2387 W over 10 s ~= 6.63 Wh; battery charging so charge>0, discharge=0.
        self.assertGreater(lt["pv_kwh"], 0.005)
        self.assertLess(lt["pv_kwh"], 0.008)
        self.assertGreater(lt["load_kwh"], 0.0)
        self.assertGreater(lt["charge_kwh"], 0.0)
        self.assertEqual(lt["discharge_kwh"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
