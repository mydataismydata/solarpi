#!/usr/bin/env python3
"""`solar` — a text status view of the dashboard for the terminal (e.g. the Raspberry Pi
Connect remote shell). Reads the local JSON API; stdlib-only, no dependencies.

    solar           one-shot status (AC input hidden)
    solar in        also show the AC input line
    solar usage     today's + yesterday's PV / Load / Battery energy totals (the dashboard's Today strip)
    solar export-hourly   write today's hourly energy to a CSV on the Pi (to pull remotely)
    solar watch     refresh every few seconds, with lifetime totals (Ctrl+C to quit)
    solar watch in  watch + AC input

Override the target with SOLAR_DASH_URL (default http://127.0.0.1:8000).
Override the CSV export folder with SOLAR_EXPORT_DIR (default ~/solardash/exports).
"""
import csv
import json
import os
import sys
import time
import urllib.request

BASE = os.environ.get("SOLAR_DASH_URL", "http://127.0.0.1:8000").rstrip("/")
EXPORT_DIR = os.path.expanduser(os.environ.get("SOLAR_EXPORT_DIR", "~/solardash/exports"))
USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def c(code, s):
    return f"\033[{code}m{s}\033[0m" if USE_COLOR else s


GREEN = lambda s: c("32", s)
YEL = lambda s: c("33", s)
MAG = lambda s: c("35", s)
CYAN = lambda s: c("36", s)
RED = lambda s: c("31", s)
DIM = lambda s: c("2", s)
BOLD = lambda s: c("1", s)
LBL = lambda t: t.ljust(7)


def clear():
    """Clear screen + home cursor, but only on an interactive terminal (never when piped)."""
    if sys.stdout.isatty():
        sys.stdout.write("\033[2J\033[H")


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=5) as r:
        return json.load(r)


def bar(frac, width=10):
    frac = 0.0 if frac is None else max(0.0, min(1.0, frac))
    n = int(round(frac * width))
    return "█" * n + "░" * (width - n)


def fmt(v, d=0):
    return "—" if v is None else f"{v:,.{d}f}"


def hm(mins):
    mins = max(0, int(round(mins)))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


VAL_W = 8  # fixed width for the value+unit column so every bar starts at the same column


def statline(label, value, bar_str, trailing=""):
    """label | right-aligned value | bar | trailing — bars align across all rows."""
    return f"  {LBL(label)}{value.rjust(VAL_W)}  {bar_str}  {trailing}".rstrip()


def day_buckets():
    """Recent daily energy buckets keyed by local date (YYYY-MM-DD), from the daily roll-up
    (GET /api/energy?period=day) — the same source as the dashboard's Today strip."""
    now = time.localtime()
    today_mid = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
    ej = get(f"/api/energy?period=day&start={today_mid - 86400}")  # window covers yesterday + today
    return {b.get("bucket"): b for b in ej.get("buckets", [])}


def render_usage():
    """`solar usage` — today's and yesterday's PV in / Load out / Battery charged / discharged (kWh)."""
    try:
        days = day_buckets()
    except Exception:
        return RED("offline ●") + DIM(f"  dashboard unreachable ({BASE})")
    now = time.localtime()
    today_mid = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))
    yest = time.localtime(today_mid - 12 * 3600)  # midday yesterday — robust across DST shifts
    today = days.get(time.strftime("%Y-%m-%d", now), {})
    yesterday = days.get(time.strftime("%Y-%m-%d", yest), {})

    def vals(d):
        return [d.get("pv_kwh"), d.get("load_kwh"), d.get("charge_kwh"), d.get("discharge_kwh")]

    peak = max([v or 0 for v in vals(today) + vals(yesterday)]) or 1  # shared scale so the days compare

    def block(title, when, d):
        pv, load, chg, dis = vals(d)
        return [
            f"{BOLD(title)}    {DIM(when)}",
            statline("Solar", f"{fmt(pv, 1)} kWh", YEL(bar((pv or 0) / peak)), DIM("solar generated")),
            statline("Load", f"{fmt(load, 1)} kWh", MAG(bar((load or 0) / peak)), DIM("consumed")),
            statline("Batt +", f"{fmt(chg, 1)} kWh", GREEN(bar((chg or 0) / peak)), DIM("charged")),
            statline("Batt -", f"{fmt(dis, 1)} kWh", YEL(bar((dis or 0) / peak)), DIM("discharged")),
        ]

    L = block("TODAY", time.strftime("%a %d %b", now), today)
    L.append("")
    L += block("YESTERDAY", time.strftime("%a %d %b", yest), yesterday)
    return "\n".join(L)


def export_hourly():
    """`solar export-hourly` — write today's per-hour energy buckets to a CSV on the Pi
    (columns mirror the dashboard's CSV). Returns a status line: the path written, or an error."""
    now = time.localtime()
    today_mid = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
    try:
        ej = get(f"/api/energy?period=hour&start={today_mid}")  # hourly buckets from today's midnight on
    except Exception:
        return RED("offline ●") + DIM(f"  dashboard unreachable ({BASE})")
    buckets = ej.get("buckets", [])
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        path = os.path.join(EXPORT_DIR, f"solar-hourly-{time.strftime('%Y-%m-%d', now)}.csv")
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(("hour", "solar_kwh", "load_kwh", "battery_charged_kwh", "battery_discharged_kwh"))
            for b in buckets:
                w.writerow([b.get("bucket"), b.get("pv_kwh"), b.get("load_kwh"),
                            b.get("charge_kwh"), b.get("discharge_kwh")])
    except OSError as e:
        return RED("✗ export failed  ") + DIM(str(e))
    return (GREEN("✔ exported  ") + f"{len(buckets)} hourly rows\n"
            + f"  {BOLD(path)}\n"
            + DIM("  pull it via Pi Connect file transfer, or copy from this shell with:\n")
            + DIM(f"    cat {path}"))


