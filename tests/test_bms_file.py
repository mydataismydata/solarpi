"""The human-readable packs file: one-pack-per-line parsing, and the file-vs-env precedence."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash.config import _parse_bms_file, _resolve_bms


class PacksFileParseTest(unittest.TestCase):
    def test_columns_mac_position_name(self):
        addrs, pos = _parse_bms_file("AA:C2:37:0B:40:6A  6  Pack-6\n")
        self.assertEqual(addrs, [("AA:C2:37:0B:40:6A", "Pack-6")])
        self.assertEqual(pos, {"AA:C2:37:0B:40:6A": 6})

    def test_position_and_name_optional(self):
        # MAC only -> name defaults to the last 6 hex digits, no position recorded.
        addrs, pos = _parse_bms_file("aa:c2:37:0b:40:05\n")
        self.assertEqual(addrs, [("aa:c2:37:0b:40:05", "0b4005")])
        self.assertEqual(pos, {})

    def test_name_without_position(self):
        # Second column that isn't an integer is treated as the name (no position).
        addrs, pos = _parse_bms_file("AA:BB:CC:DD:EE:01  Garage\n")
        self.assertEqual(addrs, [("AA:BB:CC:DD:EE:01", "Garage")])
        self.assertEqual(pos, {})

    def test_multiword_name(self):
        addrs, pos = _parse_bms_file("AA:BB:CC:DD:EE:01  2  Rack A left\n")
        self.assertEqual(addrs, [("AA:BB:CC:DD:EE:01", "Rack A left")])
        self.assertEqual(pos, {"AA:BB:CC:DD:EE:01": 2})

    def test_comments_blanks_and_header_ignored(self):
        text = (
            "# MAC   pos  name\n"
            "\n"
            "MAC pos name\n"                       # un-commented header: first token isn't a MAC
            "AA:C2:37:06:56:72  1  Pack-1   # inline comment\n"
            "  AA:C2:37:06:57:4C  2\n"             # leading whitespace tolerated
        )
        addrs, pos = _parse_bms_file(text)
        self.assertEqual(addrs, [("AA:C2:37:06:56:72", "Pack-1"), ("AA:C2:37:06:57:4C", "06574C")])
        self.assertEqual(pos, {"AA:C2:37:06:56:72": 1, "AA:C2:37:06:57:4C": 2})

    def test_bad_mac_line_skipped(self):
        # Wrong length / non-hex first token isn't a MAC -> dropped, not polled as garbage.
        addrs, _ = _parse_bms_file("ZZ:ZZ  1  nope\nAA:BB:CC:DD:EE:0G  2  alsobad\n")
        self.assertEqual(addrs, [])

    def test_empty(self):
        self.assertEqual(_parse_bms_file(""), ([], {}))


class ResolveBmsPrecedenceTest(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ("SOLAR_BMS_ADDRESSES", "SOLAR_BMS_POSITIONS")}
        os.environ.pop("SOLAR_BMS_ADDRESSES", None)
        os.environ.pop("SOLAR_BMS_POSITIONS", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_file_wins_when_present(self):
        os.environ["SOLAR_BMS_ADDRESSES"] = "FF:FF:FF:FF:FF:FF"  # should be ignored
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("AA:C2:37:0B:40:6A  6  Pack-6\n")
            path = fh.name
        try:
            addrs, pos = _resolve_bms(path)
            self.assertEqual(addrs, [("AA:C2:37:0B:40:6A", "Pack-6")])
            self.assertEqual(pos, {"AA:C2:37:0B:40:6A": 6})
        finally:
            os.unlink(path)

    def test_falls_back_to_env_when_file_missing(self):
        os.environ["SOLAR_BMS_ADDRESSES"] = "AA:BB:CC:DD:EE:01=p1"
        addrs, _ = _resolve_bms("/no/such/packs.conf")
        self.assertEqual(addrs, [("AA:BB:CC:DD:EE:01", "p1")])

    def test_falls_back_to_env_when_file_has_no_packs(self):
        # A present-but-comment-only file must not shadow a real env config.
        os.environ["SOLAR_BMS_ADDRESSES"] = "AA:BB:CC:DD:EE:01=p1"
        with tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False) as fh:
            fh.write("# only comments here\n")
            path = fh.name
        try:
            addrs, _ = _resolve_bms(path)
            self.assertEqual(addrs, [("AA:BB:CC:DD:EE:01", "p1")])
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
