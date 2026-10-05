"""Local HTTP API and static-file server for a PD42S1 motor controller."""

from __future__ import annotations

import argparse
import json
import math
import mimetypes
import struct
import threading
import time
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import unquote, urlparse

from .telemetry import TelemetryService, read_all_status_fast
from .transport import BusMotorController, INTERFACES

try:
    from pd42s1_motor import MotorController, PD42S1Error
    from pd42s1_motor import commands as motor_commands
    from pd42s1_motor.protocol import pack_frame
except ImportError as exc:  # pragma: no cover - exercised by a clean user install
    raise SystemExit(
        "pd42s1_motor is not installed. Run: pip install -e ../pd42s1-motor"
    ) from exc


WEB_ROOT = Path(__file__).with_name("web")
ALLOWED_BAUDS = {9600, 19200, 38400, 57600, 115200, 230400, 460800, 921600}
ADVANCED_CONTROL_CODES = {
    "pwm-position": 0xF5,
    "pwm-torque": 0xF6,
    "pwm-speed": 0xF7,
    "open-speed": 0xE0,
    "open-absolute": 0xE1,
    "open-relative": 0xE2,
    "open-pulse": 0xE3,
}


class ApiError(Exception):
    def __init__(self, message: str, status: int = HTTPStatus.BAD_REQUEST):
        super().__init__(message)
        self.status = status


