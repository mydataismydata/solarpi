"""Backfill soc_count: replay stored samples through the ChargeCounter for one bank.

Fills inverter_samples.soc_count for the samples in [--start, --end) the same way the live poller
does, and saves the bank's count at the end so the live counter carries on from it. Run it once per
stretch a bank was connected, oldest first, with the service stopped (the live counter would
otherwise overwrite the saved count). Safe to re-run.

Where the count starts:
  --soc 37.8   from this SOC at --start
  (no --soc)   from the bank's saved count if it has one, else from its first full charge

Usage (on the Pi, from the project root):
    .venv/bin/python deploy/backfill_soc_count.py --bank AA:..,AA:.. --capacity-ah 640 \\
        --start 2026-06-08 --end "2026-10-08 14:57:56" [--soc 37.8] [--db data/solar.sqlite]
"""
import argparse
import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from solardash.charge_counter import FULL_V, ChargeCounter, bank_key
from solardash.db import TimeSeriesStore


def epoch(text: str) -> int:
    """Local wall-clock time ('2026-10-08' or '2026-10-08 14:57:56') -> epoch seconds."""
    fmt = "%Y-%m-%d %H:%M:%S" if " " in text else "%Y-%m-%d"
    return int(dt.datetime.strptime(text, fmt).astimezone().timestamp())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--bank", required=True, help="the bank's BMS MACs, comma-separated")
    ap.add_argument("--capacity-ah", type=float, required=True)
    ap.add_argument("--full-v", type=float, default=FULL_V)
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", help="default: now")
    ap.add_argument("--soc", type=float, help="SOC (%%) at --start; default: saved count or first full charge")
    ap.add_argument("--db", default="data/solar.sqlite")
    a = ap.parse_args()

    store = TimeSeriesStore(a.db)
    key = bank_key(a.bank.split(","))
    counter = ChargeCounter(store, key, a.capacity_ah, full_v=a.full_v)
    if a.soc is not None:
        counter.soc, counter.ts = a.soc, None
    start, end = epoch(a.start), (epoch(a.end) if a.end else 2**31 - 1)

    with store._lock:
        rows = store._conn.execute(
            "SELECT rowid, ts, battery_voltage, battery_current FROM inverter_samples "
            "WHERE ts >= ? AND ts < ? ORDER BY ts", (start, end)
        ).fetchall()
    updates = [(counter.update(r["ts"], r["battery_voltage"], r["battery_current"]), r["rowid"]) for r in rows]
    with store._lock:
        store._conn.executemany("UPDATE inverter_samples SET soc_count = ? WHERE rowid = ?", updates)
        store._conn.commit()
    counter.save()

    filled = [u for u in updates if u[0] is not None]
    last_full = (dt.datetime.fromtimestamp(counter.last_full_ts).strftime("%Y-%m-%d %H:%M")
                 if counter.last_full_ts else "none")
    print(f"bank {key}: {len(filled)} of {len(rows)} samples counted; "
          f"ends at {counter.value()}% (last full {last_full})")


if __name__ == "__main__":
    main()
