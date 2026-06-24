"use strict";

// Palette (mirrors the Android app's ui/Color.kt dark theme).
const C = {
  pv: "#FBBF24", load: "#9C8CFB", charge: "#34D399", discharge: "#FBBF24",
  accent: "#22D3EE", accent2: "#4F9CF9", acin1: "#2DD4BF", acin2: "#14B8A6",
  txt3: "#626C7B", line: "#262C37",
};

const $ = (id) => document.getElementById(id);
const fmt = (v, d = 0) =>
  v === null || v === undefined || Number.isNaN(v)
    ? "—"
    : Number(v).toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
const clampPct = (w, max) => Math.max(0, Math.min(100, ((Number(w) || 0) / max) * 100));
const hmJs = (mins) => { mins = Math.max(0, Math.round(mins)); const h = Math.floor(mins / 60), m = mins % 60; return h && m ? `${h}h ${m}m` : h ? `${h}h` : `${m}m`; };

// Gauges (max 4000 W like the app; PV cyan->blue, Load purple).
const pvGauge = new Gauge($("pvGauge"), { id: "pv", max: 4000, unit: "W", sub: "total input", c1: C.accent, c2: C.accent2 });
const loadGauge = new Gauge($("loadGauge"), { id: "load", max: 4000, unit: "W", sub: "real power · L1+L2", c1: C.load, c2: C.load });
const acinGauge = new Gauge($("acinGauge"), { id: "acin", max: 4000, unit: "W", sub: "grid / generator", c1: C.acin1, c2: C.acin2 });
renderFlow($("flow"));

let chart = null;
let batteryChart = null;
let activeWin = 86400;
let activeBattWin = 86400;
let activePeriod = "hour";
let energyView = null; // { period, rows } of the currently displayed energy data, for CSV export
let bmsBank = null;    // latest BMS bank summary (for real battery temp in the main panel)
let lastCurrent = null; // last /api/current payload, so the W/A toggle can re-render instantly

function setPill(el, text, tone) {
  el.textContent = text;
  el.className = "pill" + (tone ? " " + tone : "");
}

// A per-string/leg readout. In amps mode the bold value is the current (A) with W·V demoted to
// the sub; otherwise it's the power (W) with V·A in the sub. Matches the wheel's own W/A toggle.
function leg(prefix, w, v, a, wattMax, ampMax, amps) {
  if (amps) {
    $(prefix + "_w").textContent = fmt(a, 1);
    $(prefix + "_u").textContent = "A";
    $(prefix + "_bar").style.width = clampPct(a, ampMax) + "%";
    $(prefix + "_sub").textContent = `${fmt(w, 0)} W · ${fmt(v, 1)} V`;
  } else {
    $(prefix + "_w").textContent = fmt(w, 0);
    $(prefix + "_u").textContent = "W";
    $(prefix + "_bar").style.width = clampPct(w, wattMax) + "%";
    $(prefix + "_sub").textContent = `${fmt(v, 1)} V · ${fmt(a, 1)} A`;
  }
}

