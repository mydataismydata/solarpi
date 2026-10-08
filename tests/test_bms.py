"""Bank aggregation tests for the BMS client (no BLE — bleak is imported lazily)."""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from types import SimpleNamespace

from solardash.bms_client import PackSample, pick_char_uuids, summarize
from solardash.jbd import NOTIFY_UUID, WRITE_UUID, PackInfo


def pack(addr, v, i, soc, residual, nominal, cells, temps, prot=0):
    return PackSample(addr, None, PackInfo(v, i, residual, nominal, soc, 2, 16, temps, prot), cells)


class BankSummaryTest(unittest.TestCase):
    def test_summarize_bank(self):
        packs = [
            pack("A", 53.8, 10.0, 72, 72.0, 100.0, [3.36] * 16, [25.0, 26.0]),
            pack("B", 53.6, 9.0, 70, 70.0, 100.0, [3.35] * 15 + [3.30], [24.0]),
            None,  # one pack failed to read — must be ignored
        ]
        s = summarize(packs)
        self.assertEqual(s.packs, 2)
        self.assertTrue(math.isclose(s.voltage, 53.7))     # average (parallel)
        self.assertTrue(math.isclose(s.current, 19.0))     # sum
        self.assertTrue(math.isclose(s.nominal_ah, 200.0))
        self.assertTrue(math.isclose(s.residual_ah, 142.0))
        self.assertTrue(math.isclose(s.soc, 71.0))         # 142/200 capacity-weighted
        self.assertTrue(math.isclose(s.capacity_kwh, 10.24))  # 200Ah * 16 * 3.2V
        self.assertTrue(math.isclose(s.cell_min, 3.30))
        self.assertTrue(math.isclose(s.cell_max, 3.36))
        self.assertTrue(math.isclose(s.cell_delta, 0.06))
        self.assertTrue(math.isclose(s.temp_min, 24.0))
        self.assertTrue(math.isclose(s.temp_max, 26.0))
        self.assertEqual(s.fault_packs, [])

    def test_four_pack_capacity(self):
        packs = [pack(str(k), 53.8, 5.0, 72, 72.0, 100.0, [3.36] * 16, [25.0]) for k in range(4)]
        s = summarize(packs)
        self.assertEqual(s.packs, 4)
        self.assertTrue(math.isclose(s.capacity_kwh, 20.48))  # 400Ah * 51.2V -> auto ETA capacity
        self.assertTrue(math.isclose(s.current, 20.0))

    def test_fault_pack_flagged(self):
        packs = [
            pack("ok", 53.8, 1.0, 72, 72.0, 100.0, [3.36] * 16, [25.0], prot=0),
            pack("bad", 53.8, 1.0, 72, 72.0, 100.0, [3.36] * 16, [25.0], prot=0x0020),
        ]
        self.assertEqual(summarize(packs).fault_packs, ["bad"])

    def test_empty_bank(self):
        self.assertIsNone(summarize([None, None]))


def _uuid(short):
    return f"0000{short}-0000-1000-8000-00805f9b34fb"


def _svc(short, chars):
    return SimpleNamespace(uuid=_uuid(short),
                           characteristics=[SimpleNamespace(uuid=_uuid(c), properties=props) for c, props in chars])


class PickCharUuidsTest(unittest.TestCase):
    def test_standard_jbd_pair(self):
        svcs = [_svc("ff00", [("ff01", ["read", "notify"]), ("ff02", ["write-without-response"])])]
        self.assertEqual(pick_char_uuids(svcs), (NOTIFY_UUID, WRITE_UUID))

    def test_kong_ff05_ff06(self):
        # the BigBattery Kong Elite Max's BMS, as its services read on the Pi
        svcs = [
            _svc("fa00", [("fa01", ["read", "write-without-response"])]),
            _svc("180a", [("2a50", ["read"])]),
            _svc("ff00", [("ff05", ["read", "notify"]), ("ff06", ["read", "write-without-response"])]),
            _svc("1800", [("2a00", ["read", "notify"])]),
        ]
        self.assertEqual(pick_char_uuids(svcs), (_uuid("ff05"), _uuid("ff06")))

    def test_no_ff00_service_falls_back_to_standard(self):
        svcs = [_svc("180a", [("2a50", ["read"])])]
        self.assertEqual(pick_char_uuids(svcs), (NOTIFY_UUID, WRITE_UUID))
        self.assertEqual(pick_char_uuids(None), (NOTIFY_UUID, WRITE_UUID))


if __name__ == "__main__":
    unittest.main(verbosity=2)
