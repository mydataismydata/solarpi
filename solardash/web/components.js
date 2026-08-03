"use strict";

/* Radial power gauge — a web port of the Android app's RadialGauge (ui/SolarGauge.kt):
   270° arc with a bottom gap (start 135°, sweep 270°), an 11-tick ring (major every 5th),
   a track, a c1->c2 gradient fill, and a circular tip marker at the value. Animated via CSS
   transitions on the fill's stroke-dashoffset and the tip group's rotation (~700ms). */

const GA = { CX: 100, CY: 100, R: 72, SW: 13, START: 135, SWEEP: 270, TICKS: 11 };

function polar(cx, cy, r, deg) {
  const a = (deg * Math.PI) / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function arcPath(cx, cy, r, a0, a1) {
  const [x0, y0] = polar(cx, cy, r, a0);
  const [x1, y1] = polar(cx, cy, r, a1);
  const large = (a1 - a0) % 360 > 180 ? 1 : 0;
  return `M ${x0.toFixed(2)} ${y0.toFixed(2)} A ${r} ${r} 0 ${large} 1 ${x1.toFixed(2)} ${y1.toFixed(2)}`;
}

function ticksSvg() {
  const { CX, CY, R, SW, START, SWEEP, TICKS } = GA;
  let out = "";
  for (let i = 0; i < TICKS; i++) {
    const ang = START + (SWEEP * i) / (TICKS - 1);
    const major = i % 5 === 0;
    const rIn = R + SW / 2 + 4;
    const rOut = R + SW / 2 + (major ? 11 : 7);
    const [x1, y1] = polar(CX, CY, rIn, ang);
    const [x2, y2] = polar(CX, CY, rOut, ang);
    out += `<line x1="${x1.toFixed(2)}" y1="${y1.toFixed(2)}" x2="${x2.toFixed(2)}" y2="${y2.toFixed(2)}" ` +
      `stroke="#626C7B" stroke-opacity="${major ? 0.7 : 0.4}" stroke-width="${major ? 1.6 : 1}" stroke-linecap="round"/>`;
  }
  return out;
}

class Gauge {
  constructor(el, opts) {
    this.el = el;
    this.max = opts.max || 1;
    const gid = "grad_" + opts.id;
    const track = arcPath(GA.CX, GA.CY, GA.R, GA.START, GA.START + GA.SWEEP);
    const [tipX, tipY] = polar(GA.CX, GA.CY, GA.R, 0); // base tip at angle 0; rotated into place
    el.classList.add("gauge");
    el.innerHTML =
      `<svg viewBox="0 0 200 200" class="g-svg">
         <defs>
           <linearGradient id="${gid}" x1="0" y1="1" x2="1" y2="0">
             <stop offset="0" stop-color="${opts.c1}"/><stop offset="1" stop-color="${opts.c2}"/>
           </linearGradient>
         </defs>
         <g class="g-ticks">${ticksSvg()}</g>
         <path class="g-track" d="${track}"/>
         <path class="g-fill" d="${track}" pathLength="1000" stroke="url(#${gid})" style="stroke-dashoffset:1000"/>
         <g class="g-tiprot" transform="rotate(${GA.START} ${GA.CX} ${GA.CY})">
           <circle class="g-tip" cx="${tipX.toFixed(2)}" cy="${tipY.toFixed(2)}" r="8" style="stroke:${opts.c2}"/>
         </g>
       </svg>
       <div class="gauge-center">
         <div class="g-val"><span class="g-num">—</span><small>${opts.unit}</small></div>
         <div class="g-sub">${opts.sub}</div>
       </div>`;
    this.fill = el.querySelector(".g-fill");
    this.tip = el.querySelector(".g-tiprot");
    this.num = el.querySelector(".g-num");
    this.unitEl = el.querySelector(".g-val small");
    this.subEl = el.querySelector(".g-sub");
    this.decimals = 0;
  }

  // Switch the gauge's scale/label (e.g. W <-> A): max for the arc, unit text, sub caption, decimals.
  setUnit(unit, max, sub, decimals = 0) {
    this.max = max || 1;
    this.decimals = decimals;
    if (this.unitEl) this.unitEl.textContent = unit;
    if (sub != null && this.subEl) this.subEl.textContent = sub;
  }

  set(value) {
    const v = Number(value) || 0;
    const pct = Math.max(0, Math.min(1, v / (this.max || 1)));
    this.fill.style.strokeDashoffset = String(1000 * (1 - pct));
    const ang = GA.START + GA.SWEEP * pct;
    this.tip.setAttribute("transform", `rotate(${ang.toFixed(2)} ${GA.CX} ${GA.CY})`);
    this.num.textContent = this.decimals ? v.toFixed(this.decimals) : Math.round(v).toLocaleString();
  }
}

/* Dual-source gauge — two fill arcs stacked on one 270° track for a device drawing from two
   sources at once (the mini-split's solar DC + grid AC). A "total" arc (solar+grid) is drawn in
   the grid color, then the solar portion is painted on top in the solar color, so [0..solar]
   reads as solar and [solar..total] as grid. Both arcs animate via stroke-dashoffset like Gauge. */
class DualGauge {
  constructor(el, opts) {
    this.el = el;
    this.max = opts.max || 1;
    this.solarColor = opts.solar;
    this.gridColor = opts.grid;
    const track = arcPath(GA.CX, GA.CY, GA.R, GA.START, GA.START + GA.SWEEP);
    const [tipX, tipY] = polar(GA.CX, GA.CY, GA.R, 0);
    el.classList.add("gauge");
    el.innerHTML =
      `<svg viewBox="0 0 200 200" class="g-svg">
         <g class="g-ticks">${ticksSvg()}</g>
         <path class="g-track" d="${track}"/>
         <path class="g-fill g-fill-total" d="${track}" pathLength="1000" stroke="${opts.grid}" style="stroke-dashoffset:1000"/>
         <path class="g-fill g-fill-solar" d="${track}" pathLength="1000" stroke="${opts.solar}" style="stroke-dashoffset:1000"/>
         <g class="g-tiprot" transform="rotate(${GA.START} ${GA.CX} ${GA.CY})">
           <circle class="g-tip" cx="${tipX.toFixed(2)}" cy="${tipY.toFixed(2)}" r="8" style="stroke:${opts.grid}"/>
         </g>
       </svg>
       <div class="gauge-center">
         <div class="g-val"><span class="g-num">—</span><small>${opts.unit}</small></div>
         <div class="g-sub">${opts.sub}</div>
       </div>`;
    this.fillTotal = el.querySelector(".g-fill-total"); // grid color, spans 0..total
    this.fillSolar = el.querySelector(".g-fill-solar"); // solar color, spans 0..solar, painted on top
    this.tip = el.querySelector(".g-tiprot");
    this.tipDot = el.querySelector(".g-tip");
    this.num = el.querySelector(".g-num");
    this.subEl = el.querySelector(".g-sub");
  }

  // solar + grid in the gauge's unit (watts). Total = solar+grid fills the arc; solar drawn on top.
  set(solar, grid) {
    const s = Math.max(0, Number(solar) || 0);
    const g = Math.max(0, Number(grid) || 0);
    const total = s + g;
    const tFrac = Math.max(0, Math.min(1, total / (this.max || 1)));
    const sFrac = Math.max(0, Math.min(1, s / (this.max || 1)));
    this.fillTotal.style.strokeDashoffset = String(1000 * (1 - tFrac));
    this.fillSolar.style.strokeDashoffset = String(1000 * (1 - sFrac));
    const ang = GA.START + GA.SWEEP * tFrac;
    this.tip.setAttribute("transform", `rotate(${ang.toFixed(2)} ${GA.CX} ${GA.CY})`);
    this.tipDot.style.stroke = g > 0 ? this.gridColor : this.solarColor; // tint by the source at the tip
    this.num.textContent = Math.round(total).toLocaleString();
  }

  setSub(text) { if (this.subEl) this.subEl.textContent = text; }
}

/* Segmented state-of-charge bar — port of SocBar (ui/SolarComponents.kt): a rounded pill
   with a colored fill and 20 segment dividers, animated on width. */
function setSocBar(fillEl, pct, color) {
  const p = Math.max(0, Math.min(100, Number(pct) || 0));
  fillEl.style.width = p + "%";
  fillEl.style.background = color;
}

// JBD protection-status bits (offset 16 of the 0x03 reply). Bits 0-3 are the normal
// end-of-charge / end-of-discharge voltage cutoffs (shown as info, not an alarm); the rest
// are genuine faults (overcurrent, short, temperature, IC error, lock).
const PROT_BITS = ["Cell OV", "Cell UV", "Pack OV", "Pack UV", "Chg OT", "Chg UT", "Dsg OT", "Dsg UT", "Chg OC", "Dsg OC", "Short", "IC error", "Locked"];
const PROT_SOFT = 0x000F;
function protReasons(mask) {
  const out = [];
  for (let i = 0; i < PROT_BITS.length; i++) if (mask & (1 << i)) out.push(PROT_BITS[i]);
  return out;
}

// Temperatures: show both units, e.g. "25.2°C/77°F" (°F rounded whole).
const cToF = (c) => Math.round((Number(c) * 9) / 5 + 32);
function tempCF(c) {
  if (c === null || c === undefined || c === "" || Number.isNaN(Number(c))) return "—";
  return `${Number(c).toFixed(1)}°C/${cToF(c)}°F`;
}
function tempRangeCF(lo, hi) {
  if (lo === null || lo === undefined || hi === null || hi === undefined) return "—";
  if (Number(lo) === Number(hi)) return `${Number(lo).toFixed(1)}°C/${cToF(lo)}°F`;
  return `${Number(lo).toFixed(1)}–${Number(hi).toFixed(1)}°C / ${cToF(lo)}–${cToF(hi)}°F`;
}

/* Battery detail: each pack with its own SOC, using the same segmented battery-bar graphic. */
function renderBatteryDetail(container, d) {
  if (!d || !d.available || !d.packs || !d.packs.length) {
    container.innerHTML = '<div class="bd-empty">Battery (BMS) — waiting for first BLE read…</div>';
    return;
  }
  // Order the packs by their position in the parallel group (#1 first); unknowns last.
  const cards = [...d.packs].sort((a, b) => (a.parallel ?? 99) - (b.parallel ?? 99)).map((p) => {
    const charging = (p.current || 0) >= 0;
    const fill = p.soc <= 15 ? "#FBBF24" : charging ? "#34D399" : "#FBBF24"; // bar fill (bright reads fine)
    const cls = charging ? "val-pos" : "val-neg"; // theme-aware readable text color
    const soc = Math.max(0, Math.min(100, p.soc));
    const par = p.parallel ? `<span class="bd-pack-par">#${p.parallel}</span> ` : "";
    const reasons = protReasons(p.protection || 0);
    const hard = (p.protection || 0) & ~PROT_SOFT; // any bit outside the normal voltage cutoffs
    const fault = reasons.length
      ? ` · <span class="${hard ? "bd-fault" : "bd-prot"}">${reasons.join(", ")}</span>`
      : "";
    return `<div class="bd-pack">
        <div class="bd-pack-head"><span class="bd-pack-name">${par}${p.name}</span><span class="bd-pack-soc ${cls}">${p.soc}<small>%</small></span></div>
        <div class="socbar"><div class="socbar-fill" style="width:${soc}%;background:${fill}"></div><div class="socbar-seg"></div></div>
        <div class="bd-pack-stats">${p.voltage.toFixed(2)} V · <span class="${cls}">${p.current >= 0 ? "+" : ""}${p.current.toFixed(1)} A</span> · ${tempRangeCF(p.temp_min, p.temp_max)}${fault}</div>
      </div>`;
  }).join("");
  // Cap the single-row layout at 6 columns — past that the cards get too narrow to read.
  const cols = Math.min(d.packs.length, 6);
  container.innerHTML = `<div class="bd-packs" style="--bd-cols:${cols}">${cards}</div>`;
}

/* Power-flow diagram — web port of FlowDiagram (ui/SolarFlow.kt): Solar → Inverter → Battery
   nodes joined by wires, each carrying an animated dot when power flows. The battery wire's
   dot reverses direction when discharging. */
const ICON = {
  sun: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M2 12h2M20 12h2M5 5l1.4 1.4M17.6 17.6L19 19M19 5l-1.4 1.4M6.4 17.6L5 19"/></svg>',
  inverter: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v8"/><path d="M7.3 6.3a7 7 0 1 0 9.4 0"/></svg>',
  battery: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2.5" y="8" width="16" height="9" rx="2"/><path d="M21.5 11.5v2"/><path d="M6 12.5h6"/></svg>',
};

function renderFlow(el) {
  el.innerHTML =
    `<div class="flow">
       <div class="flow-node" data-n="solar">
         <div class="flow-ic" style="color:#22D3EE;background:rgba(34,211,238,0.13)">${ICON.sun}</div>
         <div class="flow-val"><span id="flow_solar">0</span> W</div>
         <div class="flow-lbl">Solar</div>
       </div>
       <div class="flow-wire" data-w="solar"><span class="flow-line"></span><span class="flow-dot" style="background:#22D3EE"></span></div>
       <div class="flow-node" data-n="inv">
         <div class="flow-ic" style="color:#EAF0F6;background:#1C212B">${ICON.inverter}</div>
         <div class="flow-val"><span id="flow_load">0</span> W</div>
         <div class="flow-lbl">Inverter</div>
       </div>
       <div class="flow-wire" data-w="batt"><span class="flow-line"></span><span class="flow-dot"></span></div>
       <div class="flow-node" data-n="batt">
         <div class="flow-ic" style="color:#34D399;background:rgba(52,211,153,0.13)">${ICON.battery}</div>
         <div class="flow-val"><span id="flow_batt">0</span> W</div>
         <div class="flow-lbl">Battery</div>
       </div>
     </div>`;
}

// "Nice" tick step (1/2/5 x 10^n) so axis labels are round numbers.
function niceStep(max, target) {
  const raw = (max || 1) / (target || 4);
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  const n = raw / pow;
  const s = n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10;
  return s * pow;
}

// ---- click-to-show detail popup for energy bars ----
let _ebarPopup = null;
function ebarPopup() {
  if (!_ebarPopup) {
    _ebarPopup = document.createElement("div");
    _ebarPopup.className = "ebar-popup";
    _ebarPopup.style.display = "none";
    document.body.appendChild(_ebarPopup);
  }
  return _ebarPopup;
}
function hideEbarPopup() {
  if (_ebarPopup) _ebarPopup.style.display = "none";
}

// Show the shared popup with arbitrary HTML at a viewport position (used by the power chart).
function showPopupAt(html, clientX, clientY) {
  const p = ebarPopup();
  p.innerHTML = html;
  p.style.display = "block";
  const pr = p.getBoundingClientRect();
  let left = clientX - pr.width / 2;
  left = Math.max(8, Math.min(window.innerWidth - pr.width - 8, left));
  let top = clientY - pr.height - 14;
  if (top < 8) top = clientY + 16;
  p.style.left = Math.round(left) + "px";
  p.style.top = Math.round(top) + "px";
}
function kwhText(v) {
  v = v || 0;
  return v < 1 ? Math.round(v * 1000) + " Wh" : v.toFixed(2) + " kWh";
}
function showEbarPopup(group) {
  const p = ebarPopup();
  let html;
  if (group.dataset.solar !== undefined || group.dataset.grid !== undefined) {
    // Mini-split energy column: solar (DC) vs grid (AC) draw.
    const solar = +group.dataset.solar || 0, grid = +group.dataset.grid || 0;
    html =
      `<div class="pop-title">${group.dataset.title || group.dataset.label}</div>` +
      `<div class="pop-row"><i style="background:#FBBF24"></i>Solar<b>${kwhText(solar)}</b></div>` +
      `<div class="pop-row"><i style="background:#2DD4BF"></i>Grid · AC<b>${kwhText(grid)}</b></div>`;
  } else {
    const pv = +group.dataset.pv || 0, load = +group.dataset.load || 0;
    const charge = +group.dataset.charge || 0, discharge = +group.dataset.discharge || 0;
    html =
      `<div class="pop-title">${group.dataset.title || group.dataset.label}</div>` +
      `<div class="pop-row"><i style="background:#FBBF24"></i>Solar PV<b>${kwhText(pv)}</b></div>` +
      `<div class="pop-row"><i style="background:#9C8CFB"></i>AC Output<b>${kwhText(load)}</b></div>`;
    if (charge || discharge) {
      const net = charge >= discharge ? "+" + kwhText(charge) : "−" + kwhText(discharge);
      html += `<div class="pop-row"><i style="background:#34D399"></i>Battery<b>${net}</b></div>`;
    }
  }
  p.innerHTML = html;
  p.style.display = "block";
  const r = group.getBoundingClientRect(), pr = p.getBoundingClientRect();
  let left = r.left + r.width / 2 - pr.width / 2;
  left = Math.max(8, Math.min(window.innerWidth - pr.width - 8, left));
  let top = r.top - pr.height - 8;
  if (top < 8) top = r.bottom + 8;
  p.style.left = Math.round(left) + "px";
  p.style.top = Math.round(top) + "px";
}
function initEbarPopup(container) {
  container.addEventListener("click", (e) => {
    const g = e.target.closest(".ebar-group");
    if (g) { e.stopPropagation(); showEbarPopup(g); }
  });
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".ebar-group") && !e.target.closest(".ebar-popup")) hideEbarPopup();
  });
  window.addEventListener("resize", hideEbarPopup);
  container.addEventListener("scroll", hideEbarPopup, true);
}

