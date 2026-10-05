"""High-rate PD42S1 status acquisition and fan-out."""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from pd42s1_motor import commands as motor_commands
from pd42s1_motor.protocol import (
    PROTOCOL_ADDR,
    PROTOCOL_FOOTER,
    PROTOCOL_HEADER,
    parse_response,
)
from pd42s1_motor.unpack import (
    unpack_float_be,
    unpack_int16_be,
    unpack_int32_be,
    unpack_uint32_be,
)

from .transport import rewrite_native_address


STATUS_FRAME_BYTES = 46
STATUS_DATA_BYTES = 41


def decode_status_data(data: bytes) -> dict[str, Any]:
    """Decode the status byte plus the 40-byte 0x31 payload."""
    if len(data) != STATUS_DATA_BYTES:
        raise ValueError(f"Expected {STATUS_DATA_BYTES} status bytes, received {len(data)}")
    return {
        "voltage": unpack_float_be(data, 1),
        "current": unpack_int16_be(data, 5),
        "flux": unpack_float_be(data, 7),
        "resistance": unpack_float_be(data, 11),
        "inductance": unpack_float_be(data, 15),
        "speed": unpack_int16_be(data, 19),
        "target_pos": unpack_int32_be(data, 21),
        "actual_pos": unpack_int32_be(data, 25),
        "position_error": unpack_int32_be(data, 29),
        "pulse_count": unpack_uint32_be(data, 33),
        "enabled": data[37],
        "in_position": data[38],
        "stalled": data[39],
        "address_mode": data[40],
        "motion": int(unpack_int16_be(data, 19) != 0),
    }


def _find_status_frame(buffer: bytearray, expected_address: int = PROTOCOL_ADDR) -> bytes | None:
    """Find a checksum-valid fixed-size 0x31 frame, ignoring an RS485 echo."""
    last_start = len(buffer) - STATUS_FRAME_BYTES
    for start in range(max(0, last_start + 1)):
        if buffer[start] != PROTOCOL_HEADER:
            continue
        candidate = bytes(buffer[start:start + STATUS_FRAME_BYTES])
        if len(candidate) != STATUS_FRAME_BYTES or candidate[-1] != PROTOCOL_FOOTER:
            continue
        frame = parse_response(candidate)
        if (
            frame is not None
            and frame.addr == expected_address
            and frame.cmd == 0x31
            and len(frame.data) == STATUS_DATA_BYTES
        ):
            return candidate
    return None


def read_all_status_fast(motor: Any, deadline_s: float = 0.018) -> dict[str, Any] | None:
    """Read one complete status frame without waiting for a 64-byte timeout.

    A test double without a pyserial object falls back to its high-level method.
    """
    serial_port = getattr(motor, "serial", None)
    if serial_port is None:
        return motor.read_all_status()

    address = int(getattr(motor, "device_address", PROTOCOL_ADDR))
    request = rewrite_native_address(motor_commands.read_all_status(), address)
    previous_timeout = serial_port.timeout
    buffer = bytearray()
    expires_at = time.monotonic() + deadline_s
    try:
        serial_port.timeout = 0
        serial_port.reset_input_buffer()
        serial_port.write(request)
        while time.monotonic() < expires_at:
            waiting = int(getattr(serial_port, "in_waiting", 0))
            if waiting:
                buffer.extend(serial_port.read(min(waiting, 128)))
                if len(buffer) > 256:
                    del buffer[:-256]
                raw = _find_status_frame(buffer, address)
                if raw is not None:
                    frame = parse_response(raw)
                    return decode_status_data(frame.data) if frame is not None else None
            else:
                time.sleep(0.0005)
        return None
    finally:
        serial_port.timeout = previous_timeout


class TelemetryService:
    """Acquire status at a fixed cadence and retain a bounded sample history."""

    def __init__(
        self,
        motor_lock: threading.RLock,
        command_pending: threading.Event,
        target_hz: float = 50.0,
        history_size: int = 512,
    ):
        self.motor_lock = motor_lock
        self.command_pending = command_pending
        self.target_hz = target_hz
        self.period_s = 1.0 / target_hz
        self.history: deque[dict[str, Any]] = deque(maxlen=history_size)
        self.condition = threading.Condition()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.motor: Any = None
        self.sequence = 0
        self.attempts = 0
        self.dropouts = 0
        self.started_ns = 0
        self.latest: dict[str, Any] | None = None

    @property
    def running(self) -> bool:
        return bool(self.thread and self.thread.is_alive())

    def start(self, motor: Any) -> None:
        self.stop()
        with self.condition:
            self.motor = motor
            self.history.clear()
            self.sequence = 0
            self.attempts = 0
            self.dropouts = 0
            self.latest = None
            self.started_ns = time.monotonic_ns()
            self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="pd42s1-telemetry", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        with self.condition:
            self.condition.notify_all()
        thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self.thread = None
        self.motor = None

    def stats(self) -> dict[str, Any]:
        elapsed = max(0.001, (time.monotonic_ns() - self.started_ns) / 1_000_000_000) if self.started_ns else 0
        return {
            "targetHz": self.target_hz,
            "effectiveHz": round(self.sequence / elapsed, 2) if elapsed else 0,
            "attempts": self.attempts,
            "dropouts": self.dropouts,
            "sequence": self.sequence,
        }

    def wait_after(self, sequence: int, timeout: float = 1.0) -> list[dict[str, Any]]:
        with self.condition:
            self.condition.wait_for(
                lambda: self.sequence > sequence or self.stop_event.is_set(),
                timeout=timeout,
            )
            return [sample for sample in self.history if sample["sequence"] > sequence]

    def wait_for_first(self, timeout: float = 0.25) -> dict[str, Any] | None:
        with self.condition:
            self.condition.wait_for(lambda: self.latest is not None or self.stop_event.is_set(), timeout=timeout)
            return self.latest

    def _run(self) -> None:
        next_tick = time.monotonic()
        while not self.stop_event.is_set():
            delay = next_tick - time.monotonic()
            if delay > 0 and self.stop_event.wait(delay):
                break
            tick_started = time.monotonic()
            next_tick += self.period_s
            if tick_started - next_tick > self.period_s:
                next_tick = tick_started + self.period_s

            self.attempts += 1
            status = None
            if not self.command_pending.is_set() and self.motor_lock.acquire(blocking=False):
                try:
                    if self.motor is not None and getattr(self.motor, "is_connected", False):
                        status = read_all_status_fast(self.motor, min(0.018, self.period_s * 0.9))
                finally:
                    self.motor_lock.release()
            if status is None:
                self.dropouts += 1
                continue

            timestamp_ns = time.monotonic_ns()
            with self.condition:
                self.sequence += 1
                sample = {
                    "sequence": self.sequence,
                    "monotonicNs": timestamp_ns,
                    "status": status,
                }
                self.latest = sample
                self.history.append(sample)
                self.condition.notify_all()
