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
import math
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
# It inlines the dashboard's own web/style.css and emits the same gauge / flow / panel markup,
# so the snapshot looks like the real page: radial power dials, power-flow, battery bank, etc.
_WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")

# Snapshot-only tweaks layered on top of the dashboard's stylesheet.
_SNAP_TWEAKS = (
    ".badge{font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--txt3);"
    "border:1px solid var(--line);border-radius:999px;padding:2px 9px}"
    ".snap-note{color:var(--txt3);font-size:12px}"
    ".ph-svg{display:block;width:100%;height:auto;margin-top:6px}.ph-svg text{font-family:var(--mono)}"
)

# Minimal fallback, only used if web/style.css can't be read (unusual install layout).
_FALLBACK_CSS = (
    ":root{--bg:#0E1116;--surface:#161A21;--surface2:#1C212B;--line:#262C37;--txt:#EAF0F6;"
    "--txt2:#9BA7B6;--txt3:#626C7B;--charge:#34D399;--discharge:#FBBF24;--load:#9C8CFB;"
    "--pv:#FBBF24;--fault:#F87171;--accent:#22D3EE;--track:rgba(255,255,255,.07);"
    "--sans:system-ui,sans-serif;--mono:ui-monospace,Consolas,monospace}"
    "body{background:var(--bg);color:var(--txt);font-family:var(--sans);margin:0;padding:16px}"
    ".panel{background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:16px;margin:12px 0}"
)


