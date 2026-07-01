"""Pure decode of the EG4/Deye hybrid mini-split's Tuya datapoints (DPs) into named fields.

No tinytuya import here, so this unit-tests on a bare Python like inverter.py / bms_client.py do.
The network read lives in appliance_client.py.

The EG4 24K BTU Hybrid Solar Mini-Split (AC/DC, R32) is a rebadged Deye unit and a Tuya v3.3
device; its "Solar Aircon" app is a Smart Life reskin. The DP numbers below come from the
community reverse-engineering of this exact unit (see README). DPs 106-111 (the solar/grid power
and energy split) are only exposed on the LAN — the Tuya cloud API hides them.

    DP   code                meaning
    1    switch              power on/off (bool)
    2    temp_set            setpoint, deg C
    3    temp_current        room temperature, deg C (signed)
    4    mode                operating mode enum (auto / cool / heat / fan_only ... raw passthrough)
    19   temp_set_f          setpoint, deg F
    20   temp_current_f      room temperature, deg F
    22   work_status         off / cooling / heating / ventilation (raw passthrough)
    23   fan_speed_enum      auto / low / medium / high (raw passthrough)
    24   fault               fault bitmap (bit0 sensor_fault, bit1 temp_fault)
    106  solar_power         PV input power, W            <- LAN-only
    107  solar_energy        cumulative PV energy, Wh     <- LAN-only
    108  solar_percent       share of load from PV, %     <- LAN-only
    109  grid_percent        share of load from grid, %   <- LAN-only
    110  total_energy        cumulative total energy, Wh  <- LAN-only
    111  grid_power          grid/AC input power, W       <- LAN-only

Temperature scale: most firmware reports whole degrees, but some report tenths. The raw DP values
are always preserved (the client keeps the full dps dict), and the deg-C fields here are divided by
`temp_divisor` (default 1.0; set SOLAR_APPLIANCE_TEMP_DIVISOR=10 for a tenths unit) so a scale
quirk is a config change, not a code change. Confirm the scale + the enum strings against a live
`tinytuya` status() snapshot when commissioning.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

# DP keys are strings in tinytuya's status() dict ({'dps': {'1': True, ...}}).
DP_SWITCH = "1"
DP_TEMP_SET_C = "2"
DP_TEMP_CURRENT_C = "3"
DP_MODE = "4"
DP_TEMP_SET_F = "19"
DP_TEMP_CURRENT_F = "20"
DP_WORK_STATUS = "22"
DP_FAN_SPEED = "23"
DP_FAULT = "24"
DP_SOLAR_POWER = "106"
DP_SOLAR_ENERGY = "107"
DP_SOLAR_PERCENT = "108"
DP_GRID_PERCENT = "109"
DP_TOTAL_ENERGY = "110"
DP_GRID_POWER = "111"

# Bit positions in the DP 24 fault bitmap (when it arrives as an integer).
FAULT_BITS = {0: "sensor_fault", 1: "temp_fault"}

# Fault DP values that mean "no fault" when it arrives as a string/enum.
_FAULT_CLEAR = {"", "0", "none", "no_fault", "nofault", "normal", "ok"}


@dataclass
class ApplianceStatus:
    power: Optional[bool] = None
    mode: Optional[str] = None
    work_status: Optional[str] = None
    fan_speed: Optional[str] = None
    temp_set_c: Optional[float] = None
    temp_current_c: Optional[float] = None
    temp_set_f: Optional[int] = None
    temp_current_f: Optional[int] = None
    fault_labels: List[str] = field(default_factory=list)
    solar_power: Optional[float] = None
    grid_power: Optional[float] = None
    solar_energy: Optional[float] = None
    total_energy: Optional[float] = None
    solar_percent: Optional[int] = None
    grid_percent: Optional[int] = None

    @property
    def has_fault(self) -> bool:
        return bool(self.fault_labels)


def _num(v: object) -> Optional[float]:
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _int(v: object) -> Optional[int]:
    n = _num(v)
    return int(round(n)) if n is not None else None


def _str(v: object) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _bool(v: object) -> Optional[bool]:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v != 0
    s = str(v).strip().lower()
    if s in ("true", "on", "1"):
        return True
    if s in ("false", "off", "0"):
        return False
    return None


def _fault_labels(v: object) -> List[str]:
    """Decode the DP 24 fault value: an int bitmap, or a string enum, into label(s)."""
    if v is None or isinstance(v, bool):
        return []
    if isinstance(v, int):
        if v == 0:
            return []
        hits = [name for bit, name in FAULT_BITS.items() if v & (1 << bit)]
        return hits or [f"fault_{v}"]
    s = str(v).strip()
    return [] if s.lower() in _FAULT_CLEAR else [s]


def _scaled_c(v: object, divisor: float) -> Optional[float]:
    n = _num(v)
    if n is None:
        return None
    return round(n / divisor, 1) if divisor and divisor != 1.0 else n


def decode(dps: Dict[str, object], temp_divisor: float = 1.0) -> ApplianceStatus:
    """Map a Tuya dps dict to an ApplianceStatus. Missing DPs stay None (firmware varies)."""
    return ApplianceStatus(
        power=_bool(dps.get(DP_SWITCH)),
        mode=_str(dps.get(DP_MODE)),
        work_status=_str(dps.get(DP_WORK_STATUS)),
        fan_speed=_str(dps.get(DP_FAN_SPEED)),
        temp_set_c=_scaled_c(dps.get(DP_TEMP_SET_C), temp_divisor),
        temp_current_c=_scaled_c(dps.get(DP_TEMP_CURRENT_C), temp_divisor),
        temp_set_f=_int(dps.get(DP_TEMP_SET_F)),
        temp_current_f=_int(dps.get(DP_TEMP_CURRENT_F)),
        fault_labels=_fault_labels(dps.get(DP_FAULT)),
        solar_power=_num(dps.get(DP_SOLAR_POWER)),
        grid_power=_num(dps.get(DP_GRID_POWER)),
        solar_energy=_num(dps.get(DP_SOLAR_ENERGY)),
        total_energy=_num(dps.get(DP_TOTAL_ENERGY)),
        solar_percent=_int(dps.get(DP_SOLAR_PERCENT)),
        grid_percent=_int(dps.get(DP_GRID_PERCENT)),
    )
