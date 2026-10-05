"""Small public API for timed product demonstrations, using the proven driver core."""
import argparse
import math
import struct
import threading
import time
import webbrowser
from http.server import ThreadingHTTPServer
from urllib.parse import urlparse

from .core import AppState, ApiHandler, ApiError, _finite_number, pack_frame


def number(body, key, low, high, default=None):
    return _finite_number(body.get(key, default), key, low, high)


def integer(body, key, low, high, default=None):
    value = number(body, key, low, high, default)
    if value != int(value):
        raise ApiError(f"{key} must be a whole number")
    return int(value)


def demo_frames(body):
    """Validate all fields before enabling hardware; return documented native frames."""
    mode = body.get("mode")
    duration = number(body, "duration", 1, 30, 5)
    acceleration = integer(body, "acceleration", 0, 200, 20)
    limit = integer(body, "limitMa", 0, 3000, 300)
    prefix = [pack_frame(0x64, struct.pack(">H", limit))]
    if mode == "velocity":
        speed = number(body, "rpm", -6000, 6000, 30)
        if abs(speed) < 0.1:
            raise ApiError("Speed must be at least 0.1 RPM; use Stop for zero.")
        frames = prefix + [pack_frame(0xF1, struct.pack(">BBf", int(speed < 0), acceleration, abs(speed)))]
    elif mode == "torque":
        current = integer(body, "currentMa", -3000, 3000, 150)
        if abs(current) > limit:
            raise ApiError("Torque current must be within the selected current limit.")
        frames = prefix + [pack_frame(0xF0, struct.pack(">BH", int(current < 0), abs(current)))]
    elif mode == "position":
        if body.get("positionMode", "relative") not in {"relative", "absolute"}:
            raise ApiError("Choose relative or absolute position")
        relative = body.get("positionMode", "relative") == "relative"
        position = integer(body, "position", -2147483647 if relative else 0, 2147483647 if relative else 4294967295, 6400)
        speed = integer(body, "rpm", 1, 6000, 30)
        frames = prefix + [pack_frame(0xF3 if relative else 0xF2, struct.pack(">BBHI", int(position < 0), acceleration, speed, abs(position)))]
    elif mode == "home":
        method = integer(body, "method", 0, 3, 2)
        direction = integer(body, "direction", 0, 1, 0)
        strategy = integer(body, "strategy", 0, 2, 1)
        speed = integer(body, "rpm", 1, 6000, 10)
        frames = prefix + [pack_frame(0x95, struct.pack(">I", int(duration * 1000))),
                           pack_frame(0x91, struct.pack(">BBIH", method, direction, speed, limit)),
                           pack_frame(0x92, bytes((strategy,)))]
    else:
        raise ApiError("Choose velocity, torque, position, or home.")
    return frames, duration


class ShowcaseState(AppState):
    def __post_init__(self):
        super().__post_init__()
        self.demo = {"running": False, "mode": None, "message": "Ready", "endsAt": None}
        self.demo_timer = None
        self.demo_generation = 0

    def public_state(self):
        result = super().public_state()
        result["demo"] = dict(getattr(self, "demo", {}))
        result["preview"] = self.controller_factory is PreviewMotor
        return result

    def connect(self, *args, **kwargs):
        super().connect(*args, **kwargs)
        return self.stop_demo("Connected; output disabled")

    def cancel_timer(self):
        self.demo_generation += 1
        if self.demo_timer:
            self.demo_timer.cancel()
            self.demo_timer = None

    def stop_demo(self, message="Stopped and disabled"):
        with self.lock:
            self.cancel_timer()
            abort_error = None
            if self.connected:
                try:
                    with self.motor_command() as motor:
                        if self.demo.get("mode") == "home" and self.demo.get("running"):
                            if not motor._check_ok(motor.send(pack_frame(0x93))):
                                raise ApiError("Homing abort was not acknowledged")
                except Exception as exc:
                    abort_error = exc
                # A failed homing-abort acknowledgement must never skip disable.
                self.command("disable", {})
            self.demo.update(running=False, mode=None, message=message, endsAt=None,
                             targetRpm=0, targetCurrent=0)
            if abort_error:
                raise ApiError(f"Output disabled, but homing abort failed: {abort_error}")
        return self.public_state()

    def start_demo(self, body):
        frames, duration = demo_frames(body)
        with self.lock:
            self.stop_demo()
            try:
                with self.motor_command() as motor:
                    # Apply the current limit and homing configuration while disabled.
                    for frame in frames[:-1]:
                        if not motor._check_ok(motor.send(frame)):
                            raise ApiError(f"Controller rejected command 0x{frame[2]:02X}")
                self.settings["maxTorqueMa"] = int(number(body, "limitMa", 0, 3000, 300))
                self.command("enable", {})
                self.demo = {"running": True, "mode": body["mode"], "message": "Running",
                             "endsAt": time.time() + duration,
                             "targetRpm": body.get("rpm", 30) if body["mode"] == "velocity" else 0,
                             "targetCurrent": body.get("currentMa", 150) if body["mode"] == "torque" else 0}
                with self.motor_command() as motor:
                    if not motor._check_ok(motor.send(frames[-1])):
                        raise ApiError(f"Controller rejected command 0x{frames[-1][2]:02X}")
                generation = self.demo_generation
                self.demo_timer = threading.Timer(duration, lambda: self.finish_demo(generation))
                self.demo_timer.daemon = True
                self.demo_timer.start()
            except Exception:
                self.stop_demo("Command failed; output disabled")
                raise
        return self.public_state()

    def finish_demo(self, generation=None):
        with self.lock:
            if generation is not None and generation != self.demo_generation:
                return
            try:
                self.stop_demo("Test finished; output disabled")
            except Exception as exc:
                self.demo.update(running=False, message=f"Disable failed: {exc}", endsAt=None)
                self.last_error = str(exc)

    def action(self, body):
        action = body.get("action")
        if action == "stop":
            return self.stop_demo()
        if action == "run":
            return self.start_demo(body)
        if action in {"zero", "calibrate", "release"}:
            with self.lock:
                self.stop_demo()
                if action == "zero":
                    return self.command("origin", {})
                if action == "release":
                    return self.command("release-stall", {})
                with self.motor_command() as motor:
                    if not motor._check_ok(motor.send(pack_frame(0x01))):
                        raise ApiError("Encoder calibration was not acknowledged")
                self.demo.update(mode="calibrate", message="Calibration requested. Check the driver display for completion.")
                return {"ok": True, "message": self.demo["message"]}
        raise ApiError("Unknown showcase action")

    def disconnect(self):
        with self.lock:
            if self.connected:
                self.stop_demo()
            return super().disconnect()

    def shutdown(self):
        try:
            self.stop_demo()
        finally:
            super().shutdown()