def _dashboard_css():
    """The live dashboard's stylesheet, read from the package so the snapshot always matches it."""
    try:
        with open(os.path.join(_WEB_DIR, "style.css"), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return _FALLBACK_CSS


# Static SVG radial gauge — a faithful port of web/components.js Gauge (270° arc, 11 ticks,
# c1->c2 gradient fill to the value, and a circular tip marker).
_G = {"CX": 100, "CY": 100, "R": 72, "SW": 13, "START": 135, "SWEEP": 270, "TICKS": 11}


def _polar(cx, cy, r, deg):
    a = math.radians(deg)
    return cx + r * math.cos(a), cy + r * math.sin(a)


def _arc(cx, cy, r, a0, a1):
    x0, y0 = _polar(cx, cy, r, a0)
    x1, y1 = _polar(cx, cy, r, a1)
    large = 1 if (a1 - a0) % 360 > 180 else 0
    return f"M {x0:.2f} {y0:.2f} A {r} {r} 0 {large} 1 {x1:.2f} {y1:.2f}"


def _gauge_ticks():
    g = _G
    out = []
    for i in range(g["TICKS"]):
        ang = g["START"] + g["SWEEP"] * i / (g["TICKS"] - 1)
        major = i % 5 == 0
        r_in = g["R"] + g["SW"] / 2 + 4
        r_out = g["R"] + g["SW"] / 2 + (11 if major else 7)
        x1, y1 = _polar(g["CX"], g["CY"], r_in, ang)
        x2, y2 = _polar(g["CX"], g["CY"], r_out, ang)
        out.append(f'<line x1="{x1:.2f}" y1="{y1:.2f}" x2="{x2:.2f}" y2="{y2:.2f}" '
                   f'stroke="#626C7B" stroke-opacity="{0.7 if major else 0.4}" '
                   f'stroke-width="{1.6 if major else 1}" stroke-linecap="round"/>')
    return "".join(out)


def _gauge(gid, value, mx, c1, c2, unit, sub):
    """One radial dial, in the dashboard's gauge markup so the inlined CSS styles it."""
    g = _G
    v = value or 0
    pct = max(0.0, min(1.0, v / (mx or 1)))
    track = _arc(g["CX"], g["CY"], g["R"], g["START"], g["START"] + g["SWEEP"])
    tipx, tipy = _polar(g["CX"], g["CY"], g["R"], 0)
    ang = g["START"] + g["SWEEP"] * pct
    return (
        f'<div class="gauge"><svg viewBox="0 0 200 200" class="g-svg">'
        f'<defs><linearGradient id="grad_{gid}" x1="0" y1="1" x2="1" y2="0">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
        f'<g class="g-ticks">{_gauge_ticks()}</g>'
        f'<path class="g-track" d="{track}"/>'
        f'<path class="g-fill" d="{track}" pathLength="1000" stroke="url(#grad_{gid})" '
        f'style="stroke-dashoffset:{1000 * (1 - pct):.0f}"/>'
        f'<g class="g-tiprot" transform="rotate({ang:.2f} {g["CX"]} {g["CY"]})">'
        f'<circle class="g-tip" cx="{tipx:.2f}" cy="{tipy:.2f}" r="8" style="stroke:{c2}"/></g>'
        f'</svg><div class="gauge-center"><div class="g-val"><span class="g-num">{fmt(v)}</span>'
        f'<small>{unit}</small></div><div class="g-sub">{html.escape(sub)}</div></div></div>'
    )


def _leg(label, color, watts, amps, sub):
    """Per-string / per-leg metric block shown under a gauge (PV1/PV2, L1/L2).

    Carries both the W and A value so the W/A toggle can switch it client-side; renders W first."""
    w = max(0.0, min(100.0, (watts or 0) / 4000 * 100))
    return (f'<div class="leg" data-w="{watts or 0}" data-a="{amps or 0}">'
            f'<span class="leg-top"><i class="swatch" style="background:{color}"></i>{label}</span>'
            f'<span class="leg-val"><span class="leg-num">{fmt(watts)}</span> <span class="leg-u">W</span></span>'
            f'<div class="leg-bar"><div style="background:{color};width:{w:.0f}%"></div></div>'
            f'<span class="leg-sub">{html.escape(sub)}</span></div>')


def _gauge_panel(gid, title, w_val, w_max, w_unit, w_sub, a_val, a_max, a_unit, a_sub, c1, c2, legs):
    """A gauge panel with a W/A unit toggle. Both unit datasets ride on the panel so the inline
    script can re-scale the dial + legs on click; the server renders the W state initially."""
    data = (f'data-gpanel="{gid}" '
            f'data-w-val="{w_val or 0}" data-w-max="{w_max}" data-w-unit="{w_unit}" data-w-sub="{html.escape(w_sub)}" data-w-dec="0" '
            f'data-a-val="{a_val or 0}" data-a-max="{a_max}" data-a-unit="{a_unit}" data-a-sub="{html.escape(a_sub)}" data-a-dec="1"')
    toggle = (f'<div class="head-tools"><div class="unit-toggle" data-gauge="{gid}">'
              f'<button data-u="W" class="active">W</button><button data-u="A">A</button></div></div>')
    legs_html = '<div class="legs">' + '<div class="leg-div"></div>'.join(legs) + '</div>'
    return (f'<div class="panel" {data}>'
            f'<div class="panel-head"><span class="panel-title">{html.escape(title)}</span>{toggle}</div>'
            + _gauge(gid, w_val, w_max, c1, c2, w_unit, w_sub) + legs_html + '</div>')


_ICON_SUN = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
             '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19'
             'M19 5l-1.4 1.4M6.4 17.6L5 19"/></svg>')
_ICON_INV = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">'
             '<path d="M12 3v8"/><path d="M7.3 6.3a7 7 0 1 0 9.4 0"/></svg>')
_ICON_BATT = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" '
              'stroke-linejoin="round"><rect x="2.5" y="8" width="16" height="9" rx="2"/><path d="M21.5 11.5v2"/>'
              '<path d="M6 12.5h6"/></svg>')


