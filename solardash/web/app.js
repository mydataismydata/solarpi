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
// Mini-split: one dial, two sources drawn at once — solar (amber, DC) + grid (teal, AC).
const msGauge = new DualGauge($("msGauge"), { id: "ms", max: 3000, unit: "W", sub: "solar + grid", solar: C.pv, grid: C.acin1 });
renderFlow($("flow"));

let chart = null;
let batteryChart = null;
let activeWin = 86400;
let activeBattWin = 86400;
let activePeriod = "hour";
let energyView = null; // { period, rows } of the currently displayed energy data, for CSV export
let msActivePeriod = "hour"; // selected range for the mini-split energy chart
let msEnergyView = null;     // { period, rows } currently displayed for the mini-split energy chart
let bmsBank = null;    // latest BMS bank summary (for real battery temp in the main panel)
let lastCurrent = null; // last /api/current payload, so the W/A toggle can re-render instantly
let msPowerOn = false;        // latest mini-split on/off state
let msCooldownRemaining = 0;  // seconds left in the server-enforced power lockout
let msPowerBusy = false;      // a power command is in flight
let msApplianceAvailable = false;
let msModeBusy = false;       // a mode change is in flight
let msCurrentMode = null;     // the unit's current mode (for the heat<->cool gate)
let msPendingMode = null;     // a mode the user picked but hasn't applied yet (shows an Apply button)
let msModeCooldownRemaining = 0; // seconds left in the cool/dry<->heat lockout
let invControlEnabled = false; // server exposes AC-output control (SOLAR_INVERTER_CONTROL)
let invOutputOn = null;        // latest AC-output state (true/false/null), inferred from output voltage
let invOutputBusy = false;     // an output on/off command is in flight

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
  renderAcOutBtn(d);

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
  const state = d.machine_state ?? "—";
  const tile = $("fault_tile");
  if (d.faults && d.faults.length) {
    tile.classList.add("has-fault");
    $("fault_value").textContent = `${d.faults.length} FAULT${d.faults.length > 1 ? "S" : ""}`;
    $("fault_sub").textContent = `State ${state} · ` + d.faults.map((f) => `F${String(f.code).padStart(2, "0")} ${f.text}`).join(" · ");
  } else {
    tile.classList.remove("has-fault");
    $("fault_value").textContent = "OK";
    $("fault_value").className = "tile-value ok";
    $("fault_sub").textContent = `Machine state ${state} · no active faults`;
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

// ---- mini-split (appliance) -----------------------------------------------

// The mode control's labels (a subset of the unit's DP-4 enum), shown Dry / Cold / Heat.
const MS_MODE_LABEL = { wet: "Dry", cold: "Cold", hot: "Heat", auto: "Auto", wind: "Fan", fan: "Fan" };
// Prettify a raw Tuya enum ("fan_only" -> "Fan only", "cooling" -> "Cooling").
const prettyMs = (s) => (!s ? "" : String(s).split(/[_\s]+/).filter(Boolean).map((w) => w[0].toUpperCase() + w.slice(1)).join(" "));
const msModeLabel = (m) => MS_MODE_LABEL[String(m || "").toLowerCase()] || prettyMs(m) || "—";

const MS_MAX = 3000; // dial + leg-bar scale (watts) for the mini-split's total draw

const mmss = (s) => { s = Math.max(0, Math.round(s)); return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0"); };

// Single source of truth for the power button's look: on/off, in-flight, or locked (server cooldown).
function syncPowerBtn() {
  const btn = $("ms_power_btn"), lbl = $("ms_cooldown");
  const locked = msCooldownRemaining > 0;
  btn.classList.toggle("on", msPowerOn && !locked);
  btn.classList.toggle("busy", msPowerBusy);
  btn.disabled = !msApplianceAvailable || msPowerBusy || locked;
  if (locked) {
    lbl.textContent = mmss(msCooldownRemaining);
    lbl.hidden = false;
    btn.title = `Locked ${mmss(msCooldownRemaining)} — compressor protection`;
  } else {
    lbl.hidden = true;
    btn.title = "Turn the mini-split on/off";
  }
}

const isCooling = (m) => m === "cold" || m === "wet";

// Render the mode control. Picking a mode only stages it (msPendingMode); the pill then shows the
// pending choice with a dashed ring and an Apply button appears — nothing is sent until Apply. The
// pill shows the mode even when the unit is off, so you can pre-set what it'll run when next turned
// on. During the reverse-gate we disable ONLY the options that cross the heat/cool boundary
// (cool/dry <-> heat) relative to the unit's actual mode; cool<->dry stays free.
function renderModes() {
  const cur = msCurrentMode ? String(msCurrentMode).toLowerCase() : null;
  const pend = msPendingMode ? String(msPendingMode).toLowerCase() : null;
  const sel = pend || cur;                 // what the control currently shows
  const dirty = !!pend && pend !== cur;    // a staged change waiting to be applied

  const btn = $("ms_mode_btn");
  $("ms_mode_label").textContent = msApplianceAvailable ? msModeLabel(sel) : "—";
  const tone = (sel === "cold" || sel === "hot" || sel === "wet") ? sel : "";
  btn.className = "mode-pill" + (tone ? " " + tone : "") + (dirty ? " pending" : "");
  btn.disabled = !msApplianceAvailable;

  const apply = $("ms_mode_apply"), cancel = $("ms_mode_cancel");
  apply.hidden = cancel.hidden = !(dirty && msApplianceAvailable);
  apply.disabled = cancel.disabled = msModeBusy;

  const locked = msModeCooldownRemaining > 0;
  $("ms_mode_menu").querySelectorAll("button").forEach((b) => {
    const bm = b.dataset.mode;
    b.classList.toggle("active", bm === sel);
    const crosses = locked && cur &&
      ((isCooling(cur) && bm === "hot") || (cur === "hot" && isCooling(bm)));
    b.disabled = !msApplianceAvailable || msModeBusy || crosses;
    b.title = crosses ? `Heat/cool switch locked ${mmss(msModeCooldownRemaining)} — compressor protection` : "";
  });
}

function closeModeMenu() {
  $("ms_mode_select").classList.remove("open");
  $("ms_mode_menu").hidden = true;
  $("ms_mode_btn").setAttribute("aria-expanded", "false");
}

function toggleModeMenu() {
  if ($("ms_mode_btn").disabled) return;
  const opening = $("ms_mode_menu").hidden;
  $("ms_mode_select").classList.toggle("open", opening);
  $("ms_mode_menu").hidden = !opening;
  $("ms_mode_btn").setAttribute("aria-expanded", String(opening));
}

// Stage a mode (don't send it). Picking the mode the unit is already in clears the pending change.
function selectMode(mode) {
  const cur = msCurrentMode ? String(msCurrentMode).toLowerCase() : null;
  msPendingMode = (mode === cur) ? null : mode;
  renderModes();
}

// Actually send the staged mode change (the Apply button).
function applyMode() {
  if (!msPendingMode || msModeBusy) return;
  setMsMode(msPendingMode);
}

// Discard the staged change without sending anything (the Cancel button).
function cancelMode() {
  if (msModeBusy) return;
  msPendingMode = null;
  renderModes();
}

function updateAppliance(d) {
  if (!d || !d.available) {
    setPill($("ms_pill"), "Off", "");
    msGauge.set(0, 0);
    msGauge.setSub("solar + grid");
    for (const s of ["solar", "grid"]) {
      $("ms_" + s + "_w").textContent = "—";
      $("ms_" + s + "_bar").style.width = "0%";
      $("ms_" + s + "_sub").textContent = "—";
    }
    $("ms_tile_temp").textContent = "—";
    $("ms_tile_sub").textContent = d ? "not configured" : "waiting…";
    msApplianceAvailable = false;
    msCooldownRemaining = 0;
    syncPowerBtn();
    msCurrentMode = null;
    msPendingMode = null;
    msModeCooldownRemaining = 0;
    renderModes();
    return;
  }
  const solar = Math.max(0, d.solar_power ?? 0);
  const grid = Math.max(0, d.grid_power ?? 0);
  const total = solar + grid;
  msGauge.set(solar, grid);
  const sPct = d.solar_percent ?? (total > 0 ? Math.round((solar / total) * 100) : 0);
  const gPct = d.grid_percent ?? (total > 0 ? 100 - sPct : 0);
  msGauge.setSub(total > 0 ? `${sPct}% solar` : "idle");

  $("ms_solar_w").textContent = fmt(solar, 0);
  $("ms_grid_w").textContent = fmt(grid, 0);
  $("ms_solar_bar").style.width = clampPct(solar, MS_MAX) + "%";
  $("ms_grid_bar").style.width = clampPct(grid, MS_MAX) + "%";
  $("ms_solar_sub").textContent = total > 0 ? `${sPct}%` : "—";
  $("ms_grid_sub").textContent = total > 0 ? `${gPct}%` : "—";

  const on = d.power === true;
  const work = prettyMs(d.work_status);
  setPill($("ms_pill"), on ? (work || "On") : "Off", on ? "accent" : "");
  msPowerOn = on;
  msApplianceAvailable = true;
  msCooldownRemaining = d.power_cooldown || 0;
  syncPowerBtn();
  msCurrentMode = d.mode;
  // once the unit reports the mode we staged, the change has landed — drop the pending state
  if (msPendingMode && String(msPendingMode).toLowerCase() === String(d.mode || "").toLowerCase()) msPendingMode = null;
  msModeCooldownRemaining = d.mode_cooldown || 0;
  renderModes();

  // secondary tile: current room temperature + the unit's settings
  $("ms_tile_temp").textContent = tempCF(d.temp_current_c);
  if (on) {
    const set = d.temp_set_c != null ? `${Math.round(d.temp_set_c)}°C` : "—";
    const bits = [`set ${set}`];  // mode now lives in the dropdown above, so it's not repeated here
    if (d.fan_speed) bits.push(`Fan ${prettyMs(d.fan_speed)}`);
    if (d.fault_labels && d.fault_labels.length) bits.push(`⚠ ${d.fault_labels.map(prettyMs).join(", ")}`);
    $("ms_tile_sub").textContent = bits.join(" · ");
  } else {
    $("ms_tile_sub").textContent = "Off";
  }
}

async function loadAppliance() {
  try {
    updateAppliance(await (await fetch("api/appliance", { cache: "no-store" })).json());
  } catch (e) { /* leave previous render */ }
}

async function toggleMsPower() {
  if ($("ms_power_btn").disabled) return;
  const target = !msPowerOn;
  msPowerBusy = true;
  syncPowerBtn();
  try {
    const r = await (await fetch("api/appliance/power", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on: target }),
    })).json();
    if (r.ok) {
      msPowerOn = target;
      showToast(target ? "Mini-split turned on" : "Mini-split turned off", "ok");
    } else if (r.cooldown) {
      msCooldownRemaining = r.retry_after || msCooldownRemaining;
      showToast(`Locked ${mmss(r.retry_after || 0)} — compressor protection`, "err");
    } else {
      showToast(r.error || "Command didn't go through — try again", "err");
    }
  } catch (e) {
    showToast("Command failed — dashboard unreachable", "err");
  } finally {
    msPowerBusy = false;
    syncPowerBtn();
    loadAppliance();
  }
}

