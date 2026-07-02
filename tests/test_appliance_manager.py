"""ApplianceManager: connection persistence and live poller start/stop for UI pairing.

Uses an injected fake client so no tinytuya/hardware is needed. Run from the project root:
    python tests/test_appliance_manager.py   (or: pytest)
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash.appliance_manager import ApplianceManager
from solardash.db import TimeSeriesStore


class _FakeStatus:
    solar_power = 100.0
    grid_power = 50.0


class _FakeReading:
    def __init__(self):
        self.status = _FakeStatus()
        self.raw_dps = {}


class _FakeClient:
    def __init__(self, ip, device_id, local_key, version=3.3, temp_divisor=1.0, ok=True):
        self.ip, self.device_id, self.local_key, self.version = ip, device_id, local_key, version
        self.ok = ok

    async def read(self):
        return _FakeReading() if self.ok else None


def _factory(ok=True):
    def make(ip, device_id, local_key, version=3.3, temp_divisor=1.0):
        return _FakeClient(ip, device_id, local_key, version=version, ok=ok)
    return make


class ApplianceManagerTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "appliance.json")
        self.store = TimeSeriesStore(":memory:")

    def _mgr(self, ok=True):
        return ApplianceManager(self.path, self.store, interval_s=999, client_factory=_factory(ok))

    async def test_connect_success_persists_and_starts(self):
        mgr = self._mgr(ok=True)
        r = await mgr.connect("192.168.1.50", "devid123", "abc0000000000000")
        self.assertTrue(r["ok"])
        self.assertTrue(mgr.configured)
        self.assertIsNotNone(mgr.poller)
        saved = json.load(open(self.path))
        self.assertEqual(saved["connected"], True)
        self.assertEqual(saved["ip"], "192.168.1.50")
        self.assertEqual(saved["device_id"], "devid123")
        await mgr.shutdown()

    async def test_connect_failure_does_not_persist(self):
        mgr = self._mgr(ok=False)  # the test read fails
        r = await mgr.connect("192.168.1.50", "devid123", "badkey")
        self.assertFalse(r["ok"])
        self.assertIn("no response", r["error"])
        self.assertFalse(mgr.configured)
        self.assertIsNone(mgr.poller)
        self.assertFalse(os.path.exists(self.path))  # nothing written on failure

    async def test_connect_missing_fields(self):
        mgr = self._mgr()
        r = await mgr.connect("", "devid", "key")
        self.assertFalse(r["ok"])
        self.assertIsNone(mgr.poller)

    async def test_unpair_clears_and_stops(self):
        mgr = self._mgr(ok=True)
        await mgr.connect("192.168.1.50", "devid123", "abc0000000000000")
        self.assertTrue(mgr.configured)
        r = await mgr.unpair()
        self.assertTrue(r["ok"])
        self.assertFalse(mgr.configured)
        self.assertIsNone(mgr.poller)
        # persisted as disconnected so it overrides env and survives a restart
        self.assertEqual(json.load(open(self.path)), {"connected": False})

    async def test_start_seeds_from_env_then_polls(self):
        mgr = self._mgr(ok=True)
        env_conn = {"connected": True, "ip": "10.0.0.9", "device_id": "d", "local_key": "k"}
        await mgr.start(env_conn)
        self.assertTrue(mgr.configured)
        self.assertIsNotNone(mgr.poller)
        self.assertEqual(json.load(open(self.path))["ip"], "10.0.0.9")  # migrated to file
        await mgr.shutdown()

    async def test_start_respects_unpaired_file_over_env(self):
        # an explicit unpair on disk must win over any env config
        with open(self.path, "w") as f:
            json.dump({"connected": False}, f)
        mgr = self._mgr(ok=True)
        env_conn = {"connected": True, "ip": "10.0.0.9", "device_id": "d", "local_key": "k"}
        await mgr.start(env_conn)
        self.assertFalse(mgr.configured)
        self.assertIsNone(mgr.poller)

    async def test_start_with_nothing_is_idle(self):
        mgr = self._mgr()
        await mgr.start(None)
        self.assertFalse(mgr.configured)
        self.assertIsNone(mgr.poller)


if __name__ == "__main__":
    unittest.main(verbosity=2)