def _flow(cur):
    """Power-flow diagram (Solar -> Inverter -> Battery). The pure-CSS dot animation runs in the static page."""
    pv = round(cur.get("pv_power") or 0)
    load = round(cur.get("load_total") or 0)
    batt = round(cur.get("battery_power") or 0)
    charging = (cur.get("battery_current") or 0) >= 0
    tone = "#34D399" if charging else "#FBBF24"
    solar_w = " active" if pv > 0 else ""
    batt_w = (" active" if abs(batt) > 5 else "") + ("" if charging else " reverse")
    bic_bg = "rgba(52,211,153,0.13)" if charging else "rgba(251,191,36,0.13)"
    return (
        '<div class="flow">'
        f'<div class="flow-node" data-n="solar"><div class="flow-ic" style="color:#22D3EE;background:rgba(34,211,238,0.13)">{_ICON_SUN}</div>'
        f'<div class="flow-val">{fmt(pv)} W</div><div class="flow-lbl">Solar</div></div>'
        f'<div class="flow-wire{solar_w}" data-w="solar"><span class="flow-line"></span><span class="flow-dot" style="background:#22D3EE"></span></div>'
        f'<div class="flow-node" data-n="inv"><div class="flow-ic" style="color:#EAF0F6;background:#1C212B">{_ICON_INV}</div>'
        f'<div class="flow-val">{fmt(load)} W</div><div class="flow-lbl">Inverter</div></div>'
        f'<div class="flow-wire{batt_w}" data-w="batt"><span class="flow-line"></span><span class="flow-dot" style="background:{tone}"></span></div>'
        f'<div class="flow-node" data-n="batt"><div class="flow-ic" style="color:{tone};background:{bic_bg}">{_ICON_BATT}</div>'
        f'<div class="flow-val">{"+" if batt > 0 else ""}{fmt(batt)} W</div><div class="flow-lbl">Battery</div></div>'
        '</div>'
    )


def _nice_step(mx, target=4):
    """'Nice' axis step (1/2/5 x 10^n) so the energy-bar ticks are round numbers."""
    raw = (mx or 1) / target
    if raw <= 0:
        return 1
    powv = 10 ** math.floor(math.log10(raw))
    n = raw / powv
    s = 1 if n < 1.5 else 2 if n < 3 else 5 if n < 7 else 10
    return s * powv


def _energy_bars(hourly):
    """Today's hourly Solar vs Load as the dashboard's grouped energy bars — all 24 hours, with
    future / empty hours shown as a flat baseline. Each bar carries data-* for the click popup."""
    by_hour = {}
    for b in hourly or []:
        try:
            by_hour[int((b.get("bucket") or "")[-5:-3])] = b  # "YYYY-MM-DD HH:00" -> HH
        except ValueError:
            pass
    slots = []
    for hr in range(24):
        b = by_hour.get(hr) or {}
        slots.append({"label": f"{hr:02d}", "title": f"{hr:02d}:00", "pv": b.get("pv_kwh") or 0,
                      "load": b.get("load_kwh") or 0, "charge": b.get("charge_kwh") or 0,
                      "discharge": b.get("discharge_kwh") or 0})
    max_v = max((max(s["pv"], s["load"]) for s in slots), default=0)
    step = _nice_step(max_v)
    axis_max = max(step, math.ceil(max_v / step) * step) if max_v else step
    fmt_t = lambda t: "0" if abs(t) < 1e-9 else (f"{t:.1f}" if axis_max < 10 else str(round(t)))
    ticks, t = [], axis_max
    while t > -1e-9:
        ticks.append(t)
        t -= step
    axis = '<div class="eaxis">' + "".join(f"<span>{fmt_t(tk)}</span>" for tk in ticks) + "</div>"
    bars = "".join(
        f'<div class="ebar-group" data-title="{s["title"]}" data-pv="{s["pv"]}" data-load="{s["load"]}" '
        f'data-charge="{s["charge"]}" data-discharge="{s["discharge"]}"><div class="ebar-plot">'
        f'<div class="ebar ebar-in" style="height:{s["pv"] / axis_max * 100:.1f}%"></div>'
        f'<div class="ebar ebar-out" style="height:{s["load"] / axis_max * 100:.1f}%"></div></div>'
        f'<div class="ebar-x">{s["label"]}</div></div>'
        for s in slots
    )
    return f'<div class="ebars">{axis}<div class="ebars-track">{bars}</div></div>'


