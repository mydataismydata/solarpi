#!/usr/bin/env python3
"""Pull the mini-split's current datapoints over the LAN and print everything it exposes.

Use it to see exactly what the unit reports — the live climate fields, its cumulative energy
counters (solar / total Wh), and the FULL raw Tuya datapoint dump. The raw dump is the point: it
shows whether the unit holds any per-day / per-period energy beyond the running totals (which is what
we'd need to backfill the history chart).

Connection: taken from the dashboard's saved pairing file (data/appliance.json) by default, or pass
it explicitly.

    python deploy/appliance_probe.py
    python deploy/appliance_probe.py --ip 192.168.1.50 --id 0123456789abcdef0123 --key 0123456789abcdef

Needs tinytuya on the machine that can see the unit's LAN:  pip install tinytuya
"""
from __future__ import annotations

import argparse
import json
import os
import sys

# Make `solardash` importable when run from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from solardash import appliance


def _load_saved(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    if c.get("connected") and c.get("ip") and c.get("device_id") and c.get("local_key"):
        return c
    return None


def _kwh(wh):
    return "—" if wh is None else "%.3f kWh  (%.0f Wh)" % (wh / 1000.0, wh)


def main() -> int:
    p = argparse.ArgumentParser(description="Dump the mini-split's current Tuya datapoints.")
    p.add_argument("--ip")
    p.add_argument("--id", dest="device_id")
    p.add_argument("--key", dest="local_key")
    p.add_argument("--version", type=float, default=3.3)
    p.add_argument("--config", default="data/appliance.json", help="saved pairing file to read creds from")
    args = p.parse_args()

    ip, dev, key, ver = args.ip, args.device_id, args.local_key, args.version
    if not (ip and dev and key):
        saved = _load_saved(args.config)
        if saved:
            ip = ip or saved.get("ip")
            dev = dev or saved.get("device_id")
            key = key or saved.get("local_key")
            ver = saved.get("version", ver)
    if not (ip and dev and key):
        print("No connection info. Pass --ip/--id/--key, or pair from the dashboard first "
              "(that writes %s)." % args.config)
        return 2

    try:
        import tinytuya
    except ImportError:
        print("tinytuya isn't installed here. Run:  pip install tinytuya")
        return 2

    d = tinytuya.Device(dev, ip, key, version=ver)
    d.set_socketTimeout(5)
    data = d.status()
    if not isinstance(data, dict) or data.get("Error") or not isinstance(data.get("dps"), dict):
        print("Read failed: %r" % (data,))
        print("Check the IP / device id / local key, and that you're on the same LAN as the unit.")
        return 1

    dps = data["dps"]
    st = appliance.decode(dps)
    solar, total = st.solar_energy, st.total_energy
    grid = (total - solar) if (solar is not None and total is not None) else None

    print("=== Mini-split @ %s ===" % ip)
    print("power=%s  mode=%s  work=%s  fan=%s  room=%s degC  set=%s degC"
          % (st.power, st.mode, st.work_status, st.fan_speed, st.temp_current_c, st.temp_set_c))
    print()
    print("--- Energy: cumulative counters on the unit ---")
    print("  solar_energy (DP 107): %s" % _kwh(solar))
    print("  total_energy (DP 110): %s" % _kwh(total))
    print("  grid = total - solar : %s" % _kwh(grid))
    print("  solar_power  (DP 106): %s W" % ("—" if st.solar_power is None else st.solar_power))
    print("  grid_power   (DP 111): %s W" % ("—" if st.grid_power is None else st.grid_power))
    print("  split (DP 108/109)   : %s%% solar / %s%% grid" % (st.solar_percent, st.grid_percent))
    print()
    print("--- ALL raw datapoints (scan for any 'today' / 'day' / 'month' energy field) ---")
    for k in sorted(dps, key=lambda x: int(x) if str(x).isdigit() else 10 ** 9):
        print("  DP %-4s = %r" % (k, dps[k]))
    print()
    print("If the only energy fields are the cumulative counters above, the unit keeps a running")
    print("total but no dated history — so past days can't be reconstructed, but the dashboard now")
    print("records each day going forward. If you see a per-day/period field, tell me its DP number.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