async function setMsMode(mode) {
  if (msModeBusy) return;
  msModeBusy = true;
  renderModes();
  try {
    const r = await (await fetch("api/appliance/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    })).json();
    if (r.ok) {
      msCurrentMode = mode;  // optimistic — the next poll will confirm
      msPendingMode = null;  // applied — clear the staged change
      showToast(`Mode set to ${msModeLabel(mode)}`, "ok");
    } else if (r.cooldown) {
      msModeCooldownRemaining = r.retry_after || msModeCooldownRemaining;
      showToast(`Heat/cool switch locked ${mmss(r.retry_after || 0)} — compressor protection`, "err");
    } else {
      showToast(r.error || "Mode change didn't go through — try again", "err");
    }
  } catch (e) {
    showToast("Command failed — dashboard unreachable", "err");
  } finally {
    msModeBusy = false;
    renderModes();
    loadAppliance();
  }
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

// ---- inverter AC-output control (emergency shutoff) -----------------------
// Opt-in on the server (SOLAR_INVERTER_CONTROL). Turning ON is a direct click (recovery);
// turning OFF opens a modal whose confirm button must be held ~2s (writes SRNE 0xDF00).

const ACOUT_HOLD_MS = 2000;
let _acoutHoldTimer = null;

// Reflect the three state vars onto the power button (mirrors syncPowerBtn for the mini-split).
function syncAcOutBtn() {
  const btn = $("acout_power_btn");
  btn.hidden = !invControlEnabled;
  if (!invControlEnabled) return;
  btn.classList.toggle("on", invOutputOn === true);
  btn.classList.toggle("busy", invOutputBusy);
  btn.disabled = invOutputBusy;
  btn.title = invOutputOn === false ? "Turn the AC output ON" : "Turn the AC output OFF";
}

function renderAcOutBtn(d) {
  invControlEnabled = d.inverter_control === true;
  invOutputOn = d.output_on;  // true / false / null (unknown)
  syncAcOutBtn();
}

function onAcOutClick() {
  if (invOutputBusy) return;
  if (invOutputOn === false) setAcOutput(true);  // OFF -> ON is the safe recovery direction: direct
  else openAcOutModal();                         // ON (or unknown) -> OFF is gated by hold-to-confirm
}

async function setAcOutput(on) {
  if (invOutputBusy) return;
  invOutputBusy = true;
  syncAcOutBtn();
  try {
    const r = await (await fetch("api/inverter/output", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    })).json();
    if (r.ok) {
      invOutputOn = on;
      showToast(on ? "AC output turned on" : "AC output turned OFF", on ? "ok" : "err");
    } else if (r.cooldown) {
      showToast(`Locked ${r.retry_after || 0}s — try again`, "err");
    } else {
      showToast(r.error || "Command didn't go through — try again", "err");
    }
  } catch (e) {
    showToast("Command failed — dashboard unreachable", "err");
  } finally {
    invOutputBusy = false;
    syncAcOutBtn();
    loadCurrent();
  }
}

function openAcOutModal() { $("acout_modal").hidden = false; }
function closeAcOutModal() { cancelAcOutHold(); $("acout_modal").hidden = true; }

function startAcOutHold() {
  const fill = $("acout_hold_fill");
  fill.style.transition = `width ${ACOUT_HOLD_MS}ms linear`;
  fill.style.width = "100%";
  _acoutHoldTimer = setTimeout(() => { _acoutHoldTimer = null; closeAcOutModal(); setAcOutput(false); }, ACOUT_HOLD_MS);
}

function cancelAcOutHold() {
  if (_acoutHoldTimer) { clearTimeout(_acoutHoldTimer); _acoutHoldTimer = null; }
  const fill = $("acout_hold_fill");
  fill.style.transition = "width 0.12s ease";
  fill.style.width = "0%";
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

// "(peak 2.39 kW)" from an all-time peak in watts; blank until there's a reading. The number is
// bolded so it inherits the Solar/Load colour from the enclosing .lti.in / .lti.out.
const peakKw = (w) => (w == null || w <= 0 ? "" : `(peak <b>${fmt(w / 1000, 2)}</b> kW)`);

// All-time totals, shown compactly in the Power history header. The Solar/Load peaks are all-time
// (independent of the chart's time range), so they don't move when the range buttons change.
async function loadLifetime() {
  try {
    const lt = await (await fetch("api/energy/lifetime", { cache: "no-store" })).json();
    $("life_in").textContent = fmt(lt.pv_kwh, 1);
    $("life_out").textContent = fmt(lt.load_kwh, 1);
    $("life_charge").textContent = fmt(lt.charge_kwh, 1);
    $("life_discharge").textContent = fmt(lt.discharge_kwh, 1);
    $("life_in_peak").innerHTML = peakKw(lt.pv_peak_w);
    $("life_out_peak").innerHTML = peakKw(lt.load_peak_w);
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

// ---- mini-split energy chart (solar vs grid draw, kWh) ---------------------

const MS_ENERGY_SERIES = [
  { key: "solar", label: "Solar (kWh)", color: "#FBBF24" },
  { key: "grid", label: "Grid · AC (kWh)", color: "#2DD4BF" },
];
const msEnergyVisible = () => ({ solar: seriesVisible("msenergy", "solar"), grid: seriesVisible("msenergy", "grid") });

async function loadMsEnergy(period) {
  const slots = genSlots(period);
  if (!slots.length) return;
  let payload;
  try { payload = await (await fetch(`api/appliance/energy?period=${period}&start=${slots[0].start_ts}`, { cache: "no-store" })).json(); }
  catch (e) { return; }
  const byKey = {};
  for (const b of payload.buckets || []) byKey[b.bucket] = b;
  let tSolar = 0, tGrid = 0, maxSolar = 0, maxGrid = 0;
  const merged = slots.map((s) => {
    const d = byKey[s.key];
    const solar = d ? d.solar_kwh : 0, grid = d ? d.grid_kwh : 0;
    tSolar += solar; tGrid += grid;
    if (solar > maxSolar) maxSolar = solar;
    if (grid > maxGrid) maxGrid = grid;
    return { key: s.key, label: s.label, title: s.title, solar, grid };
  });
  msEnergyView = { period, rows: merged };
  $("mse_solar").textContent = fmt(tSolar, 1);
  $("mse_grid").textContent = fmt(tGrid, 1);
  $("mse_max_solar").textContent = fmt(maxSolar, 2);
  $("mse_max_grid").textContent = fmt(maxGrid, 2);
  renderMsEnergyBars($("msEbars"), merged, msEnergyVisible());
}

function renderMsEnergyLegend() {
  $("msElegend").innerHTML = MS_ENERGY_SERIES
    .map((s) => `<span class="item${seriesVisible("msenergy", s.key) ? "" : " off"}" data-key="${s.key}" title="Show/hide ${s.label}"><span class="swatch" style="background:${s.color}"></span>${s.label}</span>`)
    .join("");
}

function initMsERanges() {
  renderMsEnergyLegend();
  $("msElegend").addEventListener("click", (e) => {
    const item = e.target.closest(".item");
    if (!item) return;
    const key = item.dataset.key;
    const next = !seriesVisible("msenergy", key);
    setSeriesVisible("msenergy", key, next);
    item.classList.toggle("off", !next);
    if (msEnergyView) renderMsEnergyBars($("msEbars"), msEnergyView.rows, msEnergyVisible());
  });
  $("mseRanges").addEventListener("click", (e) => {
    const btn = e.target.closest("button");
    if (!btn) return;
    document.querySelectorAll("#mseRanges button").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    msActivePeriod = btn.dataset.period;
    loadMsEnergy(msActivePeriod);
  });
}

// ---- settings menu --------------------------------------------------------

const SETTING = { acin: "solar.showAcIn", battery: "solar.showBattery", energy: "solar.showEnergy", history: "solar.showHistory", batteryHistory: "solar.showBatteryHistory", msEnergy: "solar.showMsEnergy" };
const getBool = (k, def) => { const v = localStorage.getItem(k); return v === null ? def : v === "1"; };

function applySettings() {
  const acin = getBool(SETTING.acin, true);
  const battery = getBool(SETTING.battery, true);
  const energy = getBool(SETTING.energy, true);
  const history = getBool(SETTING.history, true);
  const batteryHistory = getBool(SETTING.batteryHistory, true);
  const msEnergy = getBool(SETTING.msEnergy, true);
  document.body.classList.toggle("hide-acin", !acin);
  document.body.classList.toggle("hide-battery", !battery);
  document.body.classList.toggle("hide-energy", !energy);
  document.body.classList.toggle("hide-history", !history);
  document.body.classList.toggle("hide-battery-history", !batteryHistory);
  document.body.classList.toggle("hide-msenergy", !msEnergy);
  $("toggleAcIn").checked = acin;
  $("toggleBattery").checked = battery;
  $("toggleEnergy").checked = energy;
  $("toggleHistory").checked = history;
  $("toggleBatteryHistory").checked = batteryHistory;
  $("toggleMsEnergy").checked = msEnergy;
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
  bind(SETTING.msEnergy, "toggleMsEnergy");
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
initMsERanges();
initEbarPopup($("msEbars"));
$("exportBtn").addEventListener("click", exportEnergyCSV);
$("snapBtn").addEventListener("click", takeSnapshot);
$("ms_power_btn").addEventListener("click", toggleMsPower);
$("ms_mode_btn").addEventListener("click", (e) => { e.stopPropagation(); toggleModeMenu(); });
$("ms_mode_menu").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && !b.disabled) { selectMode(b.dataset.mode); closeModeMenu(); } });
$("ms_mode_apply").addEventListener("click", (e) => { e.stopPropagation(); applyMode(); });
$("ms_mode_cancel").addEventListener("click", (e) => { e.stopPropagation(); cancelMode(); });
$("acout_power_btn").addEventListener("click", onAcOutClick);
$("acout_modal_cancel").addEventListener("click", closeAcOutModal);
$("acout_modal").addEventListener("click", (e) => { if (e.target === $("acout_modal")) closeAcOutModal(); });
$("acout_modal_confirm").addEventListener("pointerdown", (e) => { e.preventDefault(); startAcOutHold(); });
["pointerup", "pointerleave", "pointercancel"].forEach((ev) => $("acout_modal_confirm").addEventListener(ev, cancelAcOutHold));
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAcOutModal(); });
document.addEventListener("click", (e) => { if (!e.target.closest("#ms_mode_select")) closeModeMenu(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModeMenu(); });
initSettings();
initUnitToggles();
loadBattery();
loadAppliance();
loadCurrent();
loadHistory(activeWin);
loadBatteryHistory(activeBattWin);
loadToday();
loadLifetime();
loadEnergy(activePeriod);
loadMsEnergy(msActivePeriod);
setInterval(loadCurrent, 5000);
setInterval(loadAppliance, 5000);
setInterval(() => {
  if (msCooldownRemaining > 0) { msCooldownRemaining = Math.max(0, msCooldownRemaining - 1); syncPowerBtn(); }
  if (msModeCooldownRemaining > 0) { msModeCooldownRemaining = Math.max(0, msModeCooldownRemaining - 1); renderModes(); }
}, 1000);
setInterval(loadBattery, 20000);
setInterval(() => loadHistory(activeWin), 30000);
setInterval(() => loadBatteryHistory(activeBattWin), 30000);
setInterval(loadToday, 60000);
setInterval(loadLifetime, 60000);
setInterval(() => loadEnergy(activePeriod), 60000);
setInterval(() => loadMsEnergy(msActivePeriod), 60000);