# Tiny inline script (offline, no network) driving the snapshot's three interactions: the
# energy-bar click popup (port of web/components.js showEbarPopup), the PV/Load W<->A unit
# toggle (port of the dashboard's Gauge.setUnit), and a client-side "Export CSV" of the bars.
_SNAP_SCRIPT = """<script>
(function(){
 /* energy-bar click popup */
 var pop;
 function ensure(){if(!pop){pop=document.createElement('div');pop.className='ebar-popup';pop.style.display='none';document.body.appendChild(pop);}return pop;}
 function kwh(v){v=v||0;return v<1?Math.round(v*1000)+' Wh':v.toFixed(2)+' kWh';}
 function show(g){
  var p=ensure();
  var pv=+g.dataset.pv||0,load=+g.dataset.load||0,ch=+g.dataset.charge||0,di=+g.dataset.discharge||0;
  var h='<div class="pop-title">'+(g.dataset.title||'')+'</div>'+
   '<div class="pop-row"><i style="background:#FBBF24"></i>Solar PV<b>'+kwh(pv)+'</b></div>'+
   '<div class="pop-row"><i style="background:#9C8CFB"></i>AC Output<b>'+kwh(load)+'</b></div>';
  if(ch||di){var net=ch>=di?'+'+kwh(ch):'\\u2212'+kwh(di);h+='<div class="pop-row"><i style="background:#34D399"></i>Battery<b>'+net+'</b></div>';}
  p.innerHTML=h;p.style.display='block';
  var r=g.getBoundingClientRect(),pr=p.getBoundingClientRect();
  var left=r.left+r.width/2-pr.width/2;left=Math.max(8,Math.min(window.innerWidth-pr.width-8,left));
  var top=r.top-pr.height-8;if(top<8)top=r.bottom+8;
  p.style.left=Math.round(left)+'px';p.style.top=Math.round(top)+'px';
 }
 function hidePop(){if(pop)pop.style.display='none';}
 document.addEventListener('click',function(e){var g=e.target.closest('.ebar-group');if(g){show(g);}else if(!e.target.closest('.unit-toggle')&&!e.target.closest('#snapExport')){hidePop();}});
 window.addEventListener('resize',hidePop);

 /* PV / Load W<->A unit toggle: re-scale the dial + legs from the panel's data-{w,a}-* */
 function num(v,dec){return dec?(+v).toFixed(dec):Math.round(+v).toLocaleString();}
 function setUnit(panel,u){
  var max=+panel.getAttribute('data-'+u+'-max')||1,val=+panel.getAttribute('data-'+u+'-val')||0;
  var dec=+panel.getAttribute('data-'+u+'-dec')||0,unit=panel.getAttribute('data-'+u+'-unit')||'',sub=panel.getAttribute('data-'+u+'-sub')||'';
  var pct=Math.max(0,Math.min(1,val/(max||1)));
  panel.querySelector('.g-fill').style.strokeDashoffset=String(1000*(1-pct));
  panel.querySelector('.g-tiprot').setAttribute('transform','rotate('+(135+270*pct).toFixed(2)+' 100 100)');
  panel.querySelector('.g-num').textContent=num(val,dec);
  panel.querySelector('.g-val small').textContent=unit;
  panel.querySelector('.g-sub').textContent=sub;
  panel.querySelectorAll('.leg').forEach(function(leg){
   var lv=+leg.getAttribute('data-'+u)||0;
   leg.querySelector('.leg-num').textContent=num(lv,dec);
   leg.querySelector('.leg-u').textContent=unit;
   var bar=leg.querySelector('.leg-bar>div');
   if(bar)bar.style.width=Math.max(0,Math.min(100,lv/(max||1)*100)).toFixed(0)+'%';
  });
 }
 document.querySelectorAll('.unit-toggle button').forEach(function(btn){
  btn.addEventListener('click',function(){
   var tog=btn.closest('.unit-toggle'),panel=btn.closest('[data-gpanel]');
   tog.querySelectorAll('button').forEach(function(b){b.classList.toggle('active',b===btn);});
   setUnit(panel,btn.getAttribute('data-u').toLowerCase());
  });
 });

 /* Export CSV — built client-side from the embedded hourly bars (matches `solar export-hourly`) */
 var ex=document.getElementById('snapExport');
 if(ex)ex.addEventListener('click',function(){
  var rows=[['hour','solar_kwh','load_kwh','battery_charged_kwh','battery_discharged_kwh']];
  document.querySelectorAll('.ebar-group').forEach(function(g){
   rows.push([g.dataset.title,g.dataset.pv,g.dataset.load,g.dataset.charge,g.dataset.discharge]);
  });
  var csv=rows.map(function(r){return r.join(',');}).join('\\n');
  var a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([csv],{type:'text/csv'}));
  a.download='solar-hourly-'+(document.body.getAttribute('data-date')||'today')+'.csv';
  document.body.appendChild(a);a.click();
  setTimeout(function(){URL.revokeObjectURL(a.href);a.remove();},0);
 });
})();
</script>"""