function updateTiles(d) {
  if (!d || !d.available) {
    $("status").textContent = "waiting for first sample…";
    $("liveDot").className = "dot stale";
    return;
  }

  lastCurrent = d; // let the per-tile W/A toggles re-render instantly

  // Solar PV wheel + strings — unit per its own tile toggle (W or A)
  const pvA = gaugeUnit("pv") === "A";
  if (pvA) { pvGauge.setUnit("A", 20, "total current", 1); pvGauge.set((d.pv1_current || 0) + (d.pv2_current || 0)); }
  else { pvGauge.setUnit("W", 4000, "total input", 0); pvGauge.set(d.pv_power); }
  const pvOn = (d.pv_power ?? 0) > 10;
  setPill($("pv_pill"), pvOn ? "Powering" : "Idle", pvOn ? "accent" : "");
  leg("pv1", d.pv1_power, d.pv1_voltage, d.pv1_current, 2000, 6, pvA);
  leg("pv2", d.pv2_power, d.pv2_voltage, d.pv2_current, 2000, 6, pvA);

  // Load wheel + legs
  const loadA = gaugeUnit("load") === "A";
  if (loadA) { loadGauge.setUnit("A", 40, "current · L1+L2", 1); loadGauge.set((d.load_current || 0) + (d.load_l2_current || 0)); }
  else { loadGauge.setUnit("W", 4000, "real power · L1+L2", 0); loadGauge.set(d.load_total); }
  setPill($("load_pill"), `${fmt(d.output_frequency, 2)} Hz`, "");
  leg("l1", d.load_power, d.output_voltage, d.load_current, 2000, 16, loadA);
  leg("l2", d.load_l2_power, d.output_l2_voltage, d.load_l2_current, 2000, 16, loadA);

  // AC Input (grid / generator). grid_power/current aren't decoded registers yet, so the wheel
  // reads 0 until one is mapped; the L1/L2 voltage + frequency below are live.
  if (gaugeUnit("acin") === "A") { acinGauge.setUnit("A", 40, "grid / generator", 1); acinGauge.set(d.grid_current); }
  else { acinGauge.setUnit("W", 4000, "grid / generator", 0); acinGauge.set(d.grid_power); }
  const gridLive = (d.grid_voltage ?? 0) > 50;
  setPill($("acin_pill"), gridLive ? "Live input" : "No input", gridLive ? "accent" : "");
  $("acin1_v").textContent = fmt(d.grid_voltage, 1);
  $("acin1_bar").style.width = clampPct(d.grid_voltage, 260) + "%";
  $("acin1_sub").textContent = `${fmt(d.grid_frequency, 2)} Hz`;
  $("acin2_v").textContent = fmt(d.grid_l2_voltage, 1);
  $("acin2_bar").style.width = clampPct(d.grid_l2_voltage, 260) + "%";
  $("acin2_sub").textContent = gridLive ? "input" : "off-grid";

  // Power-flow diagram
  updateFlow(d);

  // Battery
  const charging = (d.battery_current ?? 0) >= 0;
  const tone = charging ? C.charge : C.discharge;
  $("battery_soc").textContent = fmt(d.battery_soc, 0);
  const w = d.battery_power;
  const watts = $("batt_watts");
  watts.textContent = (w != null && w > 0 ? "+" : "") + fmt(w, 0) + " W";
  watts.className = "batt-watts " + (charging ? "val-pos" : "val-neg");
  setPill($("batt_pill"), charging ? "Charging" : "Discharging", charging ? "green" : "amber");
  setSocBar($("socbar_fill"), d.battery_soc, (d.battery_soc ?? 100) <= 15 ? C.discharge : tone);
  const etaEl = $("batt_eta");
  if (d.battery_eta_minutes == null) {
    etaEl.textContent = "holding · idle";
    etaEl.className = "batt-eta val-muted";
  } else if (d.battery_eta_kind === "full") {
    etaEl.innerHTML = `▲ ${hmJs(d.battery_eta_minutes)} to full`;
    etaEl.className = "batt-eta val-pos";
  } else {
    etaEl.innerHTML = `▼ ${hmJs(d.battery_eta_minutes)} to empty`;
    etaEl.className = "batt-eta val-neg";
  }
  const battTemp = bmsBank ? bmsBank.temp_max : d.battery_temp; // BMS temp is real; inverter reads 0
  $("batt_v").textContent = fmt(d.battery_voltage, 2);
  $("batt_a").textContent = (d.battery_current != null && d.battery_current >= 0 ? "+" : "") + fmt(d.battery_current, 1);
  $("batt_t").textContent = fmt(battTemp, 1);

  // Secondary tiles — temps in both °C and °F
  $("dc_temp").textContent = tempCF(d.dc_temp);
  $("temp_sub").textContent = `AC ${tempCF(d.ac_temp)} · batt ${tempCF(battTemp)}`;
  $("machine_state").textContent = d.machine_state ?? "—";

  const tile = $("fault_tile");
  if (d.faults && d.faults.length) {
    tile.classList.add("has-fault");
    $("fault_value").textContent = `${d.faults.length} FAULT${d.faults.length > 1 ? "S" : ""}`;
    $("fault_sub").textContent = d.faults.map((f) => `F${String(f.code).padStart(2, "0")} ${f.text}`).join(" · ");
  } else {
    tile.classList.remove("has-fault");
    $("fault_value").textContent = "OK";
    $("fault_value").className = "tile-value ok";
    $("fault_sub").textContent = "No active faults";
  }

  // freshness
  const age = Math.floor(Date.now() / 1000) - d.ts;
  const dot = $("liveDot");
  if (age <= 30) { dot.className = "dot live"; $("status").textContent = "live · just now"; }
  else if (age <= 120) { dot.className = "dot live"; $("status").textContent = `live · ${age}s ago`; }
  else { dot.className = "dot stale"; $("status").textContent = `stale · ${Math.floor(age / 60)}m ago`; }
}

