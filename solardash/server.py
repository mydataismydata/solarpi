"""FastAPI app: serves the dashboard and a small JSON API, and runs the poll loop
as a background task. Thin adapter — all payload logic lives in api.py (unit-tested).

Run (after `pip install -r requirements.txt`):
    uvicorn solardash.server:app --host 0.0.0.0 --port 8000
Point at the real inverter with SOLAR_INVERTER_IP / SOLAR_INVERTER_SERIAL.
"""
from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Query
from fastapi.staticfiles import StaticFiles

from . import api, cli
from .appliance_client import ApplianceClient, AppliancePoller
from .bms_poller import BmsPoller
from .client import InverterClient
from .config import Config
from .db import TimeSeriesStore
from .faults import FaultCatalog
from .poller import Poller

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


def _start_mdns(port: int):
    """Advertise the dashboard as a `_solarpi._tcp` service so the Android app can auto-discover it.

    Best-effort: returns (Zeroconf, ServiceInfo) on success, or (None, None) if zeroconf isn't
    installed or registration fails — the dashboard still serves either way, just without mDNS.
    """
    try:
        import socket

        from zeroconf import ServiceInfo, Zeroconf

        # Resolve the primary LAN IP without sending anything (UDP connect just sets the route).
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()

        info = ServiceInfo(
            "_solarpi._tcp.local.",
            "Solar Pi._solarpi._tcp.local.",
            addresses=[socket.inet_aton(ip)],
            port=port,
            properties={"path": "/api"},
        )
        zc = Zeroconf()
        zc.register_service(info)
        return zc, info
    except Exception:
        return None, None


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = Config.from_env()
    db_dir = os.path.dirname(cfg.db_path)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    store = TimeSeriesStore(cfg.db_path)
    catalog = FaultCatalog.load()
    client = InverterClient(cfg.inverter_ip, cfg.inverter_serial, port=cfg.inverter_port)
    poller = Poller(client, store, interval_s=cfg.poll_interval_s)

    stop = asyncio.Event()
    task = asyncio.create_task(poller.run(stop))

    bms_poller = None
    bms_task = None
    bms_stop = asyncio.Event()
    if cfg.bms_enabled and cfg.bms_addresses:
        bms_poller = BmsPoller(cfg.bms_addresses, interval_s=cfg.bms_interval_s, positions=cfg.bms_positions)
        bms_task = asyncio.create_task(bms_poller.run(bms_stop))

    # Mini-split appliance (EG4/Deye over Wi-Fi, Tuya local v3.3). Off unless fully configured.
    appliance_poller = None
    appliance_task = None
    appliance_stop = asyncio.Event()
    if cfg.appliance_enabled and cfg.appliance_ip and cfg.appliance_device_id and cfg.appliance_local_key:
        appliance_client = ApplianceClient(
            cfg.appliance_ip,
            cfg.appliance_device_id,
            cfg.appliance_local_key,
            version=cfg.appliance_version,
            temp_divisor=cfg.appliance_temp_divisor,
        )
        appliance_poller = AppliancePoller(appliance_client, interval_s=cfg.appliance_interval_s)
        appliance_task = asyncio.create_task(appliance_poller.run(appliance_stop))

    # Advertise over mDNS so the Android app finds us. The serve port comes from uvicorn, not Config,
    # so read it from SOLAR_HTTP_PORT (default 8000) — set it if you serve on a different port.
    http_port = int(os.environ.get("SOLAR_HTTP_PORT", "8000"))
    zc, zc_info = _start_mdns(http_port)

    app.state.store = store
    app.state.catalog = catalog
    app.state.cfg = cfg
    app.state.poller = poller
    app.state.bms_poller = bms_poller
    app.state.appliance_poller = appliance_poller
    try:
        yield
    finally:
        if zc is not None:
            try:
                if zc_info is not None:
                    zc.unregister_service(zc_info)
                zc.close()
            except Exception:
                pass
        stop.set()
        task.cancel()
        if bms_task:
            bms_stop.set()
            bms_task.cancel()
        if appliance_task:
            appliance_stop.set()
            appliance_task.cancel()
        for t in (task, bms_task, appliance_task):
            if t:
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        store.close()