def _power_history_svg(ts, pv, load, batt):
    """Inline SVG line chart of the last 6 h: Solar / Load / Battery power (W), no JS, no deps.
    Negative battery (discharging) dips below the zero line; gaps in a series break the line."""
    t0, t1 = ts[0], ts[-1]
    span = (t1 - t0) or 1
    vals = [v for arr in (pv, load, batt) for v in arr if v is not None]
    ymax = max(vals + [0.0])
    ymin = min(vals + [0.0])
    if ymax <= ymin:
        ymax = ymin + 1
    W, H, L, R, T, B = 960, 260, 50, 12, 10, 22
    pw, ph = W - L - R, H - T - B
    xf = lambda t: L + (t - t0) / span * pw
    yf = lambda v: T + (ymax - v) / (ymax - ymin) * ph
    p = [f'<svg viewBox="0 0 {W} {H}" class="ph-svg" role="img" aria-label="Power history, last 6 hours">']
    ystep = _nice_step(ymax - ymin, 4)
    tick = math.floor(ymin / ystep) * ystep
    while tick <= ymax + 1e-9:  # y gridlines + W labels (the zero line is drawn a touch stronger)
        y = yf(tick)
        strong = abs(tick) < 1e-9
        p.append(f'<line x1="{L}" y1="{y:.1f}" x2="{W - R}" y2="{y:.1f}" stroke="#262C37" stroke-width="{1.4 if strong else 1}"/>')
        p.append(f'<text x="{L - 6}" y="{y + 3:.1f}" text-anchor="end" font-size="10" fill="#626C7B">{tick:,.0f}</text>')
        tick += ystep
    for i in range(5):  # ~5 time labels across the span
        t = t0 + span * i / 4
        p.append(f'<text x="{xf(t):.1f}" y="{H - 7}" text-anchor="middle" font-size="10" fill="#626C7B">'
                 f'{time.strftime("%H:%M", time.localtime(t))}</text>')

    def line(arr, color):  # one polyline per contiguous (gap-free) run of points
        runs, seg = [], []
        for i, v in enumerate(arr):
            if v is None:
                if len(seg) > 1:
                    runs.append(seg)
                seg = []
            else:
                seg.append(f"{xf(ts[i]):.1f},{yf(v):.1f}")
        if len(seg) > 1:
            runs.append(seg)
        return "".join(f'<polyline points="{" ".join(s)}" fill="none" stroke="{color}" '
                       f'stroke-width="1.6" stroke-linejoin="round" stroke-linecap="round"/>' for s in runs)

    p.append(line(batt, "#34D399"))
    p.append(line(load, "#9C8CFB"))
    p.append(line(pv, "#FBBF24"))
    p.append("</svg>")
    return "".join(p)


