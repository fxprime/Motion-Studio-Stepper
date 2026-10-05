import json
import struct
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from showcase.server import ApiError, PreviewMotor, ShowcaseHandler, ShowcaseState, demo_frames


class FrameTests(unittest.TestCase):
    def test_velocity(self):
        frames, duration = demo_frames({"mode": "velocity", "rpm": -30})
        self.assertEqual(frames[-1][2], 0xF1)
        self.assertEqual(struct.unpack(">BBf", frames[-1][3:-2]), (1, 20, 30))
        self.assertEqual(duration, 5)

    def test_torque(self):
        frames, _ = demo_frames({"mode": "torque", "currentMa": -150})
        self.assertEqual(frames[-1][2], 0xF0)
        self.assertEqual(struct.unpack(">BH", frames[-1][3:-2]), (1, 150))

    def test_position(self):
        for mode, position, code, direction in [("relative", -6400, 0xF3, 1), ("absolute", 6400, 0xF2, 0)]:
            frames, _ = demo_frames({"mode": "position", "positionMode": mode, "position": position})
            self.assertEqual(frames[-1][2], code)
            self.assertEqual(struct.unpack(">BBHI", frames[-1][3:-2]), (direction, 20, 30, 6400))

    def test_home(self):
        frames, _ = demo_frames({"mode": "home", "duration": 3, "method": 3, "direction": 1})
        self.assertEqual([frame[2] for frame in frames], [0x64, 0x95, 0x91, 0x92])
        self.assertEqual(struct.unpack(">I", frames[1][3:-2]), (3000,))
        self.assertEqual(struct.unpack(">BBIH", frames[2][3:-2]), (3, 1, 10, 300))

    def test_invalid_commands(self):
        for body in [{"mode": "torque", "currentMa": 301}, {"mode": "velocity", "rpm": 0},
                     {"mode": "velocity", "rpm": float("nan")}, {"mode": "velocity", "duration": 31},
                     {"mode": "position", "positionMode": "wrong"},
                     {"mode": "position", "positionMode": "absolute", "position": -1},
                     {"mode": "home", "direction": .5}]:
            with self.subTest(body=body), self.assertRaises(ApiError):
                demo_frames(body)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.app = ShowcaseState(controller_factory=PreviewMotor)
        self.app.connect("PREVIEW", 115200)

    def tearDown(self):
        self.app.shutdown()

    def test_invalid_does_not_enable(self):
        with self.assertRaises(ApiError):
            self.app.start_demo({"mode": "torque", "currentMa": 3001})
        self.assertFalse(self.app.motor.enabled)

    def test_repeat_modes_and_zero(self):
        for body in [{"mode": "velocity", "rpm": 30}, {"mode": "velocity", "rpm": -30},
                     {"mode": "torque", "currentMa": 150}, {"mode": "position"}, {"mode": "home"}]:
            with self.subTest(body=body):
                self.app.start_demo(body)
                self.assertTrue(self.app.motor.enabled)
                self.app.stop_demo()
                self.assertFalse(self.app.motor.enabled)
        self.app.action({"action": "zero"})
        self.assertEqual(self.app.motor.position, 0)

    def test_timer_disables(self):
        self.app.start_demo({"mode": "velocity", "duration": 1})
        deadline = time.monotonic() + 3
        while self.app.demo["running"] and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertFalse(self.app.motor.enabled)
        self.assertFalse(self.app.demo["running"])

    def test_old_timer_cannot_stop_new_test(self):
        self.app.start_demo({"mode": "velocity"})
        old = self.app.demo_generation
        self.app.start_demo({"mode": "torque"})
        self.app.finish_demo(old)
        self.assertTrue(self.app.motor.enabled)

    def test_limit_configured_before_enabling(self):
        events = []
        send = self.app.motor.send
        enabled = self.app.motor.set_enabled
        self.app.motor.send = lambda frame: (events.append(frame[2]), send(frame))[1]
        self.app.motor.set_enabled = lambda state: (events.append("enable" if state else "disable"), enabled(state))[1]
        self.app.start_demo({"mode": "velocity"})
        self.assertLess(events.index(0x64), events.index("enable"))
        self.assertLess(events.index("enable"), events.index(0xF1))

    def test_rejected_configuration_stays_disabled(self):
        send = self.app.motor.send
        self.app.motor.send = lambda frame: {"status": 0} if frame[2] == 0x64 else send(frame)
        with self.assertRaises(ApiError):
            self.app.start_demo({"mode": "velocity"})
        self.assertFalse(self.app.motor.enabled)

    def test_abort_failure_still_disables(self):
        self.app.start_demo({"mode": "home"})
        original = self.app.motor.send
        self.app.motor.send = lambda frame: {"status": 0} if frame[2] == 0x93 else original(frame)
        with self.assertRaises(ApiError):
            self.app.stop_demo()
        self.assertFalse(self.app.motor.enabled)

    def test_disconnect_stops(self):
        self.app.start_demo({"mode": "velocity"})
        motor = self.app.motor
        self.app.disconnect()
        self.assertFalse(motor.enabled)

    def test_calibration_ack(self):
        result = self.app.action({"action": "calibrate"})
        self.assertIn("requested", result["message"])


class HttpTests(unittest.TestCase):
    def test_public_api_and_static(self):
        app = ShowcaseState(controller_factory=PreviewMotor)
        server = ThreadingHTTPServer(("127.0.0.1", 0), ShowcaseHandler)
        server.app = app
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        try:
            with urlopen(base + "/") as response:
                self.assertIn(b"Motion Studio", response.read())
            for route, body in [("/api/connect", {"port": "PREVIEW"}),
                                ("/api/action", {"action": "run", "mode": "velocity"}),
                                ("/api/action", {"action": "stop"}),
                                ("/api/disconnect", {})]:
                request = Request(base + route, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
                with urlopen(request) as response:
                    self.assertTrue(json.load(response)["ok"])
            request = Request(base + "/api/raw", data=b"{}")
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 404)
            request = Request(base + "/api/action", data=b'{"action":"stop"}', headers={"Origin": "https://untrusted.example"})
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            self.assertEqual(error.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            app.shutdown()


if __name__ == "__main__":
    unittest.main()
