#!/usr/bin/env python3
"""`solar` — a text status view of the dashboard for the terminal (e.g. the Raspberry Pi
Connect remote shell). Reads the local JSON API; stdlib-only, no dependencies.

    solar           one-shot status (AC input hidden)
    solar in        also show the AC input line
    solar usage     today's + yesterday's PV / Load / Battery energy totals (the dashboard's Today strip)
    solar export-hourly   write today's hourly energy to a CSV on the Pi (to pull remotely)
    solar snapshot  write a self-contained HTML snapshot of the dashboard (to pull + open offline)
    solar watch     refresh every few seconds, with lifetime totals (Ctrl+C to quit)
    solar watch in  watch + AC input

Override the target with SOLAR_DASH_URL (default http://127.0.0.1:8000).
Override the export folder (CSV + HTML) with SOLAR_EXPORT_DIR (default ~/solardash/exports).
"""
import csv
import html
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


# --- `solar snapshot`: a self-contained static HTML view of the dashboard ----------------
# Server-rendered here so the file always displays offline (no live fetches, no JS, no CDN).
# Palette mirrors solardash/web/style.css so it reads like the real dashboard.
_SNAPSHOT_CSS = """
:root{--bg:#0E1116;--surface:#161A21;--surface2:#1C212B;--line:#262C37;--txt:#EAF0F6;
--txt2:#9BA7B6;--txt3:#626C7B;--charge:#34D399;--discharge:#FBBF24;--load:#9C8CFB;--pv:#FBBF24;
--fault:#F87171;--accent:#22D3EE;--track:rgba(255,255,255,.07);
--mono:ui-monospace,"Cascadia Mono","Segoe UI Mono",Menlo,Consolas,monospace;
--sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,system-ui,sans-serif}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.45 var(--sans);
-webkit-font-smoothing:antialiased;padding-bottom:32px}
.topbar{position:sticky;top:0;display:flex;align-items:center;justify-content:space-between;
gap:12px;flex-wrap:wrap;padding:14px 18px;background:var(--bg);border-bottom:1px solid var(--line)}
.brand{display:flex;align-items:center;gap:10px}
.brand h1{font-size:17px;font-weight:650;margin:0}
.dot{width:9px;height:9px;border-radius:50%;background:var(--txt3)}
.dot.live{background:var(--charge)}.dot.stale{background:var(--discharge)}.dot.down{background:var(--fault)}
.badge{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--txt3);
border:1px solid var(--line);border-radius:999px;padding:2px 8px}
.status{color:var(--txt2);font-size:13px;font-variant-numeric:tabular-nums}
main{max-width:1100px;margin:0 auto;padding:16px}
.panel{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:16px}
.panel-title{font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--txt2);
font-weight:600;margin:0 0 12px}
.today{display:grid;grid-template-columns:repeat(2,1fr);gap:12px}
.hero{display:grid;grid-template-columns:repeat(2,1fr);gap:12px;margin-top:14px}
@media(min-width:760px){.today{grid-template-columns:repeat(4,1fr)}.hero{grid-template-columns:repeat(4,1fr)}}
.lt{background:var(--surface2);border:1px solid var(--line);border-radius:14px;padding:12px}
.lt label{display:block;font-size:11px;color:var(--txt2);text-transform:uppercase;letter-spacing:.4px}
.lt .v{font-size:24px;font-weight:650;font-variant-numeric:tabular-nums;margin-top:4px}
.lt .v small,.big small,.pc small{font-size:12px;color:var(--txt2);font-weight:500;margin-left:3px}
.lt.in .v{color:var(--pv)}.lt.out .v{color:var(--load)}
.tile .k{font-size:12px;color:var(--txt2);text-transform:uppercase;letter-spacing:.5px}
.big{font-size:30px;font-weight:700;font-variant-numeric:tabular-nums;margin:6px 0 2px}
.sub{font-size:12.5px;color:var(--txt2)}
.bar{height:8px;border-radius:5px;background:var(--track);overflow:hidden;margin:10px 0 6px}
.bar>i{display:block;height:100%;border-radius:5px}
.section{margin-top:14px}
.legend{display:flex;gap:18px;font-size:12px;color:var(--txt2);margin-bottom:10px}
.legend i{display:inline-block;width:10px;height:10px;border-radius:3px;margin-right:6px;vertical-align:-1px}
.packs{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}
.pack{background:var(--surface2);border:1px solid var(--line);border-radius:12px;padding:10px}
.pack .nm{font-size:12px;color:var(--txt2)}
.pack .pc{font-size:20px;font-weight:650;margin-top:2px}
.fault{color:var(--fault);font-weight:600}.ok{color:var(--charge);font-weight:600}
.foot{color:var(--txt3);font-size:12px;margin-top:20px;text-align:center}
.empty{color:var(--txt3);font-size:13px;padding:8px 0}
svg{display:block;width:100%;height:auto}svg text{font-family:var(--mono)}
"""


