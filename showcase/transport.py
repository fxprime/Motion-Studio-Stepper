"""Address-aware serial transport helpers for PD42S1 V1.2."""

from __future__ import annotations

import time
from typing import Any

from pd42s1_motor import MotorController
from pd42s1_motor.protocol import PROTOCOL_FOOTER, PROTOCOL_HEADER, parse_response


INTERFACES = {"usb", "ttl", "rs485"}


def rewrite_native_address(frame: bytes, address: int) -> bytes:
    """Return a native C5...5C frame addressed to ``address``.

    The V1.2 checksum covers the header, address, function code, and data.
    """
    if not 0 <= int(address) <= 255:
        raise ValueError("address must be between 0 and 255")
    if len(frame) < 5 or frame[0] != 0xC5 or frame[-1] != 0x5C:
        return frame
    addressed = bytearray(frame)
    addressed[1] = int(address)
    addressed[-2] = sum(addressed[:-2]) & 0xFF
    return bytes(addressed)


class BusMotorController(MotorController):
    """MotorController with native-protocol addressing and optional RS485 RTS."""

    def __init__(self, port: str = "COM1", baudrate: int = 115200):
        super().__init__(port=port, baudrate=baudrate)
        self.device_address = 1
        self.interface_mode = "usb"
        self.rs485_rts = False
        self.rs485_delay_s = 0.0

    def configure_bus(
        self,
        interface_mode: str,
        device_address: int,
        rs485_rts: bool = False,
        rs485_delay_s: float = 0.0,
    ) -> None:
        if interface_mode not in INTERFACES:
            raise ValueError(f"Unsupported interface: {interface_mode}")
        if not 1 <= int(device_address) <= 255:
            raise ValueError("device address must be between 1 and 255")
        self.interface_mode = interface_mode
        self.device_address = int(device_address)
        self.rs485_rts = bool(rs485_rts)
        self.rs485_delay_s = max(0.0, min(float(rs485_delay_s), 0.1))

    def connect(self, port: str | None = None, baudrate: int | None = None) -> bool:
        connected = super().connect(port, baudrate)
        if connected and self.interface_mode == "rs485" and self.rs485_rts:
            self._enable_rs485_rts()
        return connected

    def _enable_rs485_rts(self) -> None:
        try:
            from serial.rs485 import RS485Settings

            self.serial.rs485_mode = RS485Settings(
                rts_level_for_tx=True,
                rts_level_for_rx=False,
                delay_before_tx=self.rs485_delay_s,
                delay_before_rx=self.rs485_delay_s,
            )
        except (AttributeError, OSError, ValueError) as exc:
            self.disconnect()
            raise RuntimeError(
                "This serial adapter/OS does not support pyserial RTS-controlled RS485; "
                "use an auto-direction USB-RS485 adapter or turn RTS direction off"
            ) from exc

    def send(self, cmd_bytes: bytes) -> dict[str, Any] | None:
        """Send one addressed native frame without a fixed 64-byte read delay."""
        if not self.is_connected:
            return super().send(cmd_bytes)
        request = rewrite_native_address(cmd_bytes, self.device_address)
        serial_port = self.serial
        previous_timeout = serial_port.timeout
        buffer = bytearray()
        expires_at = time.monotonic() + 0.12
        try:
            serial_port.timeout = 0
            serial_port.reset_input_buffer()
            serial_port.write(request)
            while time.monotonic() < expires_at:
                waiting = int(getattr(serial_port, "in_waiting", 0))
                if not waiting:
                    time.sleep(0.0005)
                    continue
                buffer.extend(serial_port.read(min(waiting, 128)))
                if len(buffer) > 256:
                    del buffer[:-256]
                for start in range(len(buffer)):
                    if buffer[start] != PROTOCOL_HEADER:
                        continue
                    for end in range(start + 5, len(buffer)):
                        if buffer[end] != PROTOCOL_FOOTER:
                            continue
                        raw = bytes(buffer[start:end + 1])
                        if raw == request:
                            continue
                        frame = parse_response(raw)
                        if frame is None or frame.addr != self.device_address or frame.cmd != request[2]:
                            continue
                        return {"addr": frame.addr, "cmd": frame.cmd, "status": frame.status,
                                "data": frame.data, "raw": raw}
            return None
        finally:
            serial_port.timeout = previous_timeout