async function loadCurrent() {
  try {
    const r = await fetch("api/current", { cache: "no-store" });
    updateTiles(await r.json());
  } catch (e) {
    $("liveDot").className = "dot down";
    $("status").textContent = "server unreachable";
  }
}

async function loadBattery() {
  try {
    const d = await (await fetch("api/battery", { cache: "no-store" })).json();
    bmsBank = d.available ? d.bank : null;
    renderBatteryDetail($("batteryDetail"), d);
  } catch (e) { /* leave previous render */ }
}

// ---- history chart --------------------------------------------------------

const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

// Clickable-legend visibility, persisted per chart+series so a hidden line/bar stays hidden
// across reloads (mirrors how the W/A units, theme and panel toggles persist). Default = shown.
const seriesVisKey = (chartName, key) => `solar.series.${chartName}.${key}`;
const seriesVisible = (chartName, key) => localStorage.getItem(seriesVisKey(chartName, key)) !== "0";
const setSeriesVisible = (chartName, key, vis) => localStorage.setItem(seriesVisKey(chartName, key), vis ? "1" : "0");

function chartOpts(width) {
  // Read neutrals from CSS so the canvas axes/grid follow the active light/dark theme.
  const axisStroke = cssVar("--txt3") || C.txt3;
  const gridStroke = cssVar("--line") || C.line;
  const axis = { stroke: axisStroke, grid: { stroke: gridStroke, width: 1 }, ticks: { stroke: gridStroke } };
  return {
    width, height: 300, legend: { show: false },
    cursor: { y: false, points: { size: 6 } },
    scales: { x: { time: true } },
    series: [
      {},
      { label: "Solar", stroke: C.pv, width: 2, fill: "rgba(251,191,36,0.10)", spanGaps: false, show: seriesVisible("history", "pv") },
      { label: "Load", stroke: C.load, width: 2, spanGaps: false, show: seriesVisible("history", "load") },
      { label: "Battery", stroke: C.charge, width: 2, spanGaps: false, show: seriesVisible("history", "battery") },
    ],
    axes: [
      { ...axis },
      { ...axis, size: 52, values: (u, vals) => vals.map((v) => (Math.abs(v) >= 1000 ? v / 1000 + "k" : v)) },
    ],
  };
}

// Series shown in the Power-history chart, in uPlot series order; `idx` is the uPlot series index.
const HISTORY_SERIES = [
  { key: "pv", idx: 1, label: "Solar PV", color: C.pv },
  { key: "load", idx: 2, label: "Load", color: C.load },
  { key: "battery", idx: 3, label: "Battery", color: C.charge },
];

