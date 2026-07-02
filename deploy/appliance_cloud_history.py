#!/usr/bin/env python3
"""Pull the mini-split's historical DAILY energy from the Tuya CLOUD.

The unit's day-by-day history isn't in its LAN status() — it lives in Tuya's cloud statistics (the
device buffers usage while offline and syncs on reconnect; the phone app's multi-month chart is that
cloud data). This reads it via the Tuya Cloud API so we can then backfill the dashboard.

Prereqs — a Tuya IoT Platform cloud project with your app account linked, and its API creds. If you
ran `python -m tinytuya wizard` to get the unit's local key, you already have them in `tinytuya.json`
(this reads it by default). Otherwise pass --region/--apikey/--apisecret.

This is READ-ONLY: it discovers the energy code names and prints the daily values (plus the raw API
responses, so we can see the exact shape). Once we confirm the codes + shape, I'll add the backfill.

    # from the repo dir, with tinytuya.json present (or pass creds):
    python deploy/appliance_cloud_history.py
    python deploy/appliance_cloud_history.py --days 45          # widen the sample window
    python deploy/appliance_cloud_history.py --code add_ele     # target a specific code

Needs tinytuya:  pip install tinytuya
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ENERGY_HINTS = ("ele", "energy", "power", "kwh", "add", "solar", "grid", "elec")


def _device_id_from_pairing(path: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            c = json.load(f)
    except (OSError, ValueError):
        return None
    return (c or {}).get("device_id")


def main() -> int:
    p = argparse.ArgumentParser(description="Read the mini-split's daily energy history from Tuya cloud.")
    p.add_argument("--region", help="Tuya API region: us | eu | cn | in (default: from tinytuya.json)")
    p.add_argument("--apikey", help="Tuya IoT project Access ID / Client ID")
    p.add_argument("--apisecret", help="Tuya IoT project Access Secret")
    p.add_argument("--deviceid", help="mini-split device id (default: from the pairing file)")
    p.add_argument("--config", default="data/appliance.json", help="dashboard pairing file for the device id")
    p.add_argument("--code", action="append", help="energy statistic code(s) to try; repeatable")
    p.add_argument("--days", type=int, default=35, help="sample window for the daily-stats probe")
    args = p.parse_args()

    try:
        import tinytuya
    except ImportError:
        print("tinytuya isn't installed here. Run:  pip install tinytuya")
        return 2

    # Build the cloud client first — with just the project creds we can list the devices, so a
    # missing device id isn't fatal. With no CLI creds, Cloud() loads everything from tinytuya.json.
    if args.apikey and args.apisecret:
        cloud = tinytuya.Cloud(apiRegion=(args.region or "us"), apiKey=args.apikey,
                               apiSecret=args.apisecret, apiDeviceID=(args.deviceid or ""))
    else:
        cloud = tinytuya.Cloud()
    if getattr(cloud, "error", None):
        print("Cloud auth failed: %r" % (cloud.error,))
        print("Set up creds with `python -m tinytuya wizard`, or pass --region/--apikey/--apisecret.")
        return 2

    device_id = args.deviceid or _device_id_from_pairing(args.config)
    if not device_id:
        devs = cloud.getdevices(False)
        if isinstance(devs, list) and devs:
            print("=== Devices in your Tuya project ===")
            for d in devs:
                print("  id=%s  name=%r  product=%r  online=%s"
                      % (d.get("id"), d.get("name"), d.get("product_name"), d.get("online")))
            if len(devs) == 1:
                device_id = devs[0].get("id")
                print("\nUsing the only device: %s" % device_id)
            else:
                print("\nRe-run with --deviceid <the mini-split's id from the list above>.")
                return 2
        else:
            print("No device id, and couldn't list devices: %r" % (devs,))
            print("Pass --deviceid, or check the cloud creds in tinytuya.json.")
            return 2

    print("=== Device (cloud) ===  id=%s" % device_id)
    info = cloud.getdevices(False)
    if isinstance(info, list):
        for dev in info:
            if dev.get("id") == device_id:
                print("  name=%r  product=%r  online=%s" % (dev.get("name"), dev.get("product_name"), dev.get("online")))

    print("\n=== Current status codes (find the energy code names here) ===")
    status = cloud.getstatus(device_id)
    codes_found = []
    if isinstance(status, dict) and isinstance(status.get("result"), list):
        for item in status["result"]:
            code = str(item.get("code", ""))
            print("  %-26s = %r" % (code, item.get("value")))
            if any(h in code.lower() for h in ENERGY_HINTS):
                codes_found.append(code)
    else:
        print("  getstatus -> %r" % (status,))

    codes = args.code or codes_found
    print("\nEnergy-looking codes to probe: %s" % (codes or "(none — pass --code)"))

    # Probe the daily-statistics endpoint over a short recent window; print RAW responses so we can
    # see the exact result shape before pulling the full 28 months / writing anything.
    today = datetime.date.today()
    start = today - datetime.timedelta(days=args.days)
    print("\n=== statistics/days probe  %s .. %s ===" % (start, today))
    for code in codes:
        resp = cloud.cloudrequest(
            "/v1.0/devices/%s/statistics/days" % device_id,
            action="GET",
            query={"code": code, "start_day": start.strftime("%Y%m%d"), "end_day": today.strftime("%Y%m%d")},
        )
        print("\n--- code=%s ---" % code)
        print(json.dumps(resp, indent=2, default=str)[:2000])

    print("\n" + "=" * 70)
    print("Send me: the status-codes list, and which code's statistics/days call returned daily")
    print("values (and its shape). With that I'll pull the full ~28 months and backfill the chart.")
    print("If every statistics call errored, paste the error — we may need a different endpoint or")
    print("to enable statistics on the device's energy DP in the Tuya project.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
