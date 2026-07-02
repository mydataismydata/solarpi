"""Wire codec for the SRNE inverter: CRC-16/Modbus, Modbus-RTU (function 0x03),
and Solarman V5 framing for the IGEN/Solarman WiFi data-logger stick (TCP 8899).

Faithful port of the Android app's ModbusRtu.kt / SolarmanV5.kt, pinned to the
same reference frames as tools/verify_inverter_codec.py in the EcoUnworthy repo.
All Modbus values are big-endian; the Solarman V5 envelope is little-endian.
"""
from __future__ import annotations

from typing import List, Optional

V5_START = 0xA5
V5_END = 0x15
MODBUS_READ_HOLDING = 0x03
MODBUS_WRITE_SINGLE = 0x06


def crc16_modbus(data: bytes) -> int:
    """CRC-16/Modbus (poly 0xA001, init 0xFFFF), returned as a 16-bit int."""
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def modbus_read_request(slave: int, start_reg: int, count: int) -> bytes:
    """Build a Modbus-RTU 'read holding registers' request (CRC appended low byte first)."""
    body = bytes(
        [
            slave & 0xFF,
            MODBUS_READ_HOLDING,
            (start_reg >> 8) & 0xFF,
            start_reg & 0xFF,
            (count >> 8) & 0xFF,
            count & 0xFF,
        ]
    )
    crc = crc16_modbus(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def modbus_parse_holding(frame: bytes, expected_slave: int) -> Optional[List[int]]:
    """Parse a fn-0x03 response `[slave][0x03][byteCount][data...][crcLo][crcHi]`.

    Returns one int per 16-bit register, or None on any malformed / wrong-slave /
    bad-CRC frame (a 0x83 function byte signals a Modbus exception -> rejected).
    """
    if len(frame) < 5:
        return None
    if frame[0] != expected_slave:
        return None
    if frame[1] != MODBUS_READ_HOLDING:
        return None
    byte_count = frame[2]
    crc_start = 3 + byte_count
    if len(frame) < crc_start + 2:
        return None
    got = frame[crc_start] | (frame[crc_start + 1] << 8)
    if got != crc16_modbus(frame[:crc_start]):
        return None
    return [(frame[3 + 2 * i] << 8) | frame[3 + 2 * i + 1] for i in range(byte_count // 2)]


def modbus_write_single(slave: int, reg: int, value: int) -> bytes:
    """Build a Modbus-RTU 'write single register' (fn 0x06) request `[slave][0x06][regHi][regLo]
    [valHi][valLo][crcLo][crcHi]`. The SRNE inverter supports fn 0x06 (protocol V2.07 sec. 3)."""
    body = bytes(
        [
            slave & 0xFF,
            MODBUS_WRITE_SINGLE,
            (reg >> 8) & 0xFF,
            reg & 0xFF,
            (value >> 8) & 0xFF,
            value & 0xFF,
        ]
    )
    crc = crc16_modbus(body)
    return body + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def modbus_parse_write_echo(frame: bytes, expected_slave: int, expected_reg: int) -> Optional[int]:
    """Validate a fn-0x06 write echo. On success the inverter mirrors the request verbatim, so
    return the written value; return None on any malformed / wrong-slave / wrong-register / bad-CRC
    frame (a 0x86 function byte signals a Modbus exception -> rejected)."""
    if len(frame) < 8:
        return None
    if frame[0] != expected_slave:
        return None
    if frame[1] != MODBUS_WRITE_SINGLE:
        return None
    if ((frame[2] << 8) | frame[3]) != expected_reg:
        return None
    got = frame[6] | (frame[7] << 8)
    if got != crc16_modbus(frame[:6]):
        return None
    return (frame[4] << 8) | frame[5]


def v5_encode(serial: int, sequence: int, modbus_frame: bytes) -> bytes:
    """Wrap a Modbus frame in a Solarman V5 request for data-logger `serial`.

    Layout: A5 | len(2,LE) | control 0x4510 | seq(2,LE) | serial(4,LE) |
            frameType 0x02 | sensorType(2) | 12x time bytes | modbus | checksum | 15
    `len` counts only the data field; checksum = sum(bytes[1:checksum]) & 0xFF.
    """
    payload_len = 15 + len(modbus_frame)  # frameType(1)+sensorType(2)+3x time(4) + modbus
    out = [
        V5_START,
        payload_len & 0xFF,
        (payload_len >> 8) & 0xFF,
        0x10,
        0x45,  # control 0x4510 (LE)
        sequence & 0xFF,
        (sequence >> 8) & 0xFF,
        serial & 0xFF,
        (serial >> 8) & 0xFF,
        (serial >> 16) & 0xFF,
        (serial >> 24) & 0xFF,
        0x02,  # frame type: solar inverter
        0x00,
        0x00,  # sensor type
    ]
    out += [0x00] * 12  # total working + power-on + offset times
    out += list(modbus_frame)
    out.append(sum(out[1:]) & 0xFF)  # checksum
    out.append(V5_END)
    return bytes(out)


def v5_decode(frame: bytes) -> Optional[bytes]:
    """Validate a Solarman V5 response (control 0x1510) and return its inner Modbus frame."""
    if len(frame) < 13 or frame[0] != V5_START:
        return None
    payload_len = frame[1] | (frame[2] << 8)
    total = 13 + payload_len
    if len(frame) < total or frame[total - 1] != V5_END:
        return None
    if (sum(frame[1 : total - 2]) & 0xFF) != frame[total - 2]:
        return None
    if frame[3] != 0x10 or frame[4] != 0x15:  # response control 0x1510
        return None
    mb = frame[25 : total - 2]
    return mb or None


# --------------------------------------------------------------------------- #
# Response-side framing — the inverse direction, used by the offline simulator
# (sim/inverter_sim.py) to impersonate the Solarman dongle. Round-trip tested
# against the client-side decoders above.
# --------------------------------------------------------------------------- #


def v5_decode_request(frame: bytes) -> Optional[bytes]:
    """Validate a Solarman V5 *request* (control 0x4510) and return its inner Modbus frame.

    The request layout differs from the response by one byte of preamble, so the inner
    Modbus payload sits at offset 26 (vs 25 for responses)."""
    if len(frame) < 13 or frame[0] != V5_START:
        return None
    payload_len = frame[1] | (frame[2] << 8)
    total = 13 + payload_len
    if len(frame) < total or frame[total - 1] != V5_END:
        return None
    if (sum(frame[1 : total - 2]) & 0xFF) != frame[total - 2]:
        return None
    if frame[3] != 0x10 or frame[4] != 0x45:  # request control 0x4510
        return None
    mb = frame[26 : total - 2]
    return mb or None


def modbus_holding_response(slave: int, registers: List[int]) -> bytes:
    """Build a Modbus-RTU fn-0x03 response `[slave][0x03][byteCount][data...][crcLo][crcHi]`."""
    body = bytearray([slave & 0xFF, MODBUS_READ_HOLDING, (len(registers) * 2) & 0xFF])
    for r in registers:
        body += bytes([(r >> 8) & 0xFF, r & 0xFF])
    crc = crc16_modbus(bytes(body))
    return bytes(body) + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def v5_encode_response(serial: int, sequence: int, modbus_frame: bytes, status: int = 0x01) -> bytes:
    """Wrap a Modbus response in a Solarman V5 *response* envelope (control 0x1510).

    Mirrors a real dongle reply: the inner Modbus payload lands at offset 25 so the
    client's v5_decode() recovers it."""
    payload_len = 14 + len(modbus_frame)  # frameType(1)+status(1)+12 preamble + modbus
    out = [
        V5_START,
        payload_len & 0xFF,
        (payload_len >> 8) & 0xFF,
        0x10,
        0x15,  # control 0x1510 (response)
        sequence & 0xFF,
        (sequence >> 8) & 0xFF,
        serial & 0xFF,
        (serial >> 8) & 0xFF,
        (serial >> 16) & 0xFF,
        (serial >> 24) & 0xFF,
        0x02,  # frame type
        status & 0xFF,
    ]
    out += [0x00] * 12  # preamble padding -> modbus starts at index 25
    out += list(modbus_frame)
    out.append(sum(out[1:]) & 0xFF)  # checksum
    out.append(V5_END)
    return bytes(out)
