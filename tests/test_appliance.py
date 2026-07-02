"""Tests for the mini-split DP decode + the /api/appliance payload builder (no tinytuya/network)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash import api, appliance


# A representative dps dict as tinytuya's status() returns it (keys are strings).
SAMPLE_DPS = {
    "1": True,
    "2": 24,
    "3": 23,
    "4": "cold",
    "19": 75,
    "20": 73,
    "22": "cooling",
    "23": "high",
    "24": 0,
    "106": 1200,
    "107": 53210,
    "108": 80,
    "109": 20,
    "110": 66000,
    "111": 300,
}


class _FakePoller:
    def __init__(self, status, raw_dps):
        self.status = status
        self.raw_dps = raw_dps
        self.last_ts = 1700000000


class ApplianceDecodeTest(unittest.TestCase):
    def test_decode_full(self):
        s = appliance.decode(SAMPLE_DPS)
        self.assertTrue(s.power)
        self.assertEqual(s.temp_set_c, 24.0)
        self.assertEqual(s.temp_current_c, 23.0)
        self.assertEqual(s.temp_set_f, 75)
        self.assertEqual(s.temp_current_f, 73)
        self.assertEqual(s.mode, "cold")
        self.assertEqual(s.work_status, "cooling")
        self.assertEqual(s.fan_speed, "high")
        self.assertEqual(s.solar_power, 1200.0)
        self.assertEqual(s.grid_power, 300.0)
        self.assertEqual(s.solar_percent, 80)
        self.assertEqual(s.grid_percent, 20)
        self.assertEqual(s.solar_energy, 53210.0)
        self.assertEqual(s.total_energy, 66000.0)
        self.assertEqual(s.fault_labels, [])
        self.assertFalse(s.has_fault)

    def test_missing_dps_stay_none(self):
        s = appliance.decode({"1": False})
        self.assertFalse(s.power)
        self.assertIsNone(s.temp_current_c)
        self.assertIsNone(s.solar_power)
        self.assertEqual(s.fault_labels, [])

    def test_temp_divisor_for_tenths_firmware(self):
        s = appliance.decode({"2": 240, "3": 235}, temp_divisor=10.0)
        self.assertEqual(s.temp_set_c, 24.0)
        self.assertEqual(s.temp_current_c, 23.5)

    def test_fault_bitmap_decodes_bits(self):
        self.assertEqual(appliance.decode({"24": 1}).fault_labels, ["sensor_fault"])
        self.assertEqual(appliance.decode({"24": 3}).fault_labels, ["sensor_fault", "temp_fault"])
        self.assertEqual(appliance.decode({"24": 8}).fault_labels, ["fault_8"])
        # string-style fault enums
        self.assertEqual(appliance.decode({"24": "normal"}).fault_labels, [])
        self.assertEqual(appliance.decode({"24": "el01"}).fault_labels, ["el01"])

    def test_signed_current_temp(self):
        self.assertEqual(appliance.decode({"3": -5}).temp_current_c, -5.0)


class ApplianceApiTest(unittest.TestCase):
    def test_unavailable(self):
        self.assertEqual(api.appliance_payload(None), {"available": False, "configured": False})
        # A poller that has never produced a status is also "unavailable".
        self.assertEqual(api.appliance_payload(_FakePoller(None, None)), {"available": False, "configured": False})
        # `configured` passes through (paired but no data yet).
        self.assertEqual(api.appliance_payload(None, configured=True), {"available": False, "configured": True})

    def test_payload_shape(self):
        status = appliance.decode(SAMPLE_DPS)
        out = api.appliance_payload(_FakePoller(status, SAMPLE_DPS))
        self.assertTrue(out["available"])
        self.assertEqual(out["ts"], 1700000000)
        self.assertEqual(out["temp_current_c"], 23.0)
        self.assertEqual(out["solar_power"], 1200.0)
        self.assertEqual(out["solar_percent"], 80)
        self.assertEqual(out["fault_labels"], [])
        # raw DPs ride along for debugging an unfamiliar firmware over curl.
        self.assertEqual(out["raw_dps"], SAMPLE_DPS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