def _svg_hourly(buckets):
    """Inline SVG: today's hourly Solar vs Load (grouped bars, kWh). No JS, no external deps."""
    if not buckets:
        return '<div class="empty">No hourly data recorded yet today.</div>'
    pv = [b.get("pv_kwh") or 0 for b in buckets]
    load = [b.get("load_kwh") or 0 for b in buckets]
    labels = [(b.get("bucket") or "")[-5:] for b in buckets]  # "HH:00"
    peak = max(pv + load) or 1
    W, H, L, R, T, B = 920, 220, 40, 10, 12, 26
    pw, ph, n = W - L - R, H - T - B, len(buckets)
    gw = pw / n
    bw = max(2.0, min(11.0, gw / 2 - 1.5))
    p = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Today: hourly solar vs load">']
    for frac in (0.0, 0.5, 1.0):  # gridlines + y labels
        y = T + ph * (1 - frac)
        p.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" stroke="#262C37"/>')
        p.append(f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#626C7B">{peak * frac:.1f}</text>')
    for i in range(n):
        x0 = L + i * gw + (gw - 2 * bw) / 2
        for val, color, off in ((pv[i], "#FBBF24", 0.0), (load[i], "#9C8CFB", bw)):
            bh = ph * (val / peak)
            p.append(f'<rect x="{x0 + off:.1f}" y="{T + ph - bh:.1f}" width="{bw:.1f}" height="{bh:.1f}" rx="1.5" fill="{color}"/>')
    step = max(1, n // 8)  # sparse x labels so they don't collide
    for i in range(0, n, step):
        p.append(f'<text x="{L + i * gw + gw / 2:.1f}" y="{H - 8}" text-anchor="middle" font-size="10" fill="#626C7B">{labels[i]}</text>')
    p.append("</svg>")
    return "".join(p)


def _tile(k, big, unit, sub, color, fill_pct):
    return (f'<div class="panel tile"><div class="k">{html.escape(k)}</div>'
            f'<div class="big" style="color:{color}">{big}<small>{unit}</small></div>'
            f'<div class="bar"><i style="width:{max(0.0, min(100.0, fill_pct)):.0f}%;background:{color}"></i></div>'
            f'<div class="sub">{sub}</div></div>')


def _pack_card(p):
    soc = p.get("soc")
    return (f'<div class="pack"><div class="nm">{html.escape(str(p.get("name", "pack")))}</div>'
            f'<div class="pc">{fmt(soc)}<small>%</small></div>'
            f'<div class="bar"><i style="width:{max(0.0, min(100.0, soc or 0)):.0f}%;background:var(--charge)"></i></div>'
            f'<div class="sub">{fmt(p.get("voltage"), 2)} V · {fmt(p.get("temp_max"), 1)}° · Δ{fmt(p.get("cell_delta"), 3)} V</div></div>')


def _snapshot_doc(cur, today, hourly, life, batt):
    """Assemble the full self-contained HTML document from the captured API payloads."""
    ts = cur.get("ts") or int(time.time())
    age = int(time.time()) - ts
    dot, lab = ("live", "live") if age <= 120 else ("stale", "stale") if age <= 600 else ("down", "old")
    when = time.strftime("%a %d %b %Y · %I:%M %p %Z", time.localtime(ts))
    gen = time.strftime("%a %d %b %Y · %I:%M %p %Z", time.localtime())

    today_html = (
        '<section class="panel"><h2 class="panel-title">Today</h2><div class="today">'
        f'<div class="lt in"><label>Input · Solar</label><div class="v">{fmt(today.get("pv_kwh"), 1)}<small>kWh</small></div></div>'
        f'<div class="lt out"><label>Output · Load</label><div class="v">{fmt(today.get("load_kwh"), 1)}<small>kWh</small></div></div>'
        f'<div class="lt"><label>Battery charged</label><div class="v">{fmt(today.get("charge_kwh"), 1)}<small>kWh</small></div></div>'
        f'<div class="lt"><label>Battery discharged</label><div class="v">{fmt(today.get("discharge_kwh"), 1)}<small>kWh</small></div></div>'
        '</div></section>'
    )

    pv, load = cur.get("pv_power"), cur.get("load_total")
    soc, bw = cur.get("battery_soc"), cur.get("battery_power")
    charging = (cur.get("battery_current") or 0) >= 0
    tone = "var(--charge)" if charging else "var(--discharge)"
    sign = "+" if (bw or 0) > 0 else ""
    eta_min, kind = cur.get("battery_eta_minutes"), cur.get("battery_eta_kind")
    eta = "holding / idle" if eta_min is None else (f"▲ {hm(eta_min)} to full" if kind == "full" else f"▼ {hm(eta_min)} to empty")

    batt_tile = (
        '<div class="panel tile"><div class="k">Battery</div>'
        f'<div class="big" style="color:{tone}">{fmt(soc)}<small>%</small></div>'
        f'<div class="bar"><i style="width:{max(0.0, min(100.0, soc or 0)):.0f}%;background:{tone}"></i></div>'
        f'<div class="sub" style="color:{tone}">{sign}{fmt(bw)} W {"charging" if charging else "discharging"}</div>'
        f'<div class="sub">{fmt(cur.get("battery_voltage"), 1)} V · {html.escape(eta)}</div></div>'
    )
    temps_tile = (
        '<div class="panel tile"><div class="k">Temperatures</div>'
        f'<div class="big">{fmt(cur.get("battery_temp"), 1)}<small>°C batt</small></div>'
        f'<div class="sub">DC {fmt(cur.get("dc_temp"), 1)}° · AC {fmt(cur.get("ac_temp"), 1)}°</div></div>'
    )
    hero_html = (
        '<section class="hero">'
        + _tile("Solar PV", fmt(pv), "W", f'PV1 {fmt(cur.get("pv1_power"))} · PV2 {fmt(cur.get("pv2_power"))} W', "var(--pv)", (pv or 0) / 4000 * 100)
        + _tile("Load", fmt(load), "W", f'L1 {fmt(cur.get("load_power"))} · L2 {fmt(cur.get("load_l2_power"))} W', "var(--load)", (load or 0) / 4000 * 100)
        + batt_tile + temps_tile + '</section>'
    )

    chart_html = (
        '<section class="panel section"><h2 class="panel-title">Today · hourly energy</h2>'
        '<div class="legend"><span><i style="background:#FBBF24"></i>Solar (kWh)</span>'
        '<span><i style="background:#9C8CFB"></i>Load (kWh)</span></div>'
        f'{_svg_hourly(hourly)}</section>'
    )

    packs = (batt or {}).get("packs") or []
    if packs:
        bank = (batt or {}).get("bank") or {}
        batt_html = (
            f'<section class="panel section"><h2 class="panel-title">Battery bank · '
            f'{fmt(bank.get("soc"))}% · {fmt(bank.get("voltage"), 1)} V</h2>'
            f'<div class="packs">{"".join(_pack_card(p) for p in packs)}</div></section>'
        )
    else:
        batt_html = ""

    faults = cur.get("faults") or []
    if faults:
        txt = ", ".join(f'F{int(f.get("code", 0)):02d} {html.escape(str(f.get("text", "")))}' for f in faults)
        status_html = f'<section class="panel section"><h2 class="panel-title">Status</h2><div class="fault">⚠ {txt}</div></section>'
    else:
        status_html = '<section class="panel section"><h2 class="panel-title">Status</h2><div class="ok">● OK — no active faults</div></section>'

    foot = (f'<div class="foot">Lifetime {fmt(life.get("pv_kwh"), 1)} kWh in · {fmt(life.get("load_kwh"), 1)} kWh out'
            f' — static snapshot generated {html.escape(gen)}. Not live.</div>')

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<title>Solar snapshot · {html.escape(when)}</title><style>{_SNAPSHOT_CSS}</style></head><body>'
        f'<header class="topbar"><div class="brand"><span class="dot {dot}"></span><h1>Solar Tracking</h1>'
        f'<span class="badge">snapshot</span></div>'
        f'<div class="status">{html.escape(lab)} · {html.escape(when)}</div></header>'
        f'<main>{today_html}{hero_html}{chart_html}{batt_html}{status_html}{foot}</main></body></html>'
    )


def render_snapshot():
    """`solar snapshot` — capture the live API payloads and write a static HTML dashboard."""
    try:
        cur = get("/api/current")
    except Exception:
        return RED("offline ●") + DIM(f"  dashboard unreachable ({BASE})")
    if not cur.get("available"):
        return YEL("waiting ●") + DIM("  no data yet — nothing to snapshot")
    try:
        days = day_buckets()
    except Exception:
        days = {}
    now = time.localtime()
    today = days.get(time.strftime("%Y-%m-%d", now), {})
    today_mid = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
    try:
        hourly = get(f"/api/energy?period=hour&start={today_mid}").get("buckets", [])
    except Exception:
        hourly = []
    try:
        life = get("/api/energy/lifetime")
    except Exception:
        life = {}
    try:
        batt = get("/api/battery")
    except Exception:
        batt = {}

    doc = _snapshot_doc(cur, today, hourly, life, batt)
    try:
        os.makedirs(EXPORT_DIR, exist_ok=True)
        path = os.path.join(EXPORT_DIR, f"solar-snapshot-{time.strftime('%Y-%m-%d_%H%M', now)}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
    except OSError as e:
        return RED("✗ snapshot failed  ") + DIM(str(e))
    return (GREEN("✔ snapshot  ") + "dashboard captured\n"
            + f"  {BOLD(path)}\n"
            + DIM("  pull it via Pi Connect file transfer, then open it in any browser."))


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
    if any(a in ("snapshot", "-snapshot", "--snapshot") for a in args):
        print(render_snapshot())  # don't clear() — keep the path on screen to copy
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