app = FastAPI(title="Solar Tracking Dashboard", lifespan=lifespan)


@app.middleware("http")
async def revalidate_static(request, call_next):
    """Tell browsers to revalidate static assets each load (ETag -> 304 if unchanged), so UI
    updates appear on a normal refresh without needing a hard-refresh."""
    response = await call_next(request)
    if not request.url.path.startswith("/api"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _battery_capacity_wh() -> float:
    """Prefer the BMS-derived bank capacity, falling back to the configured value. Watt-hours."""
    cap_kwh = app.state.cfg.battery_capacity_kwh
    bp = app.state.bms_poller
    if bp is not None and bp.bank is not None:
        cap_kwh = bp.bank.capacity_kwh
    return cap_kwh * 1000


@app.get("/api/current")
async def current():
    return api.current_payload(
        app.state.store, app.state.catalog, battery_capacity_wh=_battery_capacity_wh()
    )


@app.get("/api/battery")
async def battery():
    return api.battery_payload(app.state.bms_poller)


@app.post("/api/snapshot")
async def snapshot():
    """Capture the live dashboard as a self-contained static HTML file on the server (same output
    as the `solar snapshot` CLI), saved under SOLAR_EXPORT_DIR. Returns the written path."""
    inputs = api.snapshot_inputs(
        app.state.store, app.state.catalog, app.state.bms_poller, _battery_capacity_wh()
    )
    if not inputs[0].get("available"):
        return {"ok": False, "error": "no data yet — nothing to snapshot"}
    doc = cli.snapshot_doc(*inputs)
    try:
        os.makedirs(cli.EXPORT_DIR, exist_ok=True)
        filename = f"solar-snapshot-{time.strftime('%Y-%m-%d_%H%M', time.localtime())}.html"
        path = os.path.join(cli.EXPORT_DIR, filename)
        with open(path, "w", encoding="utf-8") as f:
            f.write(doc)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": True, "filename": filename, "path": path, "dir": cli.EXPORT_DIR}


@app.get("/api/appliance")
async def appliance():
    return api.appliance_payload(app.state.appliance_poller)


@app.get("/api/history")
async def history(
    fields: Optional[str] = Query(None, description="comma-separated field names"),
    start: Optional[int] = Query(None, description="epoch seconds, inclusive"),
    end: Optional[int] = Query(None, description="epoch seconds, inclusive"),
    max_points: int = Query(1000, ge=1, le=20000),
):
    field_list = [f.strip() for f in fields.split(",")] if fields else None
    return api.history_payload(app.state.store, field_list, start, end, max_points)


@app.get("/api/energy")
async def energy(
    period: str = Query("day", description="hour | day | month"),
    start: Optional[int] = Query(None),
    end: Optional[int] = Query(None),
    limit: Optional[int] = Query(None, ge=1, le=2000),
):
    return api.energy_payload(app.state.store, period, start, end, limit)


@app.get("/api/energy/lifetime")
async def energy_lifetime():
    return api.lifetime_payload(app.state.store)


@app.get("/api/health")
async def health():
    cfg = app.state.cfg
    poller = app.state.poller
    bp = app.state.bms_poller
    ap = app.state.appliance_poller
    return {
        "ok": True,
        "samples": app.state.store.count(),
        "inverter": f"{cfg.inverter_ip}:{cfg.inverter_port}",
        "poll_interval_s": cfg.poll_interval_s,
        "last_ts": poller.last_ts,
        "consecutive_failures": poller.consecutive_failures,
        "bms": {
            "enabled": cfg.bms_enabled,
            "packs": bp.bank.packs if (bp and bp.bank) else 0,
            "last_ts": bp.last_ts if bp else None,
            "failures": bp.consecutive_failures if bp else None,
        },
        "appliance": {
            "enabled": cfg.appliance_enabled,
            "last_ts": ap.last_ts if ap else None,
            "failures": ap.consecutive_failures if ap else None,
        },
    }


# Static dashboard at / (registered last so /api/* routes win). html=True serves index.html.
app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
