"""Pure API payload builders over the store — no web framework here, so they unit-test
on a bare Python. server.py is a thin FastAPI adapter that just calls these.
"""
from __future__ import annotations

import time
from typing import Dict, List, Optional, Tuple

from .db import NUMERIC_COLUMNS, TimeSeriesStore
from .faults import FaultCatalog

# Sensible default series for the dashboard's main chart.
DEFAULT_HISTORY_FIELDS = ["pv_power", "load_total", "battery_power", "battery_soc"]


def _battery_eta(d: Dict[str, object], capacity_wh: Optional[float]):
    """Static time-to-full/empty estimate (minutes) from SOC, current power, and capacity.

    Charging -> minutes to full; discharging -> minutes to empty; idle/unknown -> (None, None).
    """
    soc = d.get("battery_soc")
    bw = d.get("battery_power")
    if not capacity_wh or soc is None or bw is None:
        return None, None
    if bw > 10:  # charging
        if soc >= 100:
            return 0, "full"
        remaining_wh = capacity_wh * (100 - soc) / 100.0
        return round(remaining_wh / bw * 60), "full"
    if bw < -10:  # discharging
        if soc <= 0:
            return 0, "empty"
        stored_wh = capacity_wh * soc / 100.0
        return round(stored_wh / (-bw) * 60), "empty"
    return None, None  # ~idle


def current_payload(
    store: TimeSeriesStore,
    catalog: Optional[FaultCatalog] = None,
    battery_capacity_wh: Optional[float] = None,
) -> Dict[str, object]:
    """Latest snapshot for the live tiles, with faults annotated and a battery ETA."""
    latest = store.latest()
    if latest is None:
        return {"available": False}
    faults = latest.get("fault_codes") or []
    annotated = catalog.annotate(faults) if catalog else [{"code": c, "text": str(c)} for c in faults]
    out: Dict[str, object] = {"available": True, "ts": latest["ts"], "faults": annotated}
    for key, value in latest.items():
        if key not in ("ts", "fault_codes"):
            out[key] = value
    # Derive per-string PV power (the stored row keeps PV1/PV2 voltage & current, not the product).
    for n in (1, 2):
        v = out.get(f"pv{n}_voltage")
        i = out.get(f"pv{n}_current")
        out[f"pv{n}_power"] = round(v * i, 1) if (v is not None and i is not None) else None
    out["battery_eta_minutes"], out["battery_eta_kind"] = _battery_eta(out, battery_capacity_wh)
    return out


def history_payload(
    store: TimeSeriesStore,
    fields: Optional[List[str]] = None,
    start: Optional[int] = None,
    end: Optional[int] = None,
    max_points: int = 1000,
) -> Dict[str, object]:
    """Columnar time-series for charting: {ts:[...], series:{field:[...]}, fields:[...]}.

    Columnar (parallel arrays) is what uPlot consumes directly and keeps the JSON small.
    """
    requested = fields or DEFAULT_HISTORY_FIELDS
    used = [f for f in requested if f in NUMERIC_COLUMNS]
    rows = store.series(used, start=start, end=end, max_points=max_points)
    return {
        "fields": used,
        "ts": [r["ts"] for r in rows],
        "series": {f: [r.get(f) for r in rows] for f in used},
        "count": len(rows),
    }


ENERGY_PERIODS = ("hour", "day", "month")


def energy_payload(
    store: TimeSeriesStore,
    period: str = "day",
    start: Optional[int] = None,
    end: Optional[int] = None,
    limit: Optional[int] = None,
) -> Dict[str, object]:
    """Energy roll-up buckets (kWh) for the trends chart."""
    if period not in ENERGY_PERIODS:
        period = "day"
    return {"period": period, "buckets": store.energy_buckets(period, start=start, end=end, limit=limit)}


def lifetime_payload(store: TimeSeriesStore) -> Dict[str, object]:
    """All-time input (PV) / output (load) energy totals (kWh) for the header strip."""
    return store.energy_lifetime()


