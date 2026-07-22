"""SQLite time-series store for inverter samples.

One row per successful poll: an epoch timestamp plus every InverterStatus field
(including the derived power/total fields, so charts need no recompute). Tuned for
a Raspberry Pi Zero 2 W — WAL mode lets the FastAPI reader run while the poller
writes, and range queries downsample server-side to keep payloads small.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Dict, List, Optional

from .inverter import InverterStatus

# (column, sql_type) in insert order. Derived fields are materialised for fast charting.
# fault_codes is stored as a JSON array of ints (TEXT).
COLUMNS = [
    ("battery_soc", "INTEGER"),
    ("battery_voltage", "REAL"),
    ("battery_current", "REAL"),
    ("battery_power", "REAL"),
    ("battery_temp", "REAL"),
    ("bms_soc", "REAL"),  # BMS bank SOC (%), stamped from the BLE poller when present (not an inverter field)
    ("pv1_voltage", "REAL"),
    ("pv1_current", "REAL"),
    ("pv2_voltage", "REAL"),
    ("pv2_current", "REAL"),
    ("pv_power", "REAL"),
    ("grid_voltage", "REAL"),
    ("grid_frequency", "REAL"),
    ("output_voltage", "REAL"),
    ("output_frequency", "REAL"),
    ("load_power", "INTEGER"),
    ("load_apparent", "INTEGER"),
    ("load_current", "REAL"),
    ("load_l2_power", "INTEGER"),
    ("load_l2_apparent", "INTEGER"),
    ("load_l2_current", "REAL"),
    ("load_total", "INTEGER"),
    ("load_apparent_total", "INTEGER"),
    ("grid_l2_voltage", "REAL"),
    ("output_l2_voltage", "REAL"),
    ("dc_temp", "REAL"),
    ("ac_temp", "REAL"),
    ("machine_state", "INTEGER"),
    ("fault_codes", "TEXT"),
]
COLUMN_NAMES = [c for c, _ in COLUMNS]
# Numeric columns that make sense to chart / average when downsampling.
NUMERIC_COLUMNS = [c for c, _ in COLUMNS if c != "fault_codes"]


def status_to_row(status: InverterStatus) -> Dict[str, object]:
    """Flatten an InverterStatus (incl. derived properties) into a column->value dict."""
    return {
        "battery_soc": status.battery_soc,
        "battery_voltage": status.battery_voltage,
        "battery_current": status.battery_current,
        "battery_power": status.battery_power,
        "battery_temp": status.battery_temp,
        "pv1_voltage": status.pv1_voltage,
        "pv1_current": status.pv1_current,
        "pv2_voltage": status.pv2_voltage,
        "pv2_current": status.pv2_current,
        "pv_power": status.pv_power,
        "grid_voltage": status.grid_voltage,
        "grid_frequency": status.grid_frequency,
        "output_voltage": status.output_voltage,
        "output_frequency": status.output_frequency,
        "load_power": status.load_power,
        "load_apparent": status.load_apparent,
        "load_current": status.load_current,
        "load_l2_power": status.load_l2_power,
        "load_l2_apparent": status.load_l2_apparent,
        "load_l2_current": status.load_l2_current,
        "load_total": status.load_total,
        "load_apparent_total": status.load_apparent_total,
        "grid_l2_voltage": status.grid_l2_voltage,
        "output_l2_voltage": status.output_l2_voltage,
        "dc_temp": status.dc_temp,
        "ac_temp": status.ac_temp,
        "machine_state": status.machine_state,
        "fault_codes": json.dumps(status.fault_codes),
    }


class TimeSeriesStore:
    def __init__(self, path: str = "data/solar.sqlite"):
        self.path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL = concurrent reader (server) + writer (poller); no-op on :memory:.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._create_schema()

    def _create_schema(self) -> None:
        cols_sql = ",\n  ".join(f"{name} {sqltype}" for name, sqltype in COLUMNS)
        with self._lock:
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS inverter_samples (\n"
                f"  ts INTEGER NOT NULL,\n  {cols_sql}\n)"
            )
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_samples_ts ON inverter_samples(ts)"
            )
            # Migration: add any columns introduced after this DB's table was first created
            # (CREATE TABLE IF NOT EXISTS won't alter an existing table). Additive and safe —
            # old rows get NULL for the new column.
            existing = {r["name"] for r in self._conn.execute("PRAGMA table_info(inverter_samples)")}
            for name, sqltype in COLUMNS:
                if name not in existing:
                    self._conn.execute(f"ALTER TABLE inverter_samples ADD COLUMN {name} {sqltype}")
            # Hourly energy accumulators (Wh). pv_wh = solar input, load_wh = AC output,
            # charge/discharge_wh = battery. Roll-ups (day/month/lifetime) SUM these rows.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS energy_hourly (\n"
                "  hour INTEGER PRIMARY KEY,\n"
                "  pv_wh REAL NOT NULL DEFAULT 0,\n"
                "  load_wh REAL NOT NULL DEFAULT 0,\n"
                "  charge_wh REAL NOT NULL DEFAULT 0,\n"
                "  discharge_wh REAL NOT NULL DEFAULT 0\n)"
            )
            # Mini-split hourly energy (Wh): how much the appliance drew from solar (DC) vs grid
            # (AC), accrued by the AppliancePoller. Rolled up day/month the same way as the inverter.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS appliance_energy_hourly (\n"
                "  hour INTEGER PRIMARY KEY,\n"
                "  solar_wh REAL NOT NULL DEFAULT 0,\n"
                "  grid_wh REAL NOT NULL DEFAULT 0\n)"
            )
            # All-time peak instantaneous power (W): a single row holding the highest pv_power /
            # load_total ever sampled. Rolled forward on insert() so the lifetime-peak read is O(1)
            # (no full-table scan every minute) and survives sample pruning. Seeded from any history
            # already on disk so an upgrade reflects past peaks immediately.
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS energy_peaks (\n"
                "  id INTEGER PRIMARY KEY CHECK (id = 1),\n"
                "  pv_w REAL NOT NULL DEFAULT 0,\n"
                "  load_w REAL NOT NULL DEFAULT 0\n)"
            )
            self._conn.execute(
                "INSERT OR IGNORE INTO energy_peaks (id, pv_w, load_w) "
                "SELECT 1, COALESCE(MAX(pv_power), 0), COALESCE(MAX(load_total), 0) FROM inverter_samples"
            )
            self._conn.commit()

    def insert(self, status: InverterStatus, ts: Optional[int] = None, bms_soc: Optional[float] = None) -> int:
        if ts is None:
            ts = int(time.time())
        row = status_to_row(status)
        row["bms_soc"] = bms_soc  # not an InverterStatus field; the poller supplies the BMS bank SOC
        cols = ["ts"] + COLUMN_NAMES
        placeholders = ", ".join("?" for _ in cols)
        values = [ts] + [row[name] for name in COLUMN_NAMES]
        pv_peak = row.get("pv_power") or 0
        load_peak = row.get("load_total") or 0
        with self._lock:
            self._conn.execute(
                f"INSERT INTO inverter_samples ({', '.join(cols)}) VALUES ({placeholders})",
                values,
            )
            # Roll the all-time power peaks forward with this sample.
            self._conn.execute(
                "INSERT INTO energy_peaks (id, pv_w, load_w) VALUES (1, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET "
                "  pv_w = MAX(pv_w, excluded.pv_w), load_w = MAX(load_w, excluded.load_w)",
                (pv_peak, load_peak),
            )
            self._conn.commit()
        return ts

    def _row_to_dict(self, row: Optional[sqlite3.Row]) -> Optional[Dict[str, object]]:
        if row is None:
            return None
        d = dict(row)
        if d.get("fault_codes"):
            try:
                d["fault_codes"] = json.loads(d["fault_codes"])
            except (ValueError, TypeError):
                d["fault_codes"] = []
        else:
            d["fault_codes"] = []
        return d

    def latest(self) -> Optional[Dict[str, object]]:
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM inverter_samples ORDER BY ts DESC LIMIT 1"
            )
            return self._row_to_dict(cur.fetchone())

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM inverter_samples").fetchone()[0]

    def series(
        self,
        fields: List[str],
        start: Optional[int] = None,
        end: Optional[int] = None,
        max_points: Optional[int] = None,
    ) -> List[Dict[str, object]]:
        """Return [{ts, field: value, ...}] over [start, end], averaged into at most
        max_points buckets (server-side downsampling for fast, small chart payloads)."""
        safe = [f for f in fields if f in NUMERIC_COLUMNS]
        if not safe:
            return []
        where, params = [], []
        if start is not None:
            where.append("ts >= ?")
            params.append(start)
        if end is not None:
            where.append("ts <= ?")
            params.append(end)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""

        with self._lock:
            total = self._conn.execute(
                f"SELECT COUNT(*) FROM inverter_samples{where_sql}", params
            ).fetchone()[0]
            if total == 0:
                return []

            if max_points and total > max_points:
                bounds = self._conn.execute(
                    f"SELECT MIN(ts), MAX(ts) FROM inverter_samples{where_sql}", params
                ).fetchone()
                span = max(1, (bounds[1] - bounds[0]))
                bucket = max(1, span // max_points)
                selects = ", ".join(f"AVG({f}) AS {f}" for f in safe)
                sql = (
                    f"SELECT (ts / ?) * ? AS ts, {selects} FROM inverter_samples"
                    f"{where_sql} GROUP BY ts / ? ORDER BY ts"
                )
                rows = self._conn.execute(sql, [bucket, bucket] + params + [bucket]).fetchall()
            else:
                selects = ", ".join(safe)
                sql = (
                    f"SELECT ts, {selects} FROM inverter_samples{where_sql} ORDER BY ts"
                )
                rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ---- energy (kWh) ----------------------------------------------------- #

    _PERIOD_FMT = {"hour": "%Y-%m-%d %H:00", "day": "%Y-%m-%d", "month": "%Y-%m"}

    def accrue(self, ts: int, dt_s: float, pv_w: float, load_w: float, batt_w: float) -> None:
        """Add energy from a dt_s-long interval at average powers into the hour-of-ts bucket.
        Negative PV/load are clamped to 0; battery splits into charge (+) / discharge (-)."""
        if dt_s <= 0:
            return
        hour = ts - (ts % 3600)
        f = dt_s / 3600.0
        pv = max(0.0, pv_w) * f
        load = max(0.0, load_w) * f
        charge = max(0.0, batt_w) * f
        discharge = max(0.0, -batt_w) * f
        with self._lock:
            self._conn.execute(
                "INSERT INTO energy_hourly (hour, pv_wh, load_wh, charge_wh, discharge_wh) "
                "VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(hour) DO UPDATE SET "
                "  pv_wh = pv_wh + excluded.pv_wh, "
                "  load_wh = load_wh + excluded.load_wh, "
                "  charge_wh = charge_wh + excluded.charge_wh, "
                "  discharge_wh = discharge_wh + excluded.discharge_wh",
                (hour, pv, load, charge, discharge),
            )
            self._conn.commit()

    def accrue_appliance_wh(self, ts: int, solar_wh: float, grid_wh: float) -> None:
        """Add already-computed mini-split energy (Wh) into the hour-of-ts bucket. Negatives clamp
        to 0; a fully-zero contribution writes nothing (no empty rows)."""
        solar = max(0.0, solar_wh)
        grid = max(0.0, grid_wh)
        if solar <= 0 and grid <= 0:
            return
        hour = ts - (ts % 3600)
        with self._lock:
            self._conn.execute(
                "INSERT INTO appliance_energy_hourly (hour, solar_wh, grid_wh) VALUES (?, ?, ?) "
                "ON CONFLICT(hour) DO UPDATE SET "
                "  solar_wh = solar_wh + excluded.solar_wh, grid_wh = grid_wh + excluded.grid_wh",
                (hour, solar, grid),
            )
            self._conn.commit()

    def accrue_appliance(self, ts: int, dt_s: float, solar_w: float, grid_w: float) -> None:
        """Add the mini-split's energy from a dt_s-long interval (avg powers) into the hour bucket."""
        if dt_s <= 0:
            return
        f = dt_s / 3600.0
        self.accrue_appliance_wh(ts, max(0.0, solar_w) * f, max(0.0, grid_w) * f)

    def appliance_energy_buckets(
        self, period: str, start: Optional[int] = None, end: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Dict[str, object]]:
        """Roll the mini-split's hourly energy into hour/day/month buckets (local time). kWh."""
        fmt = self._PERIOD_FMT.get(period)
        if fmt is None:
            return []
        where, params = [], []
        if start is not None:
            where.append("hour >= ?")
            params.append(start - (start % 3600))
        if end is not None:
            where.append("hour <= ?")
            params.append(end)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT strftime('{fmt}', hour, 'unixepoch', 'localtime') AS bucket, "
            f"  MIN(hour) AS start_ts, "
            f"  SUM(solar_wh) AS solar, SUM(grid_wh) AS grid "
            f"FROM appliance_energy_hourly{where_sql} GROUP BY bucket ORDER BY start_ts"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [
            {
                "bucket": r["bucket"],
                "start_ts": r["start_ts"],
                "solar_kwh": round((r["solar"] or 0) / 1000.0, 4),
                "grid_kwh": round((r["grid"] or 0) / 1000.0, 4),
            }
            for r in rows
        ]
        return out[-limit:] if limit else out

    def energy_buckets(
        self, period: str, start: Optional[int] = None, end: Optional[int] = None, limit: Optional[int] = None
    ) -> List[Dict[str, object]]:
        """Roll hourly energy up into hour/day/month buckets (local time). Values in kWh."""
        fmt = self._PERIOD_FMT.get(period)
        if fmt is None:
            return []
        where, params = [], []
        if start is not None:
            where.append("hour >= ?")
            params.append(start - (start % 3600))
        if end is not None:
            where.append("hour <= ?")
            params.append(end)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        sql = (
            f"SELECT strftime('{fmt}', hour, 'unixepoch', 'localtime') AS bucket, "
            f"  MIN(hour) AS start_ts, "
            f"  SUM(pv_wh) AS pv, SUM(load_wh) AS load, "
            f"  SUM(charge_wh) AS charge, SUM(discharge_wh) AS discharge "
            f"FROM energy_hourly{where_sql} GROUP BY bucket ORDER BY start_ts"
        )
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        out = [
            {
                "bucket": r["bucket"],
                "start_ts": r["start_ts"],
                "pv_kwh": round((r["pv"] or 0) / 1000.0, 4),
                "load_kwh": round((r["load"] or 0) / 1000.0, 4),
                "charge_kwh": round((r["charge"] or 0) / 1000.0, 4),
                "discharge_kwh": round((r["discharge"] or 0) / 1000.0, 4),
            }
            for r in rows
        ]
        return out[-limit:] if limit else out

    def energy_lifetime(self) -> Dict[str, object]:
        """All-time energy totals (kWh), peak instantaneous power (W), and the span covered."""
        with self._lock:
            r = self._conn.execute(
                "SELECT SUM(pv_wh) pv, SUM(load_wh) load, SUM(charge_wh) charge, "
                "SUM(discharge_wh) discharge, MIN(hour) since, MAX(hour) last FROM energy_hourly"
            ).fetchone()
            p = self._conn.execute("SELECT pv_w, load_w FROM energy_peaks WHERE id = 1").fetchone()
        return {
            "pv_kwh": round((r["pv"] or 0) / 1000.0, 3),
            "load_kwh": round((r["load"] or 0) / 1000.0, 3),
            "charge_kwh": round((r["charge"] or 0) / 1000.0, 3),
            "discharge_kwh": round((r["discharge"] or 0) / 1000.0, 3),
            "pv_peak_w": round(p["pv_w"], 1) if p else None,
            "load_peak_w": round(p["load_w"], 1) if p else None,
            "since": r["since"],
            "last": r["last"],
        }

    def prune(self, older_than_ts: int) -> int:
        """Delete samples older than the given epoch ts; returns rows removed."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM inverter_samples WHERE ts < ?", (older_than_ts,)
            )
            self._conn.commit()
            return cur.rowcount

    def close(self) -> None:
        with self._lock:
            self._conn.close()
