"""Small Modbus RTU codec for PD42S1 diagnostics over RS485.

The GUI uses the controller's native protocol for full functionality. These
helpers make the manual's optional Modbus register map available to scripts and
the raw terminal without adding a third-party Modbus dependency.
"""

from __future__ import annotations

import struct


def crc16(data: bytes) -> int:
    value = 0xFFFF
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = (value >> 1) ^ 0xA001 if value & 1 else value >> 1
    return value


def add_crc(payload: bytes) -> bytes:
    """Append Modbus CRC low byte first, as transmitted on the wire."""
    checksum = crc16(payload)
    return payload + struct.pack("<H", checksum)


def validate(frame: bytes) -> bool:
    return len(frame) >= 4 and crc16(frame[:-2]) == struct.unpack("<H", frame[-2:])[0]


def read_input_registers(address: int, register: int, count: int = 1) -> bytes:
    return add_crc(struct.pack(">BBHH", address, 0x04, register, count))


def write_single_register(address: int, register: int, value: int) -> bytes:
    return add_crc(struct.pack(">BBHH", address, 0x06, register, value))


def write_multiple_registers(address: int, register: int, values: list[int]) -> bytes:
    if not values:
        raise ValueError("at least one register value is required")
    body = struct.pack(">BBHHB", address, 0x10, register, len(values), len(values) * 2)
    return add_crc(body + struct.pack(f">{len(values)}H", *values))