class PreviewMotor:
    """Local simulated motor; never opens a serial device."""
    @classmethod
    def available_ports(cls):
        return [{"device": "PREVIEW", "description": "Simulated PD42S1"}]

    def __init__(self, **kwargs):
        self.is_connected = False
        self.enabled = False
        self.position = 0.0
        self.target = 0.0
        self.rpm = 0.0
        self.current = 0
        self.mode = "position"
        self.last = time.monotonic()

    def connect(self, *args):
        self.is_connected = True
        return True

    def disconnect(self):
        self.is_connected = False

    def set_accel(self, value):
        pass

    def set_speed(self, value):
        pass

    def set_enabled(self, value):
        self.enabled = value
        return True

    def stop(self):
        self.rpm = 0
        self.current = 0
        self.target = self.position
        return True

    def _check_ok(self, reply):
        return reply and reply.get("status") == 1

    def set_origin(self):
        self.position = self.target = 0
        return True

    def send(self, frame):
        code, data = frame[2], frame[3:-2]
        if code == 0x2F:
            return {"status": 1, "data": bytes((1, int(not self.enabled)))}
        if code == 0xF1:
            direction, _, speed = struct.unpack(">BBf", data)
            self.rpm, self.mode = speed * (-1 if direction else 1), "velocity"
        elif code == 0xF0:
            direction, current = struct.unpack(">BH", data)
            self.current = current * (-1 if direction else 1)
            self.rpm, self.mode = self.current / 15, "torque"
        elif code in {0xF2, 0xF3}:
            direction, _, speed, position = struct.unpack(">BBHI", data)
            value = position * (-1 if direction else 1)
            self.target = value + self.position if code == 0xF3 else value
            self.rpm, self.mode = speed, "position"
        elif code == 0x92:
            self.target, self.rpm, self.mode = 0, 10, "position"
        return {"status": 1}

    def read_all_status(self):
        now = time.monotonic()
        dt = min(now - self.last, .1)
        self.last = now
        speed = self.rpm if self.enabled else 0
        if self.mode == "position":
            error = self.target - self.position
            speed = math.copysign(min(abs(speed), abs(error) / 51200 * 60 / max(dt, .001)), error)
        self.position += speed / 60 * 51200 * dt
        return {"voltage": 24.0, "current": self.current if self.enabled and self.mode == "torque" else round(speed * 2),
                "speed": speed, "actual_pos": round(self.position), "target_pos": round(self.target),
                "position_error": round(self.target - self.position), "enabled": int(not self.enabled),
                "motion": int(abs(speed) > .1), "in_position": int(abs(self.target - self.position) < 100),
                "stalled": 0, "pulse_count": 0, "address_mode": 0, "flux": 0, "resistance": 0, "inductance": 0}


class ShowcaseHandler(ApiHandler):
    def do_POST(self):
        try:
            origin = self.headers.get("Origin")
            allowed = {f"http://127.0.0.1:{self.server.server_port}",
                       f"http://localhost:{self.server.server_port}"}
            if origin and origin not in allowed:
                raise ApiError("Cross-origin motor commands are not allowed", 403)
            path = urlparse(self.path).path
            body = self._body()
            if path == "/api/action":
                result = self.app.action(body)
            elif path == "/api/connect":
                if self.app.connected:
                    self.app.stop_demo()
                result = self.app.connect(str(body.get("port", "")), int(number(body, "baud", 9600, 921600, 115200)),
                                          interface_mode=str(body.get("interface", "usb")),
                                          device_address=body.get("deviceAddress", 1), auto_disable_seconds=60)
            elif path == "/api/disconnect":
                result = self.app.disconnect()
            else:
                raise ApiError("Route is not available in the showcase", 404)
            self._json({"ok": True, "result": result})
        except Exception as exc:
            self._error(exc)


def main():
    parser = argparse.ArgumentParser(description="ModuleMore PD42S1 showcase")
    parser.add_argument("--port", type=int, default=8422)
    parser.add_argument("--preview", action="store_true", help="Simulate hardware for product demos")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    app = ShowcaseState(controller_factory=PreviewMotor) if args.preview else ShowcaseState()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), ShowcaseHandler)
    server.daemon_threads = True
    server.app = app
    url = f"http://127.0.0.1:{args.port}"
    print(f"ModuleMore Showcase: {url}" + (" (simulated hardware)" if args.preview else ""))
    if not args.no_browser:
        threading.Timer(.4, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.shutdown()
        server.server_close()
