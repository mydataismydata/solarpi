"""Tests for the pure API payload builders (no web framework needed)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash import api, cli
from solardash.db import TimeSeriesStore
from solardash.faults import FaultCatalog
from solardash.inverter import InverterStatus


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.store = TimeSeriesStore(":memory:")
        self.catalog = FaultCatalog.load()

    def tearDown(self):
        self.store.close()

    def test_current_unavailable_when_empty(self):
        self.assertEqual(api.current_payload(self.store, self.catalog), {"available": False})

    def test_current_payload_with_faults(self):
        self.store.insert(
            InverterStatus(battery_soc=88, battery_voltage=53.2, fault_codes=[9]), ts=1234
        )
        out = api.current_payload(self.store, self.catalog)
        self.assertTrue(out["available"])
        self.assertEqual(out["ts"], 1234)
        self.assertEqual(out["battery_soc"], 88)
        self.assertEqual(out["faults"], [{"code": 9, "text": "PV overvoltage protection"}])
        self.assertNotIn("fault_codes", out)  # raw codes replaced by annotated faults

    def test_current_derives_per_string_pv(self):
        self.store.insert(
            InverterStatus(pv1_voltage=100.0, pv1_current=5.0, pv2_voltage=120.0, pv2_current=4.0), ts=1
        )
        out = api.current_payload(self.store, self.catalog)
        self.assertAlmostEqual(out["pv1_power"], 500.0)
        self.assertAlmostEqual(out["pv2_power"], 480.0)

    def test_battery_eta_discharging(self):
        # 4.8 kWh, 50% SOC, discharging 1000 W -> 2.4 kWh / 1000 W = 144 min to empty
        self.store.insert(InverterStatus(battery_soc=50, battery_voltage=50.0, battery_current=-20.0), ts=1)
        out = api.current_payload(self.store, self.catalog, battery_capacity_wh=4800)
        self.assertEqual(out["battery_eta_kind"], "empty")
        self.assertAlmostEqual(out["battery_eta_minutes"], 144, delta=1)

    def test_battery_eta_charging(self):
        # 80% SOC, charging 1200 W -> remaining 960 Wh / 1200 W = 48 min to full
        self.store.insert(InverterStatus(battery_soc=80, battery_voltage=50.0, battery_current=24.0), ts=1)
        out = api.current_payload(self.store, self.catalog, battery_capacity_wh=4800)
        self.assertEqual(out["battery_eta_kind"], "full")
        self.assertAlmostEqual(out["battery_eta_minutes"], 48, delta=1)

    def test_battery_eta_idle_and_no_capacity(self):
        self.store.insert(InverterStatus(battery_soc=90, battery_voltage=50.0, battery_current=0.0), ts=1)
        self.assertIsNone(api.current_payload(self.store, self.catalog, battery_capacity_wh=4800)["battery_eta_minutes"])
        # No capacity configured -> no estimate.
        self.store.insert(InverterStatus(battery_soc=50, battery_voltage=50.0, battery_current=-20.0), ts=2)
        self.assertIsNone(api.current_payload(self.store, self.catalog)["battery_eta_minutes"])

    def test_history_columnar_shape(self):
        for i in range(3):
            self.store.insert(InverterStatus(battery_voltage=float(50 + i)), ts=100 + i)
        out = api.history_payload(self.store, ["battery_voltage"], max_points=1000)
        self.assertEqual(out["fields"], ["battery_voltage"])
        self.assertEqual(out["ts"], [100, 101, 102])
        self.assertEqual(out["series"]["battery_voltage"], [50.0, 51.0, 52.0])
        self.assertEqual(out["count"], 3)

    def test_history_drops_unknown_fields(self):
        self.store.insert(InverterStatus(pv1_voltage=100.0, pv1_current=5.0), ts=1)
        out = api.history_payload(self.store, ["pv_power", "bogus_field"])
        self.assertEqual(out["fields"], ["pv_power"])  # derived column kept, bogus dropped
        self.assertEqual(out["series"]["pv_power"], [500.0])

    def test_snapshot_inputs_shape(self):
        # Empty store -> current is unavailable; the server endpoint relies on this to bail out.
        cur, today, hourly, life, batt, hist = api.snapshot_inputs(self.store, self.catalog, None)
        self.assertFalse(cur.get("available"))
        self.assertIsInstance(today, dict)
        self.assertIsInstance(hourly, list)
        self.assertIn("battery_power", hist["series"])  # power-history series for the snapshot chart

    def test_snapshot_doc_renders_self_contained_html(self):
        import time as _time
        # A reading at "now" so it lands in today's buckets and the snapshot reports it as live.
        self.store.insert(InverterStatus(battery_soc=72, battery_voltage=53.0, pv1_voltage=120.0, pv1_current=8.0), ts=int(_time.time()))
        inputs = api.snapshot_inputs(self.store, self.catalog, None, battery_capacity_wh=4800)
        self.assertTrue(inputs[0]["available"])
        doc = cli.snapshot_doc(*inputs)
        self.assertTrue(doc.startswith("<!DOCTYPE html>"))
        self.assertIn("Power history", doc)
        self.assertIn("snapshot", doc)
        self.assertNotIn("http://", doc)  # self-contained: no external network references


if __name__ == "__main__":
    unittest.main(verbosity=2)