/* Grouped energy bar chart over pre-built calendar slots [{label, pv, load, charge, discharge} in
   kWh], with a fixed kWh scale on the left. Every slot renders a column (empty slots show a flat
   baseline), so the full day/month/year grid is always shown. Click a column for a detail popup. */
function renderEnergyBars(container, slots, visible) {
  hideEbarPopup();
  if (!slots || !slots.length) {
    container.innerHTML = '<div class="ebars-empty">No energy logged yet — give it a bit.</div>';
    return;
  }
  // Which series the legend currently shows (defaults to both); the kWh axis scales to only these.
  const showIn = !visible || visible.pv !== false;
  const showOut = !visible || visible.load !== false;
  let maxV = 0;
  for (const s of slots) {
    if (showIn) maxV = Math.max(maxV, s.pv);
    if (showOut) maxV = Math.max(maxV, s.load);
  }
  const step = niceStep(maxV);
  const axisMax = Math.max(step, Math.ceil(maxV / step) * step);
  const fmtTick = (t) =>
    Math.abs(t) < 1e-9 ? "0" : axisMax < 1 ? t.toFixed(2) : axisMax < 10 ? t.toFixed(1) : String(Math.round(t));

  const ticks = [];
  for (let t = axisMax; t > -1e-9; t -= step) ticks.push(t);
  const axis = `<div class="eaxis" title="kWh">${ticks.map((t) => `<span>${fmtTick(t)}</span>`).join("")}</div>`;

  const bars = slots
    .map((s) => {
      const ih = (s.pv / axisMax) * 100;
      const oh = (s.load / axisMax) * 100;
      const inBar = showIn ? `<div class="ebar ebar-in" style="height:${ih.toFixed(1)}%"></div>` : "";
      const outBar = showOut ? `<div class="ebar ebar-out" style="height:${oh.toFixed(1)}%"></div>` : "";
      return `<div class="ebar-group" data-label="${s.label}" data-title="${s.title || s.label}" data-pv="${s.pv}" data-load="${s.load}" data-charge="${s.charge || 0}" data-discharge="${s.discharge || 0}">
        <div class="ebar-plot">${inBar}${outBar}</div>
        <div class="ebar-x">${s.label}</div>
      </div>`;
    })
    .join("");

  container.innerHTML = axis + `<div class="ebars-track">${bars}</div>`;
}