function renderLegend() {
  $("legend").innerHTML = HISTORY_SERIES
    .map((s) => `<span class="item${seriesVisible("history", s.key) ? "" : " off"}" data-key="${s.key}" title="Show/hide ${s.label}"><span class="swatch" style="background:${s.color}"></span>${s.label} (W)</span>`)
    .join("");
}

// Make the Power-history legend clickable: each item shows/hides its line, and uPlot rescales the
// y-axis to whatever is left. Delegated on the container, so it survives legend re-renders.
function initLegend() {
  $("legend").addEventListener("click", (e) => {
    const item = e.target.closest(".item");
    if (!item) return;
    const s = HISTORY_SERIES.find((x) => x.key === item.dataset.key);
    if (!s) return;
    const next = !seriesVisible("history", s.key);
    setSeriesVisible("history", s.key, next);
    item.classList.toggle("off", !next);
    if (chart) chart.setSeries(s.idx, { show: next });
  });
}

async function loadHistory(win) {
  const now = Math.floor(Date.now() / 1000);
  const url = `api/history?fields=pv_power,load_total,battery_power&start=${now - win}&max_points=600`;
  let payload;
  try { payload = await (await fetch(url, { cache: "no-store" })).json(); } catch (e) { return; }

  const data = [
    payload.ts,
    payload.series.pv_power || [],
    payload.series.load_total || [],
    payload.series.battery_power || [],
  ];
  hideEbarPopup();
  const width = $("chart").clientWidth || 800;
  if (chart) {
    chart.setData(data);
    chart.setSize({ width, height: 300 });
  } else {
    chart = new uPlot(chartOpts(width), data, $("chart"));
    chart.over.addEventListener("click", onChartClick);
  }
}

// Click the Power history to pin a popup with the values at that moment.
function onChartClick(e) {
  if (!chart) return;
  const idx = chart.cursor.idx;
  if (idx == null) return;
  e.stopPropagation();
  const ts = chart.data[0][idx];
  const w = (v) => (v == null ? "—" : Math.round(v).toLocaleString() + " W");
  const batt = chart.data[3][idx];
  const bw = batt == null ? "—" : (batt > 0 ? "+" : "") + Math.round(batt).toLocaleString() + " W";
  const when = new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const html =
    `<div class="pop-title">${when}</div>` +
    `<div class="pop-row"><i style="background:${C.pv}"></i>Solar PV<b>${w(chart.data[1][idx])}</b></div>` +
    `<div class="pop-row"><i style="background:${C.load}"></i>Load<b>${w(chart.data[2][idx])}</b></div>` +
    `<div class="pop-row"><i style="background:${C.charge}"></i>Battery<b>${bw}</b></div>`;
  showPopupAt(html, e.clientX, e.clientY);
}

// ---- battery history chart (state of charge, %) ---------------------------

function batteryChartOpts(width) {
  const axisStroke = cssVar("--txt3") || C.txt3;
  const gridStroke = cssVar("--line") || C.line;
  const axis = { stroke: axisStroke, grid: { stroke: gridStroke, width: 1 }, ticks: { stroke: gridStroke } };
  return {
    width, height: 300, legend: { show: false },
    cursor: { y: false, points: { size: 6 } },
    scales: { x: { time: true }, y: { range: [0, 100] } }, // charge is always 0–100%
    series: [
      {},
      { label: "Charge", stroke: C.charge, width: 2, fill: "rgba(52,211,153,0.10)", spanGaps: false },
    ],
    axes: [
      { ...axis },
      { ...axis, size: 44, values: (u, vals) => vals.map((v) => v + "%") },
    ],
  };
}

function renderBatteryLegend() {
  $("batteryLegend").innerHTML =
    `<span class="item"><span class="swatch" style="background:${C.charge}"></span>Battery charge (%)</span>`;
}