def render(show_in=False, watch=False):
    try:
        cur = get("/api/current")
    except Exception:
        return RED("offline ●") + DIM(f"  dashboard unreachable ({BASE})")
    if not cur.get("available"):
        return f"{BOLD('SOLAR PI')}    " + YEL("waiting ●") + DIM("  no data yet")

    try:
        life = get("/api/energy/lifetime")
    except Exception:
        life = {}
    try:
        now = int(time.time())
        ej = get(f"/api/energy?period=day&start={now - 2 * 86400}")
        today = ej["buckets"][-1] if ej.get("buckets") else {}
    except Exception:
        today = {}

    age = int(time.time()) - cur["ts"]
    when = time.strftime("%I:%M %p %Z", time.localtime(cur["ts"]))
    if age <= 120:
        status = GREEN("live ●")
    elif age <= 600:
        status = YEL("stale ●")
    else:
        status = RED("old ●")

    pv = cur.get("pv_power") or 0
    load = cur.get("load_total") or 0
    soc = cur.get("battery_soc")
    bw = cur.get("battery_power") or 0
    charging = (cur.get("battery_current") or 0) >= 0
    tone = GREEN if charging else YEL

    L = [f"{BOLD('SOLAR PI')}    {status}    {DIM(when)}"]

    # --- top stats: PV / Load / [AC in] / Batt / ETA ---
    L.append(statline("PV", f"{fmt(pv)} W", YEL(bar(pv / 4000)),
                      DIM(f"PV1 {fmt(cur.get('pv1_power'))} · PV2 {fmt(cur.get('pv2_power'))}")))
    L.append(statline("Load", f"{fmt(load)} W", MAG(bar(load / 4000)),
                      DIM(f"L1 {fmt(cur.get('load_power'))} · L2 {fmt(cur.get('load_l2_power'))}")))
    if show_in:
        gv = cur.get("grid_voltage") or 0
        if gv > 50:
            L.append(statline("AC in", f"{fmt(gv, 1)} V", CYAN(bar(gv / 260)),
                              DIM(f"{fmt(cur.get('grid_frequency'), 2)} Hz")))
        else:
            L.append(f"  {LBL('AC in')}{DIM('off-grid · no AC input')}")
    sign = "+" if bw > 0 else ""
    L.append(statline("Batt", f"{fmt(soc)} %", tone(bar((soc or 0) / 100)),
                      tone(f"{sign}{fmt(bw)} W {'charging' if charging else 'discharging'}")
                      + DIM(f"  {fmt(cur.get('battery_voltage'), 1)} V")))

    eta_min, kind = cur.get("battery_eta_minutes"), cur.get("battery_eta_kind")
    if eta_min is None:
        eta = DIM("— holding / idle")
    elif kind == "full":
        eta = GREEN(f"▲ {hm(eta_min)} to full")
    else:
        eta = YEL(f"▼ {hm(eta_min)} to empty")
    L.append(f"  {LBL('ETA')}{eta}")

    # --- break, then text-only: Temps / Usage / Status ---
    L.append("")
    L.append(f"  {LBL('Temps')}" + DIM("DC ") + f"{fmt(cur.get('dc_temp'), 1)}°"
             + DIM("  AC ") + f"{fmt(cur.get('ac_temp'), 1)}°"
             + DIM("  Batt ") + f"{fmt(cur.get('battery_temp'), 1)}°")
    usage = (f"  {LBL('Usage')}" + DIM("today ")
             + f"{YEL(fmt(today.get('pv_kwh'), 1))} in · {MAG(fmt(today.get('load_kwh'), 1))} out kWh")
    if watch:
        usage += DIM(f"   ·   life {fmt(life.get('pv_kwh'), 1)} / {fmt(life.get('load_kwh'), 1)} kWh")
    L.append(usage)
    faults = cur.get("faults") or []
    if faults:
        L.append(f"  {LBL('Status')}" + RED(", ".join(f"F{f['code']:02d} {f['text']}" for f in faults)))
    else:
        L.append(f"  {LBL('Status')}" + GREEN("OK") + DIM("  no active faults"))
    return "\n".join(L)


def main():
    args = sys.argv[1:]
    if any(a in ("usage", "-usage", "--usage", "-u") for a in args):
        clear()
        print(render_usage())
        return
    if any(a in ("export-hourly", "-export-hourly", "--export-hourly") for a in args):
        print(export_hourly())  # don't clear() — keep the path on screen to copy
        return
    watch = any(a in ("watch", "-w", "--watch") for a in args)
    show_in = any(a in ("in", "-i", "--in") for a in args)
    if not watch:
        clear()
        print(render(show_in=show_in))
        return
    try:
        while True:
            clear()
            sys.stdout.write(render(show_in=show_in, watch=True) + "\n"
                             + DIM("  (refreshing every 5s · Ctrl+C to quit)") + "\n")
            sys.stdout.flush()
            time.sleep(5)
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    main()
