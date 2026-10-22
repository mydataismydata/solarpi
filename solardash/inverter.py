"""SRNE / Eco-Worthy hybrid-inverter holding-register map (Modbus fn 0x03) and decode.

Faithful port of the Android app's SrneInverter.kt. Register source:
danzelziggy/srne-solarman (srne_hesp.yaml) / SRNE Modbus V2.07.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# Contiguous register blocks to read as (startRegister, count). Read independently;
# a block the firmware rejects is simply skipped. PV2 is its own tight block because
# the firmware NAKs any read that runs past 0x0111.
BLOCKS: List[Tuple[int, int]] = [
    (0x0100, 0x0F),  # battery + PV1
    (0x010F, 0x03),  # PV2 voltage / current / power
    (0x0200, 0x11),  # fault bits (0x0200-03) + fault codes (0x0204-07) + machine state (0x0210)
    (0x0212, 0x1B),  # grid / output / load L1 / temps
    (0x0230, 0x05),  # load L2 current / power / apparent (split phase)
]


def _u(m: Dict[int, int], addr: int, scale: float) -> Optional[float]:
    v = m.get(addr)
    return None if v is None else v * scale


def _s(m: Dict[int, int], addr: int, scale: float) -> Optional[float]:
    v = m.get(addr)
    if v is None:
        return None
    if v & 0x8000:
        v -= 0x10000
    return v * scale


@dataclass(frozen=True)
class InverterStatus:
    """Decoded snapshot. None = register not (yet) read. Units in field comments."""

    battery_soc: Optional[int] = None            # %
    battery_voltage: Optional[float] = None      # V
    battery_current: Optional[float] = None      # A (+ charge / - discharge), sign-normalised
    battery_temp: Optional[float] = None         # degC
    pv1_voltage: Optional[float] = None          # V
    pv1_current: Optional[float] = None          # A
    pv2_voltage: Optional[float] = None          # V
    pv2_current: Optional[float] = None          # A
    grid_voltage: Optional[float] = None         # V (L1)
    grid_frequency: Optional[float] = None       # Hz
    output_voltage: Optional[float] = None       # V (L1)
    output_frequency: Optional[float] = None     # Hz
    load_power: Optional[int] = None             # W (L1)
    load_apparent: Optional[int] = None          # VA (L1)
    load_current: Optional[float] = None         # A (L1)
    load_l2_power: Optional[int] = None          # W (L2, split phase)
    load_l2_apparent: Optional[int] = None       # VA (L2, split phase)
    load_l2_current: Optional[float] = None      # A (L2, split phase)
    grid_l2_voltage: Optional[float] = None      # V (split phase)
    output_l2_voltage: Optional[float] = None    # V (split phase)
    dc_temp: Optional[float] = None              # degC
    ac_temp: Optional[float] = None              # degC
    fault_codes: List[int] = field(default_factory=list)  # active codes from 0x0204..0x0207
    machine_state: Optional[int] = None          # working state (0x0210)

    @property
    def has_fault(self) -> bool:
        return len(self.fault_codes) > 0

    @property
    def battery_power(self) -> Optional[float]:
        if self.battery_voltage is not None and self.battery_current is not None:
            return self.battery_voltage * self.battery_current
        return None

    @property
    def pv1_power(self) -> Optional[float]:
        if self.pv1_voltage is not None and self.pv1_current is not None:
            return self.pv1_voltage * self.pv1_current
        return None

    @property
    def pv2_power(self) -> Optional[float]:
        if self.pv2_voltage is not None and self.pv2_current is not None:
            return self.pv2_voltage * self.pv2_current
        return None

    @property
    def pv_power(self) -> Optional[float]:
        """Total PV power (W) across both MPPT strings, derived from V x I (the dedicated
        total-power register reads 0 on this unit)."""
        a, b = self.pv1_power, self.pv2_power
        if a is not None and b is not None:
            return a + b
        return a if a is not None else b

    @property
    def load_total(self) -> Optional[int]:
        """Total real load (W) across both split-phase legs; falls back to whichever is present."""
        if self.load_power is not None and self.load_l2_power is not None:
            return self.load_power + self.load_l2_power
        return self.load_power if self.load_power is not None else self.load_l2_power

    @property
    def load_apparent_total(self) -> Optional[int]:
        if self.load_apparent is not None and self.load_l2_apparent is not None:
            return self.load_apparent + self.load_l2_apparent
        return self.load_apparent if self.load_apparent is not None else self.load_l2_apparent

    @property
    def has_data(self) -> bool:
        return (
            self.battery_soc is not None
            or self.battery_voltage is not None
            or self.pv_power is not None
            or self.load_total is not None
        )


def decode(m: Dict[int, int]) -> InverterStatus:
    """Decode a register map (addr -> 16-bit value) into an InverterStatus."""
    return InverterStatus(
        battery_soc=m.get(0x0100),
        battery_voltage=_u(m, 0x0101, 0.1),
        # This unit reports battery current as +discharge / -charge (opposite of the app's
        # "+ charge / - discharge" convention), so negate to normalise.
        battery_current=_s(m, 0x0102, -0.1),
        battery_temp=_s(m, 0x0103, 0.1),
        pv1_voltage=_u(m, 0x0107, 0.1),
        pv1_current=_u(m, 0x0108, 0.1),
        pv2_voltage=_u(m, 0x010F, 0.1),
        pv2_current=_u(m, 0x0110, 0.1),
        grid_voltage=_u(m, 0x0213, 0.1),
        grid_frequency=_u(m, 0x0215, 0.01),
        output_voltage=_u(m, 0x0216, 0.1),
        output_frequency=_u(m, 0x0218, 0.01),
        load_power=m.get(0x021B),
        load_apparent=m.get(0x021C),
        load_current=_u(m, 0x0219, 0.1),
        load_l2_current=_u(m, 0x0230, 0.1),
        load_l2_power=m.get(0x0232),
        load_l2_apparent=m.get(0x0234),
        grid_l2_voltage=_u(m, 0x022A, 0.1),
        output_l2_voltage=_u(m, 0x022C, 0.1),
        dc_temp=_u(m, 0x0220, 0.1),
        ac_temp=_u(m, 0x0221, 0.1),
        fault_codes=[m[a] for a in (0x0204, 0x0205, 0x0206, 0x0207) if m.get(a)],
        machine_state=m.get(0x0210),
    )


def has_core(raw: Dict[int, int]) -> bool:
    """Liveness gate: a read whose pack voltage (0x0101) is absent/zero is a partial/bogus
    reply (a dropped block from the WiFi dongle) and must not overwrite good data."""
    return raw.get(0x0101, 0) > 0


# Unsigned load power / apparent-power registers (L1 W, L1 VA, L2 W, L2 VA). A value with the top
# bit set (32.8 kW or more) is far past anything the inverter can deliver, so the block it came in
# is garbled. On 27 Jun 2026 one reply read L2 power as 65,204 W (0xFEB4) and L2 apparent as 0.
LOAD_POWER_REGS = (0x021B, 0x021C, 0x0232, 0x0234)


def drop_garbled(fresh: Dict[int, int]) -> Dict[int, int]:
    """Remove every block of `fresh` that holds an impossible load power, so merge() carries the
    previous values forward as it does for a block the read missed."""
    out = dict(fresh)
    for start, count in BLOCKS:
        block = range(start, start + count)
        if any(fresh.get(a, 0) & 0x8000 for a in LOAD_POWER_REGS if a in block):
            for a in block:
                out.pop(a, None)
    return out


def merge(previous: Optional[Dict[int, int]], fresh: Dict[int, int]) -> Dict[int, int]:
    """Overlay `fresh` registers onto `previous`, carrying forward any register a partial read
    missed or that came in a garbled block (drop_garbled). Registers returned this cycle
    (including legitimate zeros, e.g. PV at night) win."""
    out: Dict[int, int] = {}
    if previous:
        out.update(previous)
    out.update(drop_garbled(fresh))
    return out