def snapshot_inputs(
    store: TimeSeriesStore,
    catalog: Optional[FaultCatalog],
    bms_poller,
    battery_capacity_wh: Optional[float] = None,
) -> Tuple[dict, dict, list, dict, dict, dict]:
    """Gather the six payloads a static HTML snapshot needs, straight from the store/pollers
    (no HTTP). The in-process twin of what `solar snapshot` fetches over the API, so the server's
    POST /api/snapshot and the CLI feed the same `cli.snapshot_doc(...)` builder.

    Returns (current, today_bucket, hourly_buckets, lifetime, battery, history).
    """
    cur = current_payload(store, catalog, battery_capacity_wh=battery_capacity_wh)
    now = time.localtime()
    today_mid = int(time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1)))
    day_key = time.strftime("%Y-%m-%d", now)
    days = energy_payload(store, "day", start=today_mid - 86400)["buckets"]  # yesterday + today
    today = next((b for b in days if b.get("bucket") == day_key), {})
    hourly = energy_payload(store, "hour", start=today_mid)["buckets"]
    life = lifetime_payload(store)
    batt = battery_payload(bms_poller)
    now_s = int(time.time())
    hist = history_payload(
        store, ["pv_power", "load_total", "battery_power"], start=now_s - 6 * 3600, max_points=360
    )
    return cur, today, hourly, life, batt, hist


def battery_payload(poller) -> Dict[str, object]:
    """Latest BMS snapshot: bank summary + per-pack detail (cells, temps, SOC)."""
    if poller is None or getattr(poller, "bank", None) is None:
        return {"available": False}
    b = poller.bank
    packs = []
    for s in poller.packs:
        if s is None:
            continue
        temps = s.info.temps_c
        packs.append({
            "name": s.name,
            "address": s.address,
            "parallel": s.parallel,
            "voltage": s.info.voltage,
            "current": s.info.current,
            "power": round(s.info.power, 1),
            "soc": s.info.soc,
            "residual_ah": s.info.residual_ah,
            "nominal_ah": s.info.nominal_ah,
            "cycles": s.info.cycles,
            "protection": s.info.protection,
            "cells": s.cells,
            "cell_min": s.cell_min,
            "cell_max": s.cell_max,
            "cell_delta": s.cell_delta,
            "temps": temps,
            "temp_min": min(temps) if temps else None,
            "temp_max": max(temps) if temps else None,
            "has_fault": s.info.has_fault,
        })
    return {
        "available": True,
        "ts": poller.last_ts,
        "bank": {
            "packs": b.packs,
            "voltage": b.voltage,
            "current": b.current,
            "power": b.power,
            "soc": b.soc,
            "nominal_ah": b.nominal_ah,
            "residual_ah": b.residual_ah,
            "capacity_kwh": b.capacity_kwh,
            "cell_min": b.cell_min,
            "cell_max": b.cell_max,
            "cell_delta": b.cell_delta,
            "temp_min": b.temp_min,
            "temp_max": b.temp_max,
            "fault_packs": b.fault_packs,
        },
        "packs": packs,
    }


def appliance_payload(poller) -> Dict[str, object]:
    """Latest mini-split snapshot: power, climate, and the solar/grid power+energy split.

    `raw_dps` carries the unmapped Tuya datapoints too, for debugging an unfamiliar firmware
    over `curl` without a code change.
    """
    if poller is None or getattr(poller, "status", None) is None:
        return {"available": False}
    s = poller.status
    return {
        "available": True,
        "ts": poller.last_ts,
        "power": s.power,
        "mode": s.mode,
        "work_status": s.work_status,
        "fan_speed": s.fan_speed,
        "temp_set_c": s.temp_set_c,
        "temp_current_c": s.temp_current_c,
        "temp_set_f": s.temp_set_f,
        "temp_current_f": s.temp_current_f,
        "fault_labels": s.fault_labels,
        "solar_power": s.solar_power,
        "grid_power": s.grid_power,
        "solar_energy": s.solar_energy,
        "total_energy": s.total_energy,
        "solar_percent": s.solar_percent,
        "grid_percent": s.grid_percent,
        "raw_dps": poller.raw_dps,
        "power_cooldown": poller.power_cooldown_remaining() if hasattr(poller, "power_cooldown_remaining") else 0,
        "mode_cooldown": poller.mode_reverse_cooldown_remaining() if hasattr(poller, "mode_reverse_cooldown_remaining") else 0,
    }