def _power_history(hist, life):
    """Power-history card (header with lifetime totals + the 6 h line chart + legend)."""
    head = ('<section class="card chart-card"><div class="chart-head"><div class="head-left">'
            '<h2>Power history · last 6h</h2>'
            '<div class="lt-inline"><span class="lti-title">Lifetime</span>'
            f'<span class="lti in">Solar <b>{fmt(life.get("pv_kwh"), 1)}</b></span>'
            f'<span class="lti out">Load <b>{fmt(life.get("load_kwh"), 1)}</b></span> kWh</div>'
            '</div></div>')
    ts = (hist or {}).get("ts") or []
    series = (hist or {}).get("series") or {}
    if not ts:
        return head + '<div class="ebars-empty">No power history recorded yet — give it a bit.</div></section>'
    svg = _power_history_svg(ts, series.get("pv_power") or [], series.get("load_total") or [],
                             series.get("battery_power") or [])
    legend = ('<div class="legend">'
              '<span class="item"><i class="swatch" style="background:#FBBF24"></i>Solar PV</span>'
              '<span class="item"><i class="swatch" style="background:#9C8CFB"></i>Load</span>'
              '<span class="item"><i class="swatch" style="background:#34D399"></i>Battery</span></div>')
    return head + svg + legend + '</section>'


def _pack_card(p):
    """One battery pack, using the dashboard's bd-pack + socbar markup."""
    soc_n = p.get("soc")
    soc = max(0.0, min(100.0, soc_n or 0))
    charging = (p.get("current") or 0) >= 0
    fill = "#FBBF24" if (soc_n or 0) <= 15 else ("#34D399" if charging else "#FBBF24")
    cls = "val-pos" if charging else "val-neg"
    par = f'<span class="bd-pack-par">#{p["parallel"]}</span> ' if p.get("parallel") else ""
    a = p.get("current")
    return (f'<div class="bd-pack"><div class="bd-pack-head">'
            f'<span class="bd-pack-name">{par}{html.escape(str(p.get("name", "")))}</span>'
            f'<span class="bd-pack-soc {cls}">{fmt(soc_n)}<small>%</small></span></div>'
            f'<div class="socbar"><div class="socbar-fill" style="width:{soc:.0f}%;background:{fill}"></div>'
            f'<div class="socbar-seg"></div></div>'
            f'<div class="bd-pack-stats">{fmt(p.get("voltage"), 2)} V · '
            f'<span class="{cls}">{"+" if (a or 0) >= 0 else ""}{fmt(a, 1)} A</span> · '
            f'{fmt(p.get("temp_max"), 1)}°C</div></div>')


