"""Runtime connect/unpair for the mini-split.

Persists the unit's LAN connection (IP, device id, local key) to a small JSON file and starts/stops
the AppliancePoller live, so the mini-split can be paired from the dashboard UI without editing
solardash.env or restarting. The file is the source of truth once written; env vars only seed it on
first run (backward compatibility with existing setups).

Stored shape: {"connected": bool, "ip": str, "device_id": str, "local_key": str, "version": float}.
Unpair writes {"connected": false} so the disconnected state survives a restart and overrides env.
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Callable, Optional

from .appliance_client import ApplianceClient, AppliancePoller

DEFAULT_INTERVAL_S = 30.0


def _complete(conn: Optional[dict]) -> bool:
    """True when a stored connection is paired and has every credential."""
    return bool(
        conn
        and conn.get("connected")
        and conn.get("ip")
        and conn.get("device_id")
        and conn.get("local_key")
    )


class ApplianceManager:
    def __init__(
        self,
        path: str,
        store,
        interval_s: float = DEFAULT_INTERVAL_S,
        version: float = 3.3,
        temp_divisor: float = 1.0,
        client_factory: Callable[..., ApplianceClient] = ApplianceClient,
    ):
        self.path = path
        self.store = store
        self.interval_s = interval_s
        self.version = version
        self.temp_divisor = temp_divisor
        self._client_factory = client_factory
        self.poller: Optional[AppliancePoller] = None
        self._task: Optional[asyncio.Task] = None
        self._stop: Optional[asyncio.Event] = None

    # ---- persistence ------------------------------------------------------ #

    def load_conn(self) -> Optional[dict]:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return None

    def _save_conn(self, conn: dict) -> None:
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(conn, f)
        os.replace(tmp, self.path)  # atomic swap so a crash can't leave a half-written file

    @property
    def configured(self) -> bool:
        return _complete(self.load_conn())

    # ---- poller lifecycle ------------------------------------------------- #

    def _spawn_poller(self, client: ApplianceClient) -> None:
        self.poller = AppliancePoller(client, interval_s=self.interval_s, store=self.store)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self.poller.run(self._stop))

    async def _stop_poller(self) -> None:
        if self._task is not None:
            self._stop.set()
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        self.poller = None
        self._task = None
        self._stop = None

    def _client_for(self, conn: dict) -> ApplianceClient:
        return self._client_factory(
            conn["ip"],
            conn["device_id"],
            conn["local_key"],
            version=conn.get("version", self.version),
            temp_divisor=self.temp_divisor,
        )

    # ---- public API ------------------------------------------------------- #

    async def start(self, env_conn: Optional[dict] = None) -> None:
        """Server startup: begin polling if a connection is stored (or seed the file from env)."""
        conn = self.load_conn()
        if conn is None and env_conn is not None:
            conn = env_conn
            self._save_conn(conn)  # migrate an existing env-based setup into the file, once
        if _complete(conn):
            self._spawn_poller(self._client_for(conn))

    async def connect(self, ip, device_id, local_key, version=None) -> dict:
        """Validate + test the connection, then persist it and start polling. Nothing is saved
        unless a live read succeeds, so a bad IP/key can't leave the dashboard stuck."""
        ip = (ip or "").strip()
        device_id = (device_id or "").strip()
        local_key = (local_key or "").strip()
        if not (ip and device_id and local_key):
            return {"ok": False, "error": "IP, device id, and local key are all required"}
        if self._client_factory is ApplianceClient:  # skip for injected (test) clients
            try:
                import tinytuya  # noqa: F401  (clear error if the Pi is missing the package)
            except ImportError:
                return {"ok": False, "error": "tinytuya isn't installed on the Pi (pip install tinytuya)"}
        try:
            ver = float(version) if version else self.version
        except (TypeError, ValueError):
            ver = self.version
        conn = {"connected": True, "ip": ip, "device_id": device_id, "local_key": local_key, "version": ver}

        client = self._client_for(conn)
        reading = await client.read()  # returns None on any failure (unreachable / wrong key)
        if reading is None:
            return {"ok": False, "error": "no response — check the IP, device id, and local key"}

        await self._stop_poller()
        self._save_conn(conn)
        self._spawn_poller(client)  # reuse the client we just proved works
        return {"ok": True}

    async def unpair(self) -> dict:
        """Forget the mini-split: stop polling and persist a disconnected state (overrides env)."""
        await self._stop_poller()
        self._save_conn({"connected": False})
        return {"ok": True}

    async def shutdown(self) -> None:
        await self._stop_poller()
