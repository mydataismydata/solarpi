"""Modbus write path for the inverter AC-output control (SRNE 0xDF00 CmdPowerOnOff).

Byte-for-byte frame checks plus the InverterControl cooldown/echo logic. The write frames
are pinned the same way the read frames are in test_codec.py.

Run from the project root:  python tests/test_inverter_write.py   (or: pytest)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash.client import InverterControl, OUTPUT_REGISTER
from solardash.codec import (
    modbus_parse_write_echo,
    modbus_write_single,
    v5_decode,
    v5_encode,
    v5_encode_response,
)


class WriteFrameTest(unittest.TestCase):
    def test_write_off_bytes(self):
        # slave 1, write 0xDF00 = 0 (AC output OFF)
        self.assertEqual(modbus_write_single(1, 0xDF00, 0).hex(), "0106df000000b21e")

    def test_write_on_bytes(self):
        # slave 1, write 0xDF00 = 1 (AC output ON)
        self.assertEqual(modbus_write_single(1, 0xDF00, 1).hex(), "0106df00000173de")

    def test_v5_wrapped_write(self):
        frame = v5_encode(1234567890, 0x5A, modbus_write_single(1, 0xDF00, 0))
        self.assertEqual(
            frame.hex(),
            "a5170010455a00d20296490200000000000000000000000000000106df000000b21e3115",
        )

    def test_echo_accepts_matching(self):
        echo = modbus_write_single(1, 0xDF00, 1)  # inverter mirrors the request on success
        self.assertEqual(modbus_parse_write_echo(echo, 1, 0xDF00), 1)

    def test_echo_rejects_wrong_register(self):
        echo = modbus_write_single(1, 0xDF00, 1)
        self.assertIsNone(modbus_parse_write_echo(echo, 1, 0x1234))

    def test_echo_rejects_wrong_slave(self):
        echo = modbus_write_single(1, 0xDF00, 1)
        self.assertIsNone(modbus_parse_write_echo(echo, 0xFF, 0xDF00))

    def test_echo_rejects_bad_crc(self):
        bad = bytearray(modbus_write_single(1, 0xDF00, 1))
        bad[-1] ^= 0xFF  # corrupt the CRC
        self.assertIsNone(modbus_parse_write_echo(bytes(bad), 1, 0xDF00))

    def test_echo_rejects_modbus_exception(self):
        # 0x86 = write-single exception response; must not be read as success
        exc = bytes([0x01, 0x86, 0x02, 0x00, 0x00, 0x00, 0x00, 0x00])
        self.assertIsNone(modbus_parse_write_echo(exc, 1, 0xDF00))

    def test_v5_roundtrip_echo(self):
        echo = modbus_write_single(1, 0xDF00, 1)
        wrapped = v5_encode_response(1234567890, 0x5A, echo)
        mb = v5_decode(wrapped)
        self.assertIsNotNone(mb)
        self.assertEqual(modbus_parse_write_echo(mb, 1, 0xDF00), 1)


class _FakeClient:
    def __init__(self, ok=True):
        self.ok = ok
        self.calls = []

    async def write_register(self, reg, value):
        self.calls.append((reg, value))
        return self.ok


class InverterControlTest(unittest.IsolatedAsyncioTestCase):
    async def test_off_then_on_writes_right_values(self):
        now = [1000.0]
        client = _FakeClient(ok=True)
        ctrl = InverterControl(client, cooldown_s=5, clock=lambda: now[0])

        off = await ctrl.set_output(False)
        self.assertTrue(off["ok"])
        self.assertEqual(off["on"], False)
        self.assertEqual(client.calls, [(OUTPUT_REGISTER, 0)])

        # a second command inside the window is blocked (anti double-fire)
        blocked = await ctrl.set_output(True)
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["cooldown"])
        self.assertEqual(blocked["retry_after"], 5)
        self.assertEqual(len(client.calls), 1)  # nothing written

        now[0] += 5  # cooldown elapses
        on = await ctrl.set_output(True)
        self.assertTrue(on["ok"])
        self.assertEqual(client.calls[-1], (OUTPUT_REGISTER, 1))

    async def test_failed_write_does_not_start_cooldown(self):
        client = _FakeClient(ok=False)
        ctrl = InverterControl(client, cooldown_s=5, clock=lambda: 0)
        r = await ctrl.set_output(False)
        self.assertFalse(r["ok"])
        self.assertIsNone(r["on"])
        self.assertEqual(ctrl.cooldown_remaining(), 0)  # retry immediately allowed


if __name__ == "__main__":
    unittest.main(verbosity=2)