async function loadBatteryHistory(win) {
  const now = Math.floor(Date.now() / 1000);
  const url = `api/history?fields=battery_soc&start=${now - win}&max_points=600`;
  let payload;
  try { payload = await (await fetch(url, { cache: "no-store" })).json(); } catch (e) { return; }

  const data = [payload.ts, payload.series.battery_soc || []];
  updateBatteryStats(data[0], data[1], win);
  const width = $("batteryChart").clientWidth || 800;
  if (batteryChart) {
    batteryChart.setData(data);
    batteryChart.setSize({ width, height: 300 });
  } else {
    batteryChart = new uPlot(batteryChartOpts(width), data, $("batteryChart"));
    batteryChart.over.addEventListener("click", onBatteryChartClick);
  }
}

// When a max/min occurred. Windows longer than a day include the date, since "@ 3:45 PM" alone
// would be ambiguous across days.
function atLabel(ts, win) {
  const d = new Date(ts * 1000);
  const t = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  return win > 86400 ? `${d.toLocaleDateString([], { month: "short", day: "numeric" })} ${t}` : t;
}

// Header Max/Min charge for the window currently shown, each tagged with when it happened.
function updateBatteryStats(ts, soc, win) {
  let maxV = -Infinity, minV = Infinity, maxTs = null, minTs = null;
  for (let i = 0; i < soc.length; i++) {
    const v = soc[i];
    if (v == null) continue;
    if (v > maxV) { maxV = v; maxTs = ts[i]; }
    if (v < minV) { minV = v; minTs = ts[i]; }
  }
  if (maxTs == null) { $("batt_max").textContent = "—"; $("batt_min").textContent = "—"; return; }
  $("batt_max").textContent = `${Math.round(maxV)}% @ ${atLabel(maxTs, win)}`;
  $("batt_min").textContent = `${Math.round(minV)}% @ ${atLabel(minTs, win)}`;
}

// Click the Battery history to pin a popup with the charge at that moment.
function onBatteryChartClick(e) {
  if (!batteryChart) return;
  const idx = batteryChart.cursor.idx;
  if (idx == null) return;
  e.stopPropagation();
  const ts = batteryChart.data[0][idx];
  const soc = batteryChart.data[1][idx];
  const when = new Date(ts * 1000).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  const html =
    `<div class="pop-title">${when}</div>` +
    `<div class="pop-row"><i style="background:${C.charge}"></i>Charge<b>${soc == null ? "—" : Math.round(soc) + "%"}</b></div>`;
  showPopupAt(html, e.clientX, e.clientY);
}

// ---- snapshot (camera button) ---------------------------------------------

let _toastTimer = null;
function showToast(msg, tone) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (tone ? " " + tone : "");
  t.hidden = false;
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { t.hidden = true; }, 5000);
}

async function takeSnapshot() {
  const btn = $("snapBtn");
  btn.disabled = true;
  btn.classList.add("busy");
  try {
    const j = await (await fetch("api/snapshot", { method: "POST" })).json();
    if (j.ok) showToast(`Snapshot saved · ${j.filename}`, "ok");
    else showToast(j.error || "Snapshot failed", "err");
  } catch (e) {
    showToast("Snapshot failed — dashboard unreachable", "err");
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
  }
}

// ---- lifetime + energy trends ---------------------------------------------

