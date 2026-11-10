import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

_tmp = tempfile.mkdtemp()
os.environ["PACER_DATA_DIR"] = _tmp
os.environ["EMULATOR_AUTH_TOKEN"] = "test-token"

import web_server as ws  # noqa: E402
from fake_console import FakeConsole  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

GPX = """<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1.1"><trk><trkseg>
<trkpt lat="54.7757" lon="56.0366"><ele>100</ele><time>2025-05-12T09:30:00Z</time></trkpt>
<trkpt lat="54.7760" lon="56.0366"><ele>101</ele><time>2025-05-12T09:30:20Z</time></trkpt>
<trkpt lat="54.7760" lon="56.0370"><ele>102</ele><time>2025-05-12T09:30:40Z</time></trkpt>
</trkseg></trk></gpx>"""
LONG_GPX = GPX.replace("09:30:40Z", "10:30:40Z")


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws.TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        cls.emu = FakeConsole()
        cls.state = ws.State("127.0.0.1", cls.emu.port)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ws.make_handler(cls.state, "secret"))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.state.cancel.set()
        cls.srv.shutdown()
        cls.emu.stop()

    def call(self, method, path, body=None, token="secret", raw=False):
        data = body if raw else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"X-Token": token})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_state(self, want, timeout=15):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("GET", "/api/status")[1]["status"]["state"]
            if st in want:
                return st
            time.sleep(0.1)
        self.fail("state timeout")

    def test_01_auth_and_index(self):
        with urllib.request.urlopen(self.base + "/") as r:
            self.assertIn(b"Pacer GPX Player", r.read())
        self.assertEqual(self.call("GET", "/api/tracks", token="bad")[0], 401)
        self.assertEqual(self.call("GET", "/api/tracks")[0], 200)

    def test_02_upload_validation(self):
        self.assertEqual(self.call("POST", "/api/upload?name=../evil.gpx", GPX.encode(), raw=True)[0], 400)
        self.assertEqual(self.call("POST", "/api/upload?name=a.txt", GPX.encode(), raw=True)[0], 400)
        self.assertEqual(self.call("POST", "/api/upload?name=bad.gpx", b"<gpx/>", raw=True)[0], 400)
        bomb = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aa">]><gpx/>'
        self.assertEqual(self.call("POST", "/api/upload?name=bomb.gpx", bomb, raw=True)[0], 400)
        code, j = self.call("POST", "/api/upload?name=ok.gpx", GPX.encode(), raw=True)
        self.assertEqual((code, j["points"]), (200, 3))
        self.assertIn("ok.gpx", self.call("GET", "/api/tracks")[1]["tracks"])

    def test_03_param_validation(self):
        self.call("POST", "/api/upload?name=ok.gpx", GPX.encode(), raw=True)
        code, j = self.call("POST", "/api/start", {"file": "ok.gpx", "params": {"speed": 999}})
        self.assertEqual(code, 400)
        code, j = self.call("POST", "/api/start", {"file": "../../etc/passwd", "params": {}})
        self.assertEqual(code, 400)

    def test_04_preview(self):
        self.call("POST", "/api/upload?name=ok.gpx", GPX.encode(), raw=True)
        code, j = self.call("POST", "/api/preview",
                            {"file": "ok.gpx", "params": {"human": True, "seed": 1}})
        self.assertEqual(code, 200)
        self.assertGreater(j["summary"]["distance_m"], 50)
        self.assertGreater(len(j["route"]), 3)

    def test_05_run_to_completion(self):
        self.call("POST", "/api/upload?name=ok.gpx", GPX.encode(), raw=True)
        self.emu.received.clear()
        code, _ = self.call("POST", "/api/start", {"file": "ok.gpx", "params": {"speed": 10}})
        self.assertEqual(code, 200)
        self.assertEqual(self.wait_state({"done", "error", "stopped"}), "done")
        self.assertTrue(any(c.startswith("geo fix") for c in self.emu.received))
        self.assertTrue(any(c.startswith("sensor set pressure") for c in self.emu.received))

    def test_06_stop_and_single_job(self):
        self.call("POST", "/api/upload?name=long.gpx", LONG_GPX.encode(), raw=True)
        self.assertEqual(self.call("POST", "/api/start", {"file": "long.gpx", "params": {"speed": 1}})[0], 200)
        self.assertEqual(self.call("POST", "/api/start", {"file": "long.gpx", "params": {}})[0], 400)
        time.sleep(0.5)
        self.call("POST", "/api/stop", {})
        self.assertEqual(self.wait_state({"stopped", "done", "error"}), "stopped")

    def test_07_emulator_status(self):
        j = self.call("GET", "/api/status")[1]
        self.assertTrue(j["emulator"]["up"])


if __name__ == "__main__":
    unittest.main()