def _finite_number(value: Any, name: str, minimum: float, maximum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(f"{name} must be a number") from exc
    if not math.isfinite(number) or not minimum <= number <= maximum:
        raise ApiError(f"{name} must be between {minimum:g} and {maximum:g}")
    return number


def advanced_control_frame(body: dict[str, Any]) -> bytes:
    """Build an advanced-control native frame from manual V1.2 sections 4.4.6-4.4.17."""
    mode = str(body.get("mode", ""))
    if mode not in ADVANCED_CONTROL_CODES:
        raise ApiError("Unsupported advanced control mode")
    function_code = ADVANCED_CONTROL_CODES[mode]
    if mode.startswith("pwm-"):
        max_pulse = int(_finite_number(body.get("maxPulseUs"), "maxPulseUs", 0, 50_000))
        min_pulse = int(_finite_number(body.get("minPulseUs"), "minPulseUs", 0, 50_000))
        if max_pulse <= min_pulse:
            raise ApiError("maxPulseUs must be greater than minPulseUs")
        if mode == "pwm-position":
            minimum, maximum = -2_147_483_648, 2_147_483_647
        elif mode == "pwm-speed":
            minimum, maximum = 0, 6000
        else:
            minimum, maximum = 0, 3000
        max_value = int(_finite_number(body.get("maxValue"), "maxValue", minimum, maximum))
        min_value = int(_finite_number(body.get("minValue"), "minValue", minimum, maximum))
        return pack_frame(function_code, struct.pack(">HHii", max_pulse, min_pulse, max_value, min_value))
    if mode == "open-pulse":
        return pack_frame(function_code)
    direction = int(_finite_number(body.get("direction"), "direction", 0, 1))
    acceleration = int(_finite_number(body.get("acceleration"), "acceleration", 0, 200))
    if mode == "open-speed":
        speed = _finite_number(body.get("speedRpm"), "speedRpm", 0.1, 400)
        return pack_frame(function_code, struct.pack(">BBf", direction, acceleration, speed))
    speed = int(_finite_number(body.get("speedRpm"), "speedRpm", 0, 400))
    position = int(_finite_number(body.get("position"), "position", 0, 4_294_967_295))
    return pack_frame(function_code, struct.pack(">BBHI", direction, acceleration, speed, position))


def response_metrics(samples: list[dict[str, Any]], target_rpm: float) -> dict[str, Any]:
    """Calculate compact step-response metrics from timestamped RPM samples."""
    if not samples or target_rpm == 0:
        return {"peakRpm": None, "overshootPct": None, "riseTimeS": None, "settlingTimeS": None}
    sign = 1 if target_rpm > 0 else -1
    normalized = [(float(item["t"]), sign * float(item["speed"])) for item in samples]
    target = abs(target_rpm)
    peak = max((speed for _, speed in normalized), default=0.0)
    overshoot = max(0.0, (peak - target) / target * 100.0)

    def first_crossing(level: float) -> float | None:
        return next((t for t, speed in normalized if speed >= level), None)

    t10 = first_crossing(target * 0.1)
    t90 = first_crossing(target * 0.9)
    rise = t90 - t10 if t10 is not None and t90 is not None and t90 >= t10 else None
    tolerance = target * 0.05
    settling = None
    for index, (timestamp, _) in enumerate(normalized):
        if all(abs(speed - target) <= tolerance for _, speed in normalized[index:]):
            settling = timestamp
            break
    return {
        "peakRpm": round(sign * peak, 3),
        "overshootPct": round(overshoot, 3),
        "riseTimeS": None if rise is None else round(rise, 3),
        "settlingTimeS": None if settling is None else round(settling, 3),
    }


def _transition_metrics(samples: list[dict[str, Any]], target_rpm: float,
                        start_rpm: float) -> dict[str, float | None]:
    """Measure one arbitrary rising or falling velocity transition."""
    delta = float(target_rpm) - float(start_rpm)
    if not samples or abs(delta) < 1e-9:
        return {"overshootPct": None, "riseTimeS": None, "settlingTimeS": None}
    first_timestamp = float(samples[0]["t"])
    normalized = [
        (float(sample["t"]) - first_timestamp,
         (float(sample["speed"]) - float(start_rpm)) / delta)
        for sample in samples
    ]

    def first_crossing(level: float) -> float | None:
        return next((timestamp for timestamp, progress in normalized if progress >= level), None)

    t10 = first_crossing(0.1)
    t90 = first_crossing(0.9)
    rise = t90 - t10 if t10 is not None and t90 is not None and t90 >= t10 else None
    overshoot = max(0.0, (max(progress for _, progress in normalized) - 1.0) * 100.0)
    settling = None
    for index, (timestamp, _) in enumerate(normalized):
        if all(abs(progress - 1.0) <= 0.05 for _, progress in normalized[index:]):
            settling = timestamp
            break
    return {
        "overshootPct": round(overshoot, 3),
        "riseTimeS": None if rise is None else round(rise, 3),
        "settlingTimeS": None if settling is None else round(settling, 3),
    }


def sequence_response_metrics(samples: list[dict[str, Any]]) -> dict[str, Any]:
    """Return worst-case step metrics across a multi-level velocity sequence."""
    if not samples:
        return {
            "peakRpm": None, "overshootPct": None, "riseTimeS": None,
            "settlingTimeS": None, "transitionCount": 0, "aggregation": "worst-case",
        }
    segments: dict[int, list[dict[str, Any]]] = {}
    for sample in samples:
        segments.setdefault(int(sample.get("stepIndex", 0)), []).append(sample)
    transitions = []
    previous_speed = 0.0
    for step_index in sorted(segments):
        segment = segments[step_index]
        target = float(segment[0].get("target", 0))
        start_speed = float(segment[0].get("startSpeed", previous_speed))
        metrics = _transition_metrics(segment, target, start_speed)
        transitions.append({"stepIndex": step_index, "targetRpm": target, **metrics})
        previous_speed = float(segment[-1].get("speed", target))

    peak_sample = max(samples, key=lambda sample: abs(float(sample.get("speed", 0))))
    count = len(transitions)

    def worst_complete(key: str) -> float | None:
        values = [transition[key] for transition in transitions if transition[key] is not None]
        return max(values) if len(values) == count and values else None

    overshoots = [transition["overshootPct"] for transition in transitions
                  if transition["overshootPct"] is not None]
    return {
        "peakRpm": round(float(peak_sample.get("speed", 0)), 3),
        "overshootPct": max(overshoots) if overshoots else None,
        "riseTimeS": worst_complete("riseTimeS"),
        "settlingTimeS": worst_complete("settlingTimeS"),
        "transitionCount": count,
        "aggregation": "worst-case",
        "transitions": transitions,
    }


def _reply_data(reply: dict[str, Any] | None) -> bytes:
    if not reply or reply.get("status") != 0x01:
        return b""
    data = bytes(reply.get("data", b""))
    return data[1:] if data and data[0] == 0x01 else b""


def parse_version_reply(reply: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode the V1.2 0x20 product/hardware/firmware response."""
    data = _reply_data(reply)
    if len(data) < 2:
        return None
    display_version = lambda value: f"V{value // 10}.{value % 10}"
    return {
        "product": "PD42S1",
        "firmware": display_version(data[0]),
        "hardware": display_version(data[1]),
    }


def parse_driver_parameters(reply: dict[str, Any] | None) -> dict[str, Any] | None:
    """Decode the documented 44-byte payload following the 0x32 status byte."""
    data = _reply_data(reply)
    if len(data) < 44:
        return None
    return {
        "workMode": data[0], "echoEnabled": bool(data[1]),
        "serialBaud": struct.unpack_from(">I", data, 2)[0],
        "canBaud": struct.unpack_from(">H", data, 6)[0],
        "directionInverted": bool(data[8]), "enableInputInverted": bool(data[9]),
        "microstep": struct.unpack_from(">H", data, 10)[0],
        "maxTorqueMa": struct.unpack_from(">H", data, 12)[0],
        "positionPid": {"kp": struct.unpack_from(">I", data, 14)[0], "ki": struct.unpack_from(">I", data, 18)[0], "kd": struct.unpack_from(">I", data, 22)[0]},
        "stallCurrentMa": struct.unpack_from(">H", data, 26)[0],
        "stallProtection": bool(data[28]), "keyLocked": bool(data[29]),
        "speedPid": {"kp": struct.unpack_from(">I", data, 30)[0], "ki": struct.unpack_from(">I", data, 34)[0], "kd": struct.unpack_from(">I", data, 38)[0]},
        "autoScreenOff": bool(data[42]), "ioStartsMotor": bool(data[43]),
    }


@dataclass
class AppState:
    controller_factory: Callable[..., MotorController] = BusMotorController
    motor: MotorController | None = None
    lock: threading.RLock = field(default_factory=threading.RLock)
    command_pending: threading.Event = field(default_factory=threading.Event)
    telemetry: TelemetryService = field(init=False)
    connected_port: str | None = None
    baud: int = 115200
    interface_mode: str = "usb"
    device_address: int = 1
    rs485_rts: bool = False
    rs485_delay_ms: float = 0.0
    settings: dict[str, Any] = field(default_factory=lambda: {
        "acceleration": 50,
        "speedRpm": 200,
        "currentMa": 150,
        "microstep": 16,
        "stepsPerRevolution": 51200,
        "autoDisableSeconds": 300,
        "autoDisablePositionError": 100,
        "workMode": 0,
        "maxTorqueMa": 1500,
        "stallProtection": True,
        "stallCurrentMa": 1500,
        "pid": {"kp": 500, "ki": 50, "kd": 200},
    })
    last_status: dict[str, Any] | None = None
    last_error: str | None = None
    device_info: dict[str, Any] = field(default_factory=dict)
    tuning: dict[str, Any] = field(default_factory=lambda: {
        "running": False, "targetRpm": 0, "samples": [], "metrics": None, "message": "Ready"
    })
    _tuning_stop: threading.Event = field(default_factory=threading.Event)
    _idle_stop: threading.Event = field(default_factory=threading.Event)
    _idle_wakeup: threading.Event = field(default_factory=threading.Event)
    _idle_thread: threading.Thread | None = field(init=False, default=None)
    _last_operation_ns: int = field(default_factory=time.monotonic_ns)
    _auto_disable_event_sequence: int = 0
    _auto_disable_message: str | None = None
    _auto_disable_success: bool | None = None
    _enable_protocol_variant: str | None = None
    _enabled_override: bool | None = None
    _enable_state_source: str = "aggregate-0x31"
    _advanced_control: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.telemetry = TelemetryService(self.lock, self.command_pending, target_hz=50.0)
        self._idle_thread = threading.Thread(
            target=self._idle_loop,
            name="pd42s1-auto-disable",
            daemon=True,
        )
        self._idle_thread.start()

    @property
    def connected(self) -> bool:
        return bool(self.motor and self.motor.is_connected)

    def public_state(self) -> dict[str, Any]:
        enabled = None
        current_status = None
        if self.telemetry.latest is not None:
            current_status = self.telemetry.latest["status"]
        elif self.last_status is not None:
            current_status = self.last_status
        if current_status is not None:
            enabled = current_status.get("enabled") == 0
        if self._enabled_override is not None:
            enabled = self._enabled_override
        tuning = {key: value for key, value in self.tuning.items() if key != "samples"}
        tuning["sampleCount"] = len(self.tuning.get("samples", []))
        timeout_seconds = float(self.settings.get("autoDisableSeconds", 0))
        idle_seconds = max(0.0, (time.monotonic_ns() - self._last_operation_ns) / 1_000_000_000)
        within_tolerance = self._position_within_auto_disable_tolerance(current_status)
        stationary = bool(current_status is not None and not current_status.get("motion")
                          and abs(float(current_status.get("speed", 0))) <= 0)
        auto_disable_active = bool(
            self.connected and enabled and timeout_seconds > 0 and stationary and within_tolerance
        )
        return {
            "connected": self.connected,
            "port": self.connected_port,
            "baud": self.baud,
            "bus": {
                "interface": self.interface_mode,
                "deviceAddress": self.device_address,
                "rs485Rts": self.rs485_rts,
                "rs485DelayMs": self.rs485_delay_ms,
            },
            "enabled": enabled,
            "enableStateSource": self._enable_state_source,
            "advancedControl": self._advanced_control,
            "error": self.last_error,
            "deviceInfo": self.device_info,
            "settings": self.settings,
            "tuning": tuning,
            "telemetry": self.telemetry.stats(),
            "autoDisable": {
                "configuredSeconds": timeout_seconds,
                "active": auto_disable_active,
                "remainingSeconds": max(0, math.ceil(timeout_seconds - idle_seconds)) if auto_disable_active else None,
                "idleSeconds": round(idle_seconds, 1),
                "positionErrorTolerance": self.settings.get("autoDisablePositionError", 100),
                "withinPositionTolerance": within_tolerance,
                "eventSequence": self._auto_disable_event_sequence,
                "message": self._auto_disable_message,
                "success": self._auto_disable_success,
            },
        }

    @contextmanager
    def motor_command(self, count_as_operation: bool = True):
        """Give hardware commands priority over periodic telemetry reads."""
        if count_as_operation:
            self.mark_operation()
        self.command_pending.set()
        try:
            with self.lock:
                yield self.require_motor()
        finally:
            self.command_pending.clear()

    def mark_operation(self) -> None:
        self._last_operation_ns = time.monotonic_ns()
        self._idle_wakeup.set()

    def _position_within_auto_disable_tolerance(self, status: dict[str, Any] | None) -> bool | None:
        if status is None:
            return None
        tolerance = abs(float(self.settings.get("autoDisablePositionError", 100)))
        try:
            return abs(float(status.get("position_error"))) <= tolerance
        except (TypeError, ValueError):
            return False

    def connect(
        self,
        port: str,
        baud: int,
        auto_disable_seconds: Any = None,
        interface_mode: str = "usb",
        device_address: Any = 1,
        rs485_rts: bool = False,
        rs485_delay_ms: Any = 0,
        auto_disable_position_error: Any = None,
    ) -> dict[str, Any]:
        if not port or len(port) > 255:
            raise ApiError("Select a valid serial port")
        if baud not in ALLOWED_BAUDS:
            raise ApiError("Unsupported baud rate")
        if interface_mode not in INTERFACES:
            raise ApiError("Interface must be usb, ttl, or rs485")
        address = int(_finite_number(device_address, "deviceAddress", 1, 255))
        delay_ms = _finite_number(rs485_delay_ms, "rs485DelayMs", 0, 100)
        if auto_disable_seconds is not None:
            self.settings["autoDisableSeconds"] = int(_finite_number(
                auto_disable_seconds,
                "autoDisableSeconds",
                0,
                86_400,
            ))
        if auto_disable_position_error is not None:
            self.settings["autoDisablePositionError"] = int(_finite_number(
                auto_disable_position_error,
                "autoDisablePositionError",
                0,
                2_147_483_647,
            ))
        self.telemetry.stop()
        with self.lock:
            if self.motor:
                self.motor.disconnect()
            motor = self.controller_factory(port=port, baudrate=baud)
            if hasattr(motor, "configure_bus"):
                motor.configure_bus(interface_mode, address, rs485_rts, delay_ms / 1000)
            else:
                motor.interface_mode = interface_mode
                motor.device_address = address
                motor.rs485_rts = bool(rs485_rts)
                motor.rs485_delay_s = delay_ms / 1000
            try:
                if not motor.connect(port, baud):
                    raise ApiError(f"Could not connect to {port}", HTTPStatus.SERVICE_UNAVAILABLE)
            except RuntimeError as exc:
                raise ApiError(str(exc), HTTPStatus.SERVICE_UNAVAILABLE) from exc
            motor.set_accel(int(self.settings["acceleration"]))
            motor.set_speed(int(self.settings["speedRpm"]))
            self.motor = motor
            self.connected_port = port
            self.baud = baud
            self.interface_mode = interface_mode
            self.device_address = address
            self.rs485_rts = bool(rs485_rts)
            self.rs485_delay_ms = delay_ms
            self._enable_protocol_variant = None
            self._enabled_override = None
            self._enable_state_source = "aggregate-0x31"
            self._advanced_control = None
            self.last_error = None
            self._refresh_device_info_locked(motor)
            self.telemetry.start(motor)
            self.mark_operation()
        return self.public_state()

    def disconnect(self) -> dict[str, Any]:
        self.stop_tuning()
        self.telemetry.stop()
        with self.lock:
            if self.motor:
                self.motor.disconnect()
            self.motor = None
            self.connected_port = None
            self.last_status = None
            self.device_info = {}
            self._enable_protocol_variant = None
            self._enabled_override = None
            self._enable_state_source = "aggregate-0x31"
            self._advanced_control = None
        return self.public_state()

    def require_motor(self) -> MotorController:
        if not self.connected or self.motor is None:
            raise ApiError("Motor is not connected", HTTPStatus.CONFLICT)
        return self.motor

    def status(self) -> dict[str, Any]:
        self.require_motor()
        sample = self.telemetry.latest or self.telemetry.wait_for_first(0.25)
        if sample is None:
            self.last_error = "The controller did not return telemetry"
            raise ApiError(self.last_error, HTTPStatus.GATEWAY_TIMEOUT)
        self.last_status = sample["status"]
        self.last_error = None
        return self.last_status

    def command(self, name: str, body: dict[str, Any]) -> dict[str, Any]:
        enabled_result = None
        motion_torque = None
        with self.motor_command() as motor:
            if name == "enable":
                control_mode_recovered = self._reclaim_serial_control_if_needed(motor)
                enabled_result = self._set_enabled_verified(motor, True)
                enabled_result["controlModeRecovered"] = control_mode_recovered
                brake_cleared = bool(motor._check_ok(motor.send(motor_commands.cmd_clear_status())))
                enabled_result["brakeCleared"] = brake_cleared
                if not brake_cleared:
                    raise ApiError("Motor enabled, but the controller brake status could not be cleared",
                                   HTTPStatus.BAD_GATEWAY)
                ok = True
            elif name == "disable":
                control_mode_recovered = self._reclaim_serial_control_if_needed(motor)
                enabled_result = self._set_enabled_verified(motor, False)
                enabled_result["controlModeRecovered"] = control_mode_recovered
                ok = True
            elif name == "stop":
                ok = motor.stop()
            elif name == "origin":
                ok = motor.set_origin()
            elif name == "clear-status":
                ok = motor._check_ok(motor.send(motor_commands.cmd_clear_status()))
            elif name == "release-stall":
                ok = motor._check_ok(motor.send(pack_frame(0xF9)))
            elif name == "save-parameters":
                ok = motor._check_ok(motor.send(pack_frame(0x04)))
            elif name == "refresh-info":
                self._refresh_device_info_locked(motor)
                ok = True
            elif name in {"relative", "jog"}:
                steps = int(_finite_number(body.get("steps"), "steps", -2_147_483_647, 2_147_483_647))
                speed = int(_finite_number(body.get("speedRpm", self.settings["speedRpm"]), "speedRpm", 1, 6000))
                motion_torque = self._apply_position_torque_limit(motor, body)
                ok = motor.move_relative(steps, speed)
            elif name == "absolute":
                position = int(_finite_number(body.get("position"), "position", 0, 4_294_967_295))
                speed = int(_finite_number(body.get("speedRpm", self.settings["speedRpm"]), "speedRpm", 1, 6000))
                motion_torque = self._apply_position_torque_limit(motor, body)
                ok = motor.move_absolute(position, speed)
            else:
                raise ApiError("Unknown motor command", HTTPStatus.NOT_FOUND)
        if not ok:
            raise ApiError(f"Controller rejected {name}", HTTPStatus.BAD_GATEWAY)
        result = {"ok": True, "command": name, "telemetrySequence": self.telemetry.sequence}
        if enabled_result is not None:
            result.update(enabled_result)
        if motion_torque is not None:
            result["torqueMa"] = motion_torque
        return result

    def _apply_position_torque_limit(self, motor: MotorController, body: dict[str, Any]) -> int:
        torque = int(_finite_number(
            body.get("torqueMa", self.settings.get("maxTorqueMa", 1500)),
            "torqueMa", 0, 3000,
        ))
        if torque != self.settings.get("maxTorqueMa"):
            if not motor._check_ok(motor.send(pack_frame(0x64, struct.pack(">H", torque)))):
                raise ApiError("Controller rejected the position torque limit", HTTPStatus.BAD_GATEWAY)
            self.settings["maxTorqueMa"] = torque
            parameters = self.device_info.get("parameters")
            if parameters:
                parameters["maxTorqueMa"] = torque
        return torque

    def _reclaim_serial_control_if_needed(self, motor: MotorController) -> bool:
        """Leave externally gated pulse/PWM modes before using serial enable control."""
        parameters = parse_driver_parameters(motor.send(pack_frame(0x32)))
        work_mode = parameters.get("workMode") if parameters else self.settings.get("workMode")
        if work_mode is None or work_mode in {0, 1, 2}:
            return False
        try:
            motor.stop()
        except Exception:
            pass
        if not motor._check_ok(motor.send(pack_frame(0x62, b"\x00"))):
            raise ApiError("Could not reclaim serial control from the external input mode",
                           HTTPStatus.BAD_GATEWAY)
        if not motor._check_ok(motor.send(motor_commands.cmd_clear_status())):
            raise ApiError("Serial control was restored, but the brake latch could not be cleared",
                           HTTPStatus.BAD_GATEWAY)
        self.settings["workMode"] = 0
        if parameters:
            parameters["workMode"] = 0
            self.device_info["parameters"] = parameters
        self._advanced_control = None
        return True

    def _set_enabled_verified(self, motor: MotorController, desired: bool) -> dict[str, Any]:
        """Use the V1.2 0xFA command and verify through 0x2F or 0x31."""
        acknowledged = False
        observed: bool | None = None
        if not desired:
            try:
                motor.stop()
            except Exception:
                pass

        for _attempt in range(2):
            try:
                acknowledged = bool(motor.set_enabled(desired)) or acknowledged
            except Exception:
                pass

            observed, source = self._read_enabled_dedicated(motor)
            if observed is not None:
                if observed == desired:
                    self._enable_protocol_variant = "native-v1.2"
                    self._enabled_override = observed
                    self._enable_state_source = source
                    return {"enabled": observed, "verified": True, "acknowledged": acknowledged,
                            "protocolVariant": "native-v1.2", "stateSource": source}
                continue

            for _read_attempt in range(3):
                status = read_all_status_fast(motor, deadline_s=0.04)
                if status is not None:
                    self.last_status = status
                    observed = status.get("enabled") == 0
                    if observed == desired:
                        self._enable_protocol_variant = "native-v1.2"
                        self._enabled_override = None
                        self._enable_state_source = "aggregate-0x31"
                        return {"enabled": observed, "verified": True, "acknowledged": acknowledged,
                                "protocolVariant": "native-v1.2", "stateSource": "aggregate-0x31"}
                time.sleep(0.01)

        action = "enable" if desired else "disable"
        if observed is None:
            raise ApiError(f"The {action} command was sent, but the controller state could not be verified",
                           HTTPStatus.GATEWAY_TIMEOUT)
        raise ApiError(f"The controller did not {action}; read-back still reports "
                       f"{'enabled' if observed else 'disabled'}", HTTPStatus.BAD_GATEWAY)

    @staticmethod
    def _read_enabled_dedicated(motor: MotorController) -> tuple[bool | None, str]:
        """Read V1.2 enable-state register 0x2F without changing state."""
        try:
            reply = motor.send(pack_frame(0x2F))
        except Exception:
            return None, "aggregate-0x31"
        if reply and reply.get("status") == 0x01:
            data = reply.get("data", b"")
            if len(data) >= 2 and data[1] in (0x00, 0x01):
                return data[1] == 0x00, "dedicated-0x2F"
        return None, "aggregate-0x31"

    def _refresh_device_info_locked(self, motor: MotorController) -> None:
        version = parse_version_reply(motor.send(pack_frame(0x20)))
        parameters = parse_driver_parameters(motor.send(pack_frame(0x32)))
        self.device_info = {"version": version, "parameters": parameters}
        if parameters:
            self.settings.update({
                "workMode": parameters["workMode"], "microstep": parameters["microstep"],
                "maxTorqueMa": parameters["maxTorqueMa"],
                "stallProtection": parameters["stallProtection"],
                "stallCurrentMa": parameters["stallCurrentMa"],
                "pid": parameters["positionPid"],
            })

    def apply_settings(self, body: dict[str, Any]) -> dict[str, Any]:
        acceleration = int(_finite_number(body.get("acceleration"), "acceleration", 0, 200))
        speed = int(_finite_number(body.get("speedRpm"), "speedRpm", 1, 6000))
        current = int(_finite_number(body.get("currentMa"), "currentMa", 0, 3000))
        microstep = int(_finite_number(body.get("microstep"), "microstep", 1, 256))
        steps_per_rev = int(_finite_number(body.get("stepsPerRevolution"), "stepsPerRevolution", 1, 10_000_000))
        auto_disable_seconds = int(_finite_number(
            body.get("autoDisableSeconds", self.settings.get("autoDisableSeconds", 300)),
            "autoDisableSeconds",
            0,
            86_400,
        ))
        auto_disable_position_error = int(_finite_number(
            body.get("autoDisablePositionError", self.settings.get("autoDisablePositionError", 100)),
            "autoDisablePositionError",
            0,
            2_147_483_647,
        ))
        work_mode = int(_finite_number(body.get("workMode", self.settings["workMode"]), "workMode", 0, 7))
        max_torque = int(_finite_number(body.get("maxTorqueMa", self.settings["maxTorqueMa"]), "maxTorqueMa", 0, 3000))
        stall_current = int(_finite_number(body.get("stallCurrentMa", self.settings["stallCurrentMa"]), "stallCurrentMa", 0, 3000))
        stall_protection = bool(body.get("stallProtection", self.settings["stallProtection"]))
        with self.motor_command() as motor:
            motor.set_accel(acceleration)
            motor.set_speed(speed)
            if not motor.set_current(current):
                raise ApiError("Controller rejected run current", HTTPStatus.BAD_GATEWAY)
            if not motor.set_microstep(microstep):
                raise ApiError("Controller rejected microstep setting", HTTPStatus.BAD_GATEWAY)
            parameter_frames = (
                ("work mode", pack_frame(0x62, bytes((work_mode,)))),
                ("maximum torque", pack_frame(0x64, struct.pack(">H", max_torque))),
                ("stall protection", pack_frame(0x6A, bytes((int(stall_protection),)))),
                ("stall current", pack_frame(0x6B, struct.pack(">H", stall_current))),
            )
            for label, frame in parameter_frames:
                if not motor._check_ok(motor.send(frame)):
                    raise ApiError(f"Controller rejected {label}", HTTPStatus.BAD_GATEWAY)
            self.settings.update({
                "acceleration": acceleration,
                "speedRpm": speed,
                "currentMa": current,
                "microstep": microstep,
                "stepsPerRevolution": steps_per_rev,
                "autoDisableSeconds": auto_disable_seconds,
                "autoDisablePositionError": auto_disable_position_error,
                "workMode": work_mode,
                "maxTorqueMa": max_torque,
                "stallProtection": stall_protection,
                "stallCurrentMa": stall_current,
            })
            self.mark_operation()
        return {"ok": True, "settings": self.settings}

    def set_device_address(self, body: dict[str, Any]) -> dict[str, Any]:
        new_address = int(_finite_number(body.get("address"), "address", 1, 255))
        with self.motor_command() as motor:
            if not motor._check_ok(motor.send(pack_frame(0x60, bytes((new_address,))))):
                raise ApiError("Controller rejected the new slave address", HTTPStatus.BAD_GATEWAY)
            motor.device_address = new_address
            self.device_address = new_address
        return {"ok": True, "deviceAddress": new_address}

    def _idle_loop(self) -> None:
        while not self._idle_stop.is_set():
            self._idle_wakeup.wait(0.1)
            self._idle_wakeup.clear()
            if self._idle_stop.is_set() or not self.connected:
                continue
            timeout_seconds = float(self.settings.get("autoDisableSeconds", 0))
            if timeout_seconds <= 0 or self.telemetry.latest is None:
                continue
            status = self.telemetry.latest["status"]
            if status.get("enabled") != 0:
                continue
            if (status.get("motion") or abs(float(status.get("speed", 0))) > 0
                    or not self._position_within_auto_disable_tolerance(status)):
                self._last_operation_ns = time.monotonic_ns()
                continue
            idle_seconds = (time.monotonic_ns() - self._last_operation_ns) / 1_000_000_000
            if idle_seconds < timeout_seconds:
                continue
            self._auto_disable_if_still_idle(timeout_seconds)

    def _auto_disable_if_still_idle(self, timeout_seconds: float) -> None:
        try:
            with self.motor_command(count_as_operation=False) as motor:
                latest = self.telemetry.latest
                idle_seconds = (time.monotonic_ns() - self._last_operation_ns) / 1_000_000_000
                if (
                    latest is None
                    or idle_seconds < timeout_seconds
                    or latest["status"].get("enabled") != 0
                    or latest["status"].get("motion")
                    or abs(float(latest["status"].get("speed", 0))) > 0
                    or not self._position_within_auto_disable_tolerance(latest["status"])
                ):
                    return
                self._set_enabled_verified(motor, False)
            self._auto_disable_event_sequence += 1
            self._auto_disable_success = True
            self._auto_disable_message = (
                f"Motor automatically disabled after holding inside the position-error tolerance "
                f"for {int(timeout_seconds)} seconds"
            )
        except Exception as exc:
            self._auto_disable_event_sequence += 1
            self._auto_disable_success = False
            self._auto_disable_message = f"Automatic motor disable failed: {exc}"
            self.last_error = self._auto_disable_message
            self.mark_operation()

    def shutdown(self) -> None:
        self._idle_stop.set()
        self._idle_wakeup.set()
        self.disconnect()
        thread = self._idle_thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=0.5)
        self._idle_thread = None

    def apply_pid(self, body: dict[str, Any]) -> dict[str, Any]:
        values = {
            key: int(_finite_number(body.get(key), key, 0, 4_294_967_295))
            for key in ("kp", "ki", "kd")
        }
        with self.motor_command() as motor:
            reply = motor.send(motor_commands.write_position_pid(values["kp"], values["ki"], values["kd"]))
            if not motor._check_ok(reply):
                raise ApiError("Controller rejected PID gains", HTTPStatus.BAD_GATEWAY)
            self.settings["pid"] = values
        return {"ok": True, "pid": values}

    def raw(self, body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("hex", "")).strip()
        try:
            payload = bytes.fromhex(text)
        except ValueError as exc:
            raise ApiError("Raw command must contain hexadecimal bytes") from exc
        if not payload or len(payload) > 64:
            raise ApiError("Raw command must contain 1–64 bytes")
        with self.motor_command() as motor:
            response = motor.send_raw(payload)
        return {"ok": True, "tx": payload.hex(" ").upper(), "rx": None if response is None else response.hex(" ").upper()}

    def set_advanced_control(self, body: dict[str, Any]) -> dict[str, Any]:
        frame = advanced_control_frame(body)
        mode = str(body["mode"])
        with self.motor_command() as motor:
            if not motor._check_ok(motor.send(frame)):
                raise ApiError(f"Controller rejected {mode}", HTTPStatus.BAD_GATEWAY)
        self._advanced_control = {
            "mode": mode,
            "functionCode": f"0x{ADVANCED_CONTROL_CODES[mode]:02X}",
        }
        return {"ok": True, **self._advanced_control}

    def start_tuning(self, body: dict[str, Any]) -> dict[str, Any]:
        raw_steps = body.get("steps")
        if raw_steps is None:
            target = _finite_number(body.get("targetRpm"), "targetRpm", -6000, 6000)
            if target == 0:
                raise ApiError("targetRpm cannot be zero")
            steps = [target]
            hold_s = _finite_number(body.get("durationS"), "durationS", 0.5, 60)
        else:
            if not isinstance(raw_steps, list) or not 2 <= len(raw_steps) <= 16:
                raise ApiError("steps must contain 2–16 speed levels")
            steps = [_finite_number(value, f"steps[{index}]", -6000, 6000)
                     for index, value in enumerate(raw_steps)]
            if any(value == 0 for value in steps):
                raise ApiError("step speed cannot be zero")
            hold_s = _finite_number(body.get("holdS"), "holdS", 0.2, 30)
            if hold_s * len(steps) > 120:
                raise ApiError("The complete step sequence cannot exceed 120 seconds")
        acceleration = int(_finite_number(body.get("acceleration", self.settings["acceleration"]), "acceleration", 0, 200))
        with self.lock:
            self.require_motor()
            if self.tuning["running"]:
                raise ApiError("A response test is already running", HTTPStatus.CONFLICT)
            self._tuning_stop.clear()
            self.tuning = {
                "running": True, "targetRpm": steps[0], "steps": steps, "stepIndex": 0,
                "holdS": hold_s, "samples": [], "metrics": None,
                "message": "Preparing response test",
            }
        thread = threading.Thread(target=self._run_tuning, args=(steps, hold_s, acceleration), daemon=True)
        thread.start()
        return self.tuning

    def _run_tuning(self, steps: list[float], hold_s: float, acceleration: int) -> None:
        last_sequence = self.telemetry.sequence
        samples: list[dict[str, Any]] = []
        error: str | None = None
        start_ns = 0
        try:
            with self.motor_command() as motor:
                self._set_enabled_verified(motor, True)
                if not motor._check_ok(motor.send(motor_commands.cmd_clear_status())):
                    raise ApiError("Controller brake status could not be cleared", HTTPStatus.BAD_GATEWAY)
            start_ns = time.monotonic_ns()

            for step_index, target in enumerate(steps):
                if self._tuning_stop.is_set():
                    break
                latest_before_step = self.telemetry.latest
                start_speed = float(latest_before_step["status"].get("speed", 0)) if latest_before_step else 0.0
                with self.lock:
                    self.tuning.update({"targetRpm": target, "stepIndex": step_index,
                                        "message": f"Step {step_index + 1} of {len(steps)} · {target:g} RPM"})
                direction = 0 if target >= 0 else 1
                with self.motor_command() as motor:
                    reply = motor.send(motor_commands.cmd_speed_mode(direction, acceleration, abs(target)))
                    if not motor._check_ok(reply):
                        raise ApiError(f"Controller rejected speed step {step_index + 1}", HTTPStatus.BAD_GATEWAY)
                last_sequence = self.telemetry.sequence
                segment_ends_at = time.monotonic() + hold_s
                while not self._tuning_stop.is_set() and time.monotonic() < segment_ends_at:
                    telemetry_batch = self.telemetry.wait_after(last_sequence, timeout=min(0.1, hold_s))
                    for telemetry_sample in telemetry_batch:
                        last_sequence = max(last_sequence, telemetry_sample["sequence"])
                        timestamp = (telemetry_sample["monotonicNs"] - start_ns) / 1_000_000_000
                        if timestamp < 0:
                            continue
                        status = telemetry_sample["status"]
                        samples.append({
                            "t": round(timestamp, 4), "target": target, "speed": status.get("speed", 0),
                            "current": status.get("current", 0), "position": status.get("actual_pos", 0),
                            "stepIndex": step_index, "startSpeed": start_speed,
                        })
                    with self.lock:
                        self.tuning["samples"] = list(samples)
        except Exception as exc:  # hardware failure must be surfaced to the UI
            error = str(exc)
        finally:
            try:
                if self.connected:
                    with self.motor_command() as motor:
                        self._set_enabled_verified(motor, False)
            except Exception as exc:
                error = error or f"Safe shutdown failed: {exc}"
            if len(steps) == 1:
                metrics = response_metrics(samples, steps[0])
            else:
                metrics = sequence_response_metrics(samples)
            with self.lock:
                self.tuning = {
                    "running": False,
                    "targetRpm": steps[-1], "steps": steps, "stepIndex": len(steps) - 1,
                    "holdS": hold_s,
                    "samples": samples,
                    "metrics": metrics,
                    "message": error or ("Test stopped" if self._tuning_stop.is_set() else "Response test complete"),
                }

    def stop_tuning(self) -> dict[str, Any]:
        self._tuning_stop.set()
        if self.connected:
            try:
                with self.motor_command() as motor:
                    motor.stop()
            except Exception as exc:
                self.last_error = str(exc)
        return self.tuning


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "PD42S1MDM/0.1"

    @property
    def app(self) -> AppState:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError("Invalid Content-Length") from exc
        if length > 32_768:
            raise ApiError("Request is too large", HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        if length == 0:
            return {}
        try:
            value = json.loads(self.rfile.read(length))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError("Request body must be valid JSON") from exc
        if not isinstance(value, dict):
            raise ApiError("Request body must be a JSON object")
        return value

    def _error(self, exc: Exception) -> None:
        if isinstance(exc, ApiError):
            self._json({"ok": False, "error": str(exc)}, exc.status)
        elif isinstance(exc, PD42S1Error):
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_GATEWAY)
        else:
            self._json({"ok": False, "error": "Internal server error"}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_GET(self) -> None:
        try:
            path = urlparse(self.path).path
            if path == "/api/ports":
                self._json({"ok": True, "ports": self.app.controller_factory.available_ports()})
            elif path == "/api/state":
                self._json({"ok": True, **self.app.public_state()})
            elif path == "/api/status":
                self._json({"ok": True, "status": self.app.status(), "state": self.app.public_state()})
            elif path == "/api/telemetry":
                self._telemetry_stream()
            else:
                self._static(path)
        except Exception as exc:
            self._error(exc)

    def _telemetry_stream(self) -> None:
        if not self.app.connected:
            raise ApiError("Motor is not connected", HTTPStatus.CONFLICT)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True
        sequence = self.app.telemetry.sequence
        try:
            while self.app.connected and self.app.telemetry.running:
                samples = self.app.telemetry.wait_after(sequence, timeout=1.0)
                if samples:
                    sequence = samples[-1]["sequence"]
                    tuning_target = self.app.tuning.get("targetRpm") if self.app.tuning.get("running") else None
                    streamed_samples = [dict(sample, tuningTargetRpm=tuning_target) for sample in samples]
                    payload = json.dumps(streamed_samples, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"event: telemetry\ndata: {payload}\n\n".encode("utf-8"))
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def do_POST(self) -> None:
        try:
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/connect":
                baud = int(_finite_number(body.get("baud", 115200), "baud", 9600, 921600))
                result = self.app.connect(
                    port=str(body.get("port", "")),
                    baud=baud,
                    auto_disable_seconds=body.get("autoDisableSeconds"),
                    interface_mode=str(body.get("interface", "usb")),
                    device_address=body.get("deviceAddress", 1),
                    rs485_rts=bool(body.get("rs485Rts", False)),
                    rs485_delay_ms=body.get("rs485DelayMs", 0),
                    auto_disable_position_error=body.get("autoDisablePositionError"),
                )
            elif path == "/api/disconnect":
                result = self.app.disconnect()
            elif path.startswith("/api/command/"):
                result = self.app.command(path.rsplit("/", 1)[-1], body)
            elif path == "/api/settings":
                result = self.app.apply_settings(body)
            elif path == "/api/bus/address":
                result = self.app.set_device_address(body)
            elif path == "/api/pid":
                result = self.app.apply_pid(body)
            elif path == "/api/raw":
                result = self.app.raw(body)
            elif path == "/api/control-mode":
                result = self.app.set_advanced_control(body)
            elif path == "/api/tuning/start":
                result = self.app.start_tuning(body)
            elif path == "/api/tuning/stop":
                result = self.app.stop_tuning()
            else:
                raise ApiError("API route not found", HTTPStatus.NOT_FOUND)
            self._json({"ok": True, "result": result})
        except Exception as exc:
            self._error(exc)

    def _static(self, request_path: str) -> None:
        relative = "index.html" if request_path == "/" else unquote(request_path.lstrip("/"))
        candidate = (WEB_ROOT / relative).resolve()
        if WEB_ROOT.resolve() not in candidate.parents or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)


class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], app: AppState | None = None):
        super().__init__(address, ApiHandler)
        self.app = app or AppState()


def main() -> None:
    parser = argparse.ArgumentParser(description="PD42S1 local web console")
    parser.add_argument("--host", default="127.0.0.1", help="Bind address (default: localhost only)")
    parser.add_argument("--port", type=int, default=8421, help="HTTP port")
    parser.add_argument("--no-browser", action="store_true", help="Do not open a browser automatically")
    args = parser.parse_args()
    server = AppServer((args.host, args.port))
    url = f"http://{args.host}:{server.server_address[1]}"
    print(f"PD42S1 MDM running at {url}")
    if not args.no_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.shutdown()
        server.server_close()


if __name__ == "__main__":
    main()