// Top strip: today's running totals (the day's bucket from the daily roll-up).
async function loadToday() {
  try {
    const now = new Date();
    const key = `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
    const start = Math.floor(new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000);
    const payload = await (await fetch(`api/energy?period=day&start=${start}`, { cache: "no-store" })).json();
    const b = (payload.buckets || []).find((x) => x.bucket === key) || {};
    $("today_in").textContent = fmt(b.pv_kwh, 1);
    $("today_out").textContent = fmt(b.load_kwh, 1);
    $("today_charge").textContent = fmt(b.charge_kwh, 1);
    $("today_discharge").textContent = fmt(b.discharge_kwh, 1);
  } catch (e) { /* leave dashes */ }
}

// "(peak 2.39 kW)" from an all-time peak in watts; blank until there's a reading.
const peakKw = (w) => (w == null || w <= 0 ? "" : `(peak ${fmt(w / 1000, 2)} kW)`);

// All-time totals, shown compactly in the Power history header. The Solar/Load peaks are all-time
// (independent of the chart's time range), so they don't move when the range buttons change.
async function loadLifetime() {
  try {
    const lt = await (await fetch("api/energy/lifetime", { cache: "no-store" })).json();
    $("life_in").textContent = fmt(lt.pv_kwh, 1);
    $("life_out").textContent = fmt(lt.load_kwh, 1);
    $("life_charge").textContent = fmt(lt.charge_kwh, 1);
    $("life_discharge").textContent = fmt(lt.discharge_kwh, 1);
    $("life_in_peak").textContent = peakKw(lt.pv_peak_w);
    $("life_out_peak").textContent = peakKw(lt.load_peak_w);
    if (lt.since) $("lifeInline").title = "Lifetime since " + new Date(lt.since * 1000).toLocaleDateString();
  } catch (e) { /* leave dashes */ }
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const FULL_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
const pad2 = (n) => String(n).padStart(2, "0");
const hour12 = (h) => (h % 12 === 0 ? 12 : h % 12) + (h < 12 ? " AM" : " PM"); // "9 AM", "12 PM"
function hourRange(h) {
  const lbl = (x) => ({ hr: x % 12 === 0 ? 12 : x % 12, ap: x < 12 ? "AM" : "PM" });
  const a = lbl(h), b = lbl((h + 1) % 24);
  return a.ap === b.ap ? `${a.hr}-${b.hr} ${a.ap}` : `${a.hr} ${a.ap}-${b.hr} ${b.ap}`; // "9-10 AM", "11 AM-12 PM"
}

// Build the full set of calendar slots for the view, each with the SQLite-localtime bucket key
// it should match: Daily=24 hours of today, Monthly=days of this month, Yearly=12 months this year.
function genSlots(period) {
  const now = new Date();
  const slots = [];
  if (period === "hour") {
    const base = new Date(now.getFullYear(), now.getMonth(), now.getDate());
    for (let h = 0; h < 24; h++) {
      const d = new Date(base.getTime() + h * 3600000);
      slots.push({ key: `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:00`, label: hour12(d.getHours()), title: hourRange(d.getHours()), start_ts: Math.floor(d.getTime() / 1000) });
    }
  } else if (period === "day") {
    const y = now.getFullYear(), m = now.getMonth();
    const days = new Date(y, m + 1, 0).getDate();
    for (let day = 1; day <= days; day++) {
      const d = new Date(y, m, day);
      slots.push({ key: `${y}-${pad2(m + 1)}-${pad2(day)}`, label: String(day), title: `${MONTHS[m]} ${day}`, start_ts: Math.floor(d.getTime() / 1000) });
    }
  } else {
    const y = now.getFullYear();
    for (let mo = 0; mo < 12; mo++) {
      const d = new Date(y, mo, 1);
      slots.push({ key: `${y}-${pad2(mo + 1)}`, label: MONTHS[mo], title: `${FULL_MONTHS[mo]} ${y}`, start_ts: Math.floor(d.getTime() / 1000) });
    }
  }
  return slots;
}

async function loadEnergy(period) {
  const slots = genSlots(period);
  if (!slots.length) return;
  let payload;
  try { payload = await (await fetch(`api/energy?period=${period}&start=${slots[0].start_ts}`, { cache: "no-store" })).json(); }
  catch (e) { return; }
  const byKey = {};
  for (const b of payload.buckets || []) byKey[b.bucket] = b;
  let tin = 0, tout = 0, maxIn = 0, maxOut = 0;
  const merged = slots.map((s) => {
    const d = byKey[s.key];
    const pv = d ? d.pv_kwh : 0, load = d ? d.load_kwh : 0;
    tin += pv; tout += load;
    if (pv > maxIn) maxIn = pv;
    if (load > maxOut) maxOut = load;
    return { key: s.key, label: s.label, title: s.title, pv, load, charge: d ? d.charge_kwh : 0, discharge: d ? d.discharge_kwh : 0 };
  });
  energyView = { period, rows: merged };
  // Totals + peak bucket for the selected period (the peak is the single highest hour/day/month).
  $("e_in").textContent = fmt(tin, 1);
  $("e_out").textContent = fmt(tout, 1);
  $("e_max_in").textContent = fmt(maxIn, 2);
  $("e_max_out").textContent = fmt(maxOut, 2);
  renderEnergyBars($("ebars"), merged, energyVisible());
}

function csvCell(s) {
  s = String(s);
  return /[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s;
}

function exportEnergyCSV() {
  const rows = (energyView && energyView.rows) || [];
  const header = ["bucket", "label", "solar_kwh", "load_kwh", "battery_charged_kwh", "battery_discharged_kwh"];
  const lines = [header.join(",")];
  for (const r of rows) {
    lines.push([
      csvCell(r.key), csvCell(r.title || r.label),
      r.pv.toFixed(3), r.load.toFixed(3), (r.charge || 0).toFixed(3), (r.discharge || 0).toFixed(3),
    ].join(","));
  }
  const blob = new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `solar-energy-${energyView ? energyView.period : "data"}.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Series in the Energy-trends bar chart; the bottom legend toggles each one.
const ENERGY_SERIES = [
  { key: "pv", label: "Input · Solar (kWh)", color: "#FBBF24" },
  { key: "load", label: "Output · Load (kWh)", color: "#9C8CFB" },
];
const energyVisible = () => ({ pv: seriesVisible("energy", "pv"), load: seriesVisible("energy", "load") });

function renderEnergyLegend() {
  $("elegend").innerHTML = ENERGY_SERIES
    .map((s) => `<span class="item${seriesVisible("energy", s.key) ? "" : " off"}" data-key="${s.key}" title="Show/hide ${s.label}"><span class="swatch" style="background:${s.color}"></span>${s.label}</span>`)
    .join("");
}

function initERanges() {
  renderEnergyLegend();
  // Clickable legend: toggle a bar series, then re-render so the kWh axis rescales to what's shown.
  $("elegend").addEventListener("click", (e) => {
    const item = e.target.closest(".item");
    if (!item) return;
    const key = item.dataset.key;
    const next = !seriesVisible("energy", key);
    setSeriesVisible("energy", key, next);
    item.classList.toggle("off", !next);
    if (energyView) renderEnergyBars($("ebars"), energyView.rows, energyVisible());
  });
  $("eranges").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    document.querySelectorAll("#eranges button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activePeriod = btn.dataset.period;
    loadEnergy(activePeriod);
  });
}

// ---- settings menu --------------------------------------------------------

const SETTING = { acin: "solar.showAcIn", battery: "solar.showBattery", energy: "solar.showEnergy", history: "solar.showHistory", batteryHistory: "solar.showBatteryHistory" };
const getBool = (k, def) => { const v = localStorage.getItem(k); return v === null ? def : v === "1"; };

function applySettings() {
  const acin = getBool(SETTING.acin, true);
  const battery = getBool(SETTING.battery, true);
  const energy = getBool(SETTING.energy, true);
  const history = getBool(SETTING.history, true);
  const batteryHistory = getBool(SETTING.batteryHistory, true);
  document.body.classList.toggle("hide-acin", !acin);
  document.body.classList.toggle("hide-battery", !battery);
  document.body.classList.toggle("hide-energy", !energy);
  document.body.classList.toggle("hide-history", !history);
  document.body.classList.toggle("hide-battery-history", !batteryHistory);
  $("toggleAcIn").checked = acin;
  $("toggleBattery").checked = battery;
  $("toggleEnergy").checked = energy;
  $("toggleHistory").checked = history;
  $("toggleBatteryHistory").checked = batteryHistory;
}

function initSettings() {
  applySettings();
  $("gearBtn").addEventListener("click", (e) => {
    e.stopPropagation();
    $("settingsMenu").hidden = !$("settingsMenu").hidden;
  });
  document.addEventListener("click", (e) => { if (!e.target.closest(".settings")) $("settingsMenu").hidden = true; });
  const bind = (key, el) => $(el).addEventListener("change", (e) => { localStorage.setItem(key, e.target.checked ? "1" : "0"); applySettings(); });
  bind(SETTING.acin, "toggleAcIn");
  bind(SETTING.battery, "toggleBattery");
  bind(SETTING.energy, "toggleEnergy");
  bind(SETTING.history, "toggleHistory");
  bind(SETTING.batteryHistory, "toggleBatteryHistory");
}

// ---- per-tile W/A unit toggles (Solar PV / AC Output / AC Input) -----------

const gaugeUnit = (key) => (localStorage.getItem("solar.unit." + key) === "A" ? "A" : "W");

function initUnitToggles() {
  document.querySelectorAll(".unit-toggle").forEach((tog) => {
    const key = tog.dataset.gauge;
    const sync = (u) => tog.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.u === u));
    sync(gaugeUnit(key));
    tog.addEventListener("click", (e) => {
      const btn = e.target.closest("button");
      if (!btn) return;
      localStorage.setItem("solar.unit." + key, btn.dataset.u);
      sync(btn.dataset.u);
      if (lastCurrent) updateTiles(lastCurrent); // re-render just from the cached reading
    });
  });
}

