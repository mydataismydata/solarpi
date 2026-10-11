"""Tests for the /api/battery payload builder (no BLE)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash import api
from solardash.bms_client import PackSample, summarize
from solardash.jbd import PackInfo


def pack(addr, name, soc, residual, cells, temps):
    return PackSample(addr, name, PackInfo(54.0, 8.0, residual, 100.0, soc, 2, 16, temps, 0), cells)


class _FakePoller:
    def __init__(self, packs):
        self.packs = packs
        self.bank = summarize(packs)
        self.last_ts = 1700000000


class BatteryApiTest(unittest.TestCase):
    def test_unavailable(self):
        self.assertEqual(api.battery_payload(None), {"available": False})

    def test_payload_shape(self):
        packs = [
            pack("AA:1", "p1", 74, 74.0, [3.36] * 16, [25.0, 26.0]),
            pack("AA:2", "p2", 78, 78.0, [3.35] * 16, [24.5]),
        ]
        out = api.battery_payload(_FakePoller(packs))
        self.assertTrue(out["available"])
        self.assertEqual(out["ts"], 1700000000)
        # bank
        self.assertEqual(out["bank"]["packs"], 2)
        self.assertTrue(math.isclose(out["bank"]["nominal_ah"], 200.0))
        self.assertTrue(math.isclose(out["bank"]["capacity_kwh"], 10.24))
        self.assertTrue(math.isclose(out["bank"]["soc"], 76.0))  # 152/200
        # per-pack
        self.assertEqual(len(out["packs"]), 2)
        p1 = out["packs"][0]
        self.assertEqual(p1["name"], "p1")
        self.assertEqual(len(p1["cells"]), 16)
        self.assertEqual(p1["temp_max"], 26.0)
        self.assertFalse(p1["has_fault"])
        self.assertIsNone(p1["count_ah"])   # no charge count supplied

    def test_count_split_across_packs(self):
        # 16 % of a 640 Ah bank = 102.4 Ah, split evenly over the two packs
        packs = [
            pack("AA:1", "p1", 31, 31.0, [3.24] * 16, [25.0]),
            pack("AA:2", "p2", 44, 44.0, [3.24] * 16, [25.0]),
        ]
        out = api.battery_payload(_FakePoller(packs), soc_count=16.0, capacity_ah=640)
        self.assertEqual(out["bank"]["count_ah"], 102.4)
        self.assertEqual([p["count_ah"] for p in out["packs"]], [51.2, 51.2])
        self.assertEqual(out["packs"][0]["residual_ah"], 31.0)   # the pack's own figure kept


if __name__ == "__main__":
    unittest.main(verbosity=2)
