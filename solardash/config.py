"""Runtime configuration from environment variables (12-factor style).

Defaults point at a LOCAL SIMULATOR so the dashboard runs anywhere out of the box.
On the Pi at the solar site, set SOLAR_INVERTER_IP / SOLAR_INVERTER_SERIAL to the
real Solarman dongle (the serial is printed on the stick).
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Battery packs (BLE MAC, optional =name). Empty by default — set your own packs via
# SOLAR_BMS_ADDRESSES in solardash.env (comma-separated; name defaults to the MAC's last 6), e.g.
#   SOLAR_BMS_ADDRESSES=AA:BB:CC:DD:EE:01,AA:BB:CC:DD:EE:02
DEFAULT_BMS_ADDRESSES: List[Tuple[str, str]] = []

# Each pack's fixed position in the parallel group (1 = master). This is STATIC (set by wiring),
# but the BMS does not expose it to the Pi: the position rides an unsolicited broadcast that the
# phone app receives but the Pi's BlueZ stack never does (verified — see pack_broadcast.py / memory).
# So it's configured here. Read each pack's "#N" off the phone app. Override with SOLAR_BMS_POSITIONS
# ("MAC=pos,MAC=pos"). Empty = no number shown.
# Empty by default — each deployment sets its own packs' positions via SOLAR_BMS_POSITIONS in
# solardash.env, e.g. SOLAR_BMS_POSITIONS=AA:C2:37:08:25:3D=1,AA:C2:37:08:25:44=2
DEFAULT_BMS_POSITIONS: Dict[str, int] = {}


def _parse_bms_positions(spec: str) -> Dict[str, int]:
    """Parse 'MAC=pos,MAC=pos' into {MAC_UPPER: pos}. ('=' separator since MACs contain ':'.)"""
    out: Dict[str, int] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        mac, _, pos = item.partition("=")
        mac, pos = mac.strip().upper(), pos.strip()
        if mac and pos.isdigit():
            out[mac] = int(pos)
    return out


def _parse_bms_addresses(spec: str) -> List[Tuple[str, str]]:
    """Parse 'MAC=name,MAC=name' (name optional) into [(mac, name), ...]."""
    out: List[Tuple[str, str]] = []
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        mac, _, name = item.partition("=")
        mac = mac.strip()
        # Default name = the MAC's last 6 hex digits (strip colons first, e.g. ...:56:72 -> 065672).
        out.append((mac, name.strip() or mac.replace(":", "")[-6:]))
    return out


_MAC_RE = re.compile(r"^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$")


def _parse_bms_file(text: str) -> Tuple[List[Tuple[str, str]], Dict[str, int]]:
    """Parse the human-readable packs file into (addresses, positions).

    One pack per line, whitespace-separated columns:  MAC  [position]  [name]
    '#' starts a comment; blank lines are skipped. Position (an integer) and name are both
    optional; name defaults to the MAC's last 6 hex digits. A first token that isn't a MAC is
    skipped, so an un-commented header row is harmless. The whole point is legibility: on its own
    aligned line an 08-vs-0B typo is obvious — unlike jammed into one comma-separated env string.
    """
    addresses: List[Tuple[str, str]] = []
    positions: Dict[str, int] = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        mac = parts[0]
        if not _MAC_RE.match(mac):
            continue
        rest = parts[1:]
        pos: Optional[int] = None
        if rest and rest[0].isdigit():
            pos = int(rest[0])
            rest = rest[1:]
        addresses.append((mac, " ".join(rest) if rest else mac.replace(":", "")[-6:]))
        if pos is not None:
            positions[mac.upper()] = pos
    return addresses, positions


def _resolve_bms(file_path: str) -> Tuple[List[Tuple[str, str]], Dict[str, int]]:
    """Battery packs come from the packs file when it exists and lists >=1 pack (preferred — it's
    readable), otherwise the legacy comma-separated SOLAR_BMS_ADDRESSES / SOLAR_BMS_POSITIONS."""
    if file_path and os.path.exists(file_path):
        try:
            with open(file_path, encoding="utf-8") as fh:
                addrs, pos = _parse_bms_file(fh.read())
            if addrs:
                return addrs, pos
        except OSError:
            pass
    addrs = (_parse_bms_addresses(os.environ["SOLAR_BMS_ADDRESSES"])
             if os.environ.get("SOLAR_BMS_ADDRESSES") else list(DEFAULT_BMS_ADDRESSES))
    pos = (_parse_bms_positions(os.environ["SOLAR_BMS_POSITIONS"])
           if os.environ.get("SOLAR_BMS_POSITIONS") else dict(DEFAULT_BMS_POSITIONS))
    return addrs, pos


@dataclass
class Config:
    inverter_ip: str = "127.0.0.1"
    inverter_serial: int = 1234567890
    inverter_port: int = 8899
    poll_interval_s: float = 10.0
    # Remote AC-output on/off control (SRNE 0xDF00 write). OFF by default: the whole feature — API
    # endpoint and dashboard button — stays hidden until you set SOLAR_INVERTER_CONTROL=1 on the Pi.
    inverter_control_enabled: bool = False
    db_path: str = "data/solar.sqlite"
    retention_days: int = 0  # 0 = keep forever; >0 prunes samples older than N days
    # Usable battery bank capacity (kWh) for the time-to-full/empty estimate. Used only as a
    # fallback — when the BMS is connected, capacity is auto-derived from the packs' rated Ah.
    battery_capacity_kwh: float = 4.8
    # JBD BMS (BLE) bank
    bms_enabled: bool = True
    bms_interval_s: float = 60.0
    bms_addresses: List[Tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_BMS_ADDRESSES))
    bms_positions: Dict[str, int] = field(default_factory=lambda: dict(DEFAULT_BMS_POSITIONS))
    # Packs can instead be listed in a human-readable file (one per line, aligned columns) so a
    # single-char MAC typo is actually visible. Path via SOLAR_BMS_FILE; default packs.conf in the
    # working dir (beside solardash.env). If it exists and lists >=1 pack it wins over the env vars.
    bms_config_path: str = "packs.conf"
    # Mini-split appliance (EG4/Deye hybrid) over Wi-Fi — Tuya local protocol v3.3, read-only.
    # Disabled until you give it the device's LAN IP, device id, and 16-char local key (extract
    # once with `tinytuya wizard` — see README). Needs the `tinytuya` package (lazy-imported).
    appliance_enabled: bool = False
    appliance_ip: str = ""
    appliance_device_id: str = ""
    appliance_local_key: str = ""
    appliance_version: float = 3.3
    appliance_interval_s: float = 30.0   # gentle: this Wi-Fi module is flaky on the LAN
    appliance_temp_divisor: float = 1.0  # set 10 if a live status() shows temps in tenths of a degree
    # Where the UI-managed mini-split connection is persisted (JSON). Lives beside the DB (in the
    # gitignored data dir). Once written it is the source of truth; the SOLAR_APPLIANCE_* env vars
    # only seed it on first run.
    appliance_config_path: str = "data/appliance.json"

    @classmethod
    def from_env(cls) -> "Config":
        bms_file = os.environ.get("SOLAR_BMS_FILE", cls.bms_config_path)
        bms_addresses, bms_positions = _resolve_bms(bms_file)
        return cls(
            inverter_ip=os.environ.get("SOLAR_INVERTER_IP", cls.inverter_ip),
            inverter_serial=int(os.environ.get("SOLAR_INVERTER_SERIAL", cls.inverter_serial)),
            inverter_port=int(os.environ.get("SOLAR_INVERTER_PORT", cls.inverter_port)),
            inverter_control_enabled=os.environ.get("SOLAR_INVERTER_CONTROL", "0") not in ("0", "false", "False"),
            poll_interval_s=float(os.environ.get("SOLAR_POLL_INTERVAL", cls.poll_interval_s)),
            db_path=os.environ.get("SOLAR_DB_PATH", cls.db_path),
            retention_days=int(os.environ.get("SOLAR_RETENTION_DAYS", cls.retention_days)),
            battery_capacity_kwh=float(os.environ.get("SOLAR_BATTERY_CAPACITY_KWH", cls.battery_capacity_kwh)),
            bms_enabled=os.environ.get("SOLAR_BMS_ENABLED", "1") not in ("0", "false", "False"),
            bms_interval_s=float(os.environ.get("SOLAR_BMS_INTERVAL", cls.bms_interval_s)),
            bms_config_path=bms_file,
            bms_addresses=bms_addresses,
            bms_positions=bms_positions,
            appliance_enabled=os.environ.get("SOLAR_APPLIANCE_ENABLED", "0") not in ("0", "false", "False"),
            appliance_ip=os.environ.get("SOLAR_APPLIANCE_IP", cls.appliance_ip),
            appliance_device_id=os.environ.get("SOLAR_APPLIANCE_DEVICE_ID", cls.appliance_device_id),
            appliance_local_key=os.environ.get("SOLAR_APPLIANCE_LOCAL_KEY", cls.appliance_local_key),
            appliance_version=float(os.environ.get("SOLAR_APPLIANCE_VERSION", cls.appliance_version)),
            appliance_interval_s=float(os.environ.get("SOLAR_APPLIANCE_INTERVAL", cls.appliance_interval_s)),
            appliance_temp_divisor=float(os.environ.get("SOLAR_APPLIANCE_TEMP_DIVISOR", cls.appliance_temp_divisor)),
            appliance_config_path=os.environ.get("SOLAR_APPLIANCE_CONFIG", cls.appliance_config_path),
        )