/* Mini-split energy bars: same grouped-column layout as renderEnergyBars, but the two series are the
   appliance's solar (DC) and grid (AC) draw in kWh. Slots: [{label, title, solar, grid}]. */
function renderMsEnergyBars(container, slots, visible) {
  hideEbarPopup();
  if (!slots || !slots.length) {
    container.innerHTML = '<div class="ebars-empty">No mini-split energy logged yet — give it a bit.</div>';
    return;
  }
  const showSolar = !visible || visible.solar !== false;
  const showGrid = !visible || visible.grid !== false;
  let maxV = 0;
  for (const s of slots) {
    if (showSolar) maxV = Math.max(maxV, s.solar);
    if (showGrid) maxV = Math.max(maxV, s.grid);
  }
  const step = niceStep(maxV);
  const axisMax = Math.max(step, Math.ceil(maxV / step) * step);
  const fmtTick = (t) =>
    Math.abs(t) < 1e-9 ? "0" : axisMax < 1 ? t.toFixed(2) : axisMax < 10 ? t.toFixed(1) : String(Math.round(t));

  const ticks = [];
  for (let t = axisMax; t > -1e-9; t -= step) ticks.push(t);
  const axis = `<div class="eaxis" title="kWh">${ticks.map((t) => `<span>${fmtTick(t)}</span>`).join("")}</div>`;

  const bars = slots
    .map((s) => {
      const sh = (s.solar / axisMax) * 100;
      const gh = (s.grid / axisMax) * 100;
      const solarBar = showSolar ? `<div class="ebar ebar-ms-solar" style="height:${sh.toFixed(1)}%"></div>` : "";
      const gridBar = showGrid ? `<div class="ebar ebar-ms-grid" style="height:${gh.toFixed(1)}%"></div>` : "";
      return `<div class="ebar-group" data-label="${s.label}" data-title="${s.title || s.label}" data-solar="${s.solar}" data-grid="${s.grid}">
        <div class="ebar-plot">${solarBar}${gridBar}</div>
        <div class="ebar-x">${s.label}</div>
      </div>`;
    })
    .join("");

  container.innerHTML = axis + `<div class="ebars-track">${bars}</div>`;
}

function updateFlow(d) {
  const pv = Math.round(d.pv_power || 0);
  const load = Math.round(d.load_total || 0);
  const batt = Math.round(d.battery_power || 0);
  document.getElementById("flow_solar").textContent = pv.toLocaleString();
  document.getElementById("flow_load").textContent = load.toLocaleString();
  document.getElementById("flow_batt").textContent = (batt > 0 ? "+" : "") + batt.toLocaleString();

  const charging = (d.battery_current ?? 0) >= 0;
  const tone = charging ? "#34D399" : "#FBBF24";
  const bic = document.querySelector('.flow-node[data-n="batt"] .flow-ic');
  bic.style.color = tone;
  bic.style.background = charging ? "rgba(52,211,153,0.13)" : "rgba(251,191,36,0.13)";

  const sw = document.querySelector('.flow-wire[data-w="solar"]');
  sw.classList.toggle("active", pv > 0);

  const bw = document.querySelector('.flow-wire[data-w="batt"]');
  bw.classList.toggle("active", Math.abs(batt) > 5);
  bw.classList.toggle("reverse", !charging); // discharging: dot travels battery -> inverter
  bw.querySelector(".flow-dot").style.background = tone;
}