def _snapshot_doc(cur, today, hourly, life, batt, hist):
    """Assemble the full self-contained HTML document, mirroring the dashboard's layout."""
    ts = cur.get("ts") or int(time.time())
    age = int(time.time()) - ts
    dot, lab = ("live", "live") if age <= 120 else (("stale", "stale") if age <= 600 else ("down", "old"))
    when = time.strftime("%a %d %b %Y · %I:%M %p %Z", time.localtime(ts))
    gen = time.strftime("%a %d %b %Y · %I:%M %p %Z", time.localtime())

    # Today strip
    today_html = (
        '<section class="lifetime"><div class="lt-head">Today</div><div class="lt-grid">'
        f'<div class="lt-item in"><label>Input · Solar</label><div class="lt-val"><span>{fmt(today.get("pv_kwh"), 1)}</span><small>kWh</small></div></div>'
        f'<div class="lt-item out"><label>Output · Load</label><div class="lt-val"><span>{fmt(today.get("load_kwh"), 1)}</span><small>kWh</small></div></div>'
        f'<div class="lt-item"><label>Battery charged</label><div class="lt-val"><span>{fmt(today.get("charge_kwh"), 1)}</span><small>kWh</small></div></div>'
        f'<div class="lt-item"><label>Battery discharged</label><div class="lt-val"><span>{fmt(today.get("discharge_kwh"), 1)}</span><small>kWh</small></div></div>'
        '</div></section>'
    )

    # Gauges + legs (PV / Load) — the dials, with a W/A unit toggle
    pv_amps = (cur.get("pv1_current") or 0) + (cur.get("pv2_current") or 0)
    pv_panel = _gauge_panel(
        "pv", "Solar PV",
        cur.get("pv_power"), 4000, "W", "total input",
        pv_amps, 20, "A", "total current",
        "#22D3EE", "#4F9CF9",
        [_leg("PV1", "#22D3EE", cur.get("pv1_power"), cur.get("pv1_current"),
              f'{fmt(cur.get("pv1_voltage"), 1)} V · {fmt(cur.get("pv1_current"), 1)} A'),
         _leg("PV2", "#4F9CF9", cur.get("pv2_power"), cur.get("pv2_current"),
              f'{fmt(cur.get("pv2_voltage"), 1)} V · {fmt(cur.get("pv2_current"), 1)} A')],
    )
    load_amps = (cur.get("load_current") or 0) + (cur.get("load_l2_current") or 0)
    load_panel = _gauge_panel(
        "load", "AC Output · Load",
        cur.get("load_total"), 4000, "W", "real power · L1+L2",
        load_amps, 40, "A", "current · L1+L2",
        "#9C8CFB", "#9C8CFB",
        [_leg("L1", "#9C8CFB", cur.get("load_power"), cur.get("load_current"),
              f'{fmt(cur.get("load_voltage"), 1)} V · {fmt(cur.get("load_current"), 1)} A'),
         _leg("L2", "#9C8CFB", cur.get("load_l2_power"), cur.get("load_l2_current"),
              f'{fmt(cur.get("load_l2_voltage"), 1)} V · {fmt(cur.get("load_l2_current"), 1)} A')],
    )

    # Battery bank panel
    soc, bw = cur.get("battery_soc"), cur.get("battery_power")
    charging = (cur.get("battery_current") or 0) >= 0
    tone = "var(--charge)" if charging else "var(--discharge)"
    pill_cls, pill_txt = ("green", "charging") if charging else ("amber", "discharging")
    eta_min, kind = cur.get("battery_eta_minutes"), cur.get("battery_eta_kind")
    eta = "holding / idle" if eta_min is None else (f"▲ {hm(eta_min)} to full" if kind == "full" else f"▼ {hm(eta_min)} to empty")
    bank = (batt or {}).get("bank") or {}
    batt_temp = bank.get("temp_max") if bank.get("temp_max") is not None else cur.get("battery_temp")
    socp = max(0.0, min(100.0, soc or 0))
    batt_panel = (
        '<div class="panel battery-panel"><div class="panel-head"><span class="panel-title">Battery bank</span>'
        f'<span class="pill {pill_cls}">{pill_txt}</span></div>'
        f'<div class="batt-top"><div class="batt-soc">{fmt(soc)}<small>%</small></div>'
        f'<div class="batt-watts" style="color:{tone}">{"+" if (bw or 0) > 0 else ""}{fmt(bw)} W</div></div>'
        f'<div class="socbar"><div class="socbar-fill" style="width:{socp:.0f}%;background:{tone}"></div><div class="socbar-seg"></div></div>'
        f'<div class="batt-eta">{html.escape(eta)}</div>'
        '<div class="batt-foot">'
        f'<div><span>{fmt(cur.get("battery_voltage"), 1)}<small> V</small></span><label>Voltage</label></div>'
        f'<div><span>{"+" if (cur.get("battery_current") or 0) >= 0 else ""}{fmt(cur.get("battery_current"), 1)}<small> A</small></span><label>Current</label></div>'
        f'<div><span>{fmt(batt_temp, 1)}<small> °C</small></span><label>Temp</label></div>'
        '</div></div>'
    )

    flow_panel = ('<div class="panel flow-panel"><div class="panel-head"><span class="panel-title">Power flow</span></div>'
                  + _flow(cur) + '</div>')

    hero_html = (f'<section class="hero">{pv_panel}{load_panel}'
                 f'<div class="col4">{flow_panel}{batt_panel}</div></section>')

    # Secondary tiles: Temperatures / Machine state / Status
    faults = cur.get("faults") or []
    if faults:
        fcodes = " ".join(f'F{int(f.get("code", 0)):02d}' for f in faults)
        ftext = ", ".join(html.escape(str(f.get("text", ""))) for f in faults)
        fault_tile = (f'<div class="tile alert has-fault" data-k="fault"><div class="tile-label">Status</div>'
                      f'<div class="tile-value">{fcodes}</div><div class="tile-sub">{ftext}</div></div>')
    else:
        fault_tile = ('<div class="tile alert" data-k="fault"><div class="tile-label">Status</div>'
                      '<div class="tile-value ok">OK</div><div class="tile-sub">No active faults</div></div>')
    state = cur.get("machine_state")
    state_txt = html.escape(str(state)) if state not in (None, "") else "—"
    tiles_html = (
        '<section class="tiles">'
        f'<div class="tile" data-k="temp"><div class="tile-label">Temperatures</div>'
        f'<div class="tile-value"><span>{fmt(cur.get("dc_temp"), 1)}</span><small> DC</small></div>'
        f'<div class="tile-sub">AC {fmt(cur.get("ac_temp"), 1)}° · batt {fmt(batt_temp, 1)}°</div></div>'
        f'<div class="tile" data-k="state"><div class="tile-label">Machine state</div>'
        f'<div class="tile-value"><span>{state_txt}</span></div>'
        f'<div class="tile-sub">inverter status</div></div>'
        f'{fault_tile}</section>'
    )

    # Energy trends (today, hourly) — with a client-side CSV export
    energy_html = (
        '<section class="card energy-card"><div class="chart-head"><h2>Energy trends · today (hourly)</h2>'
        '<button class="export-btn" id="snapExport" title="Download the hourly data as CSV">Export CSV</button></div>'
        f'<div class="etotals"><span class="et in">Solar <b>{fmt(today.get("pv_kwh"), 1)}</b> kWh</span>'
        f'<span class="et out">Load <b>{fmt(today.get("load_kwh"), 1)}</b> kWh</span></div>'
        f'{_energy_bars(hourly)}'
        '<div class="legend"><span class="item"><i class="swatch" style="background:#FBBF24"></i>Solar PV</span>'
        '<span class="item"><i class="swatch" style="background:#9C8CFB"></i>AC Output</span></div></section>'
    )

    # Battery per-pack detail
    packs = (batt or {}).get("packs") or []
    if packs:
        packs = sorted(packs, key=lambda p: p.get("parallel") if p.get("parallel") is not None else 99)
        detail_html = ('<section class="card battery-detail-card"><div class="chart-head">'
                       '<h2>Batteries · Per-pack state of charge</h2></div>'
                       f'<div class="bd-packs">{"".join(_pack_card(p) for p in packs)}</div></section>')
    else:
        detail_html = ""

    power_history_html = _power_history(hist, life)

    foot = (f'<footer class="foot">Solar Tracking · static snapshot generated {html.escape(gen)}<br>'
            '<span class="snap-note">A point-in-time capture — not live. The dashboard itself auto-updates.</span></footer>')

    return (
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>'
        '<meta name="viewport" content="width=device-width, initial-scale=1"/>'
        f'<title>Solar snapshot · {html.escape(when)}</title>'
        f'<style>{_dashboard_css()}{_SNAP_TWEAKS}</style></head>'
        f'<body class="hide-acin" data-date="{time.strftime("%Y-%m-%d", time.localtime(ts))}">'
        '<header class="topbar"><div class="brand">'
        f'<span class="dot {dot}"></span><h1>Solar Tracking</h1><span class="badge">snapshot</span></div>'
        f'<div class="top-right"><div class="status">{html.escape(lab)} · {html.escape(when)}</div></div></header>'
        f'<main>{today_html}{hero_html}{detail_html}{tiles_html}{energy_html}{power_history_html}{foot}</main>'
        f'{_SNAP_SCRIPT}</body></html>'
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
    try:
        now_s = int(time.time())
        hist = get(f"/api/history?fields=pv_power,load_total,battery_power&start={now_s - 6 * 3600}&max_points=360")
    except Exception:
        hist = {}

    doc = _snapshot_doc(cur, today, hourly, life, batt, hist)
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