// ---- theme (light / dark, persisted) --------------------------------------

const THEME_KEY = "solar.theme";

function applyTheme(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "light" ? "#F3F5F8" : "#0E1116");
}

function initTheme() {
  applyTheme(localStorage.getItem(THEME_KEY) || "dark");
  $("themeBtn").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "light" ? "dark" : "light";
    localStorage.setItem(THEME_KEY, next);
    applyTheme(next);
    // uPlot paints axes/grid onto a canvas, so rebuild it to pick up the new theme colors.
    if (chart) { chart.destroy(); chart = null; }
    if (batteryChart) { batteryChart.destroy(); batteryChart = null; }
    loadHistory(activeWin);
    loadBatteryHistory(activeBattWin);
  });
}

function initRanges() {
  $("ranges").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    document.querySelectorAll("#ranges button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeWin = Number(btn.dataset.win);
    loadHistory(activeWin);
  });
}

function initBatteryRanges() {
  $("bRanges").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    document.querySelectorAll("#bRanges button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeBattWin = Number(btn.dataset.win);
    loadBatteryHistory(activeBattWin);
  });
}

window.addEventListener("resize", () => {
  if (chart) chart.setSize({ width: $("chart").clientWidth || 800, height: 300 });
  if (batteryChart) batteryChart.setSize({ width: $("batteryChart").clientWidth || 800, height: 300 });
});

initTheme();
renderLegend();
initLegend();
renderBatteryLegend();
initRanges();
initBatteryRanges();
initERanges();
initEbarPopup($("ebars"));
$("exportBtn").addEventListener("click", exportEnergyCSV);
$("snapBtn").addEventListener("click", takeSnapshot);
initSettings();
initUnitToggles();
loadBattery();
loadCurrent();
loadHistory(activeWin);
loadBatteryHistory(activeBattWin);
loadToday();
loadLifetime();
loadEnergy(activePeriod);
setInterval(loadCurrent, 5000);
setInterval(loadBattery, 20000);
setInterval(() => loadHistory(activeWin), 30000);
setInterval(() => loadBatteryHistory(activeBattWin), 30000);
setInterval(loadToday, 60000);
setInterval(loadLifetime, 60000);
setInterval(() => loadEnergy(activePeriod), 60000);
