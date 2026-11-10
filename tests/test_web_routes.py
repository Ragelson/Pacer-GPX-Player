import json
import os
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

_tmp = tempfile.mkdtemp()
os.environ.setdefault("PACER_DATA_DIR", _tmp)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_graphhopper  # noqa: E402
import web_server as ws  # noqa: E402


class WebRouteReportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ws.TRACKS_DIR.mkdir(parents=True, exist_ok=True)
        cls.gh, gh_url = fake_graphhopper.start()
        cls.state = ws.State("127.0.0.1", 1, gh_url, tempfile.mkdtemp())
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), ws.make_handler(cls.state, "secret"))
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.gh.shutdown()

    def call(self, method, path, body=None, token="secret", binary=False):
        req = urllib.request.Request(self.base + path, method=method, headers={"X-Token": token},
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                return r.status, (data if binary else json.loads(data))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def wait_challenge(self, timeout=60):
        end = time.time() + timeout
        while time.time() < end:
            st = self.call("GET", "/api/challenge/status")[1]
            if st["state"] in ("done", "error"):
                return st
            time.sleep(0.2)
        self.fail("challenge timeout")

    def test_01_gh_status_and_auth(self):
        self.assertTrue(self.call("GET", "/api/gh/status")[1]["up"])
        self.assertEqual(self.call("POST", "/api/route/build", {}, token="x")[0], 401)

    def test_02_build_and_save_loop(self):
        code, j = self.call("POST", "/api/route/build",
                            {"mode": "loop", "lat": 54.77, "lon": 56.03, "length_km": 4, "exact": True})
        self.assertEqual(code, 200)
        self.assertAlmostEqual(j["summary"]["distance_m"], 4000, delta=0.01)
        self.assertGreater(len(j["route"]), 2)
        self.assertEqual(self.call("POST", "/api/route/save", {"id": j["id"], "name": "t_loop"})[0], 200)
        self.assertEqual(self.call("POST", "/api/route/save", {"id": j["id"], "name": "t_loop"})[0], 400)  # уже есть
        self.assertIn("t_loop.gpx", self.call("GET", "/api/tracks")[1]["tracks"])
        self.assertEqual(self.call("POST", "/api/route/save", {"id": j["id"], "name": "../x"})[0], 400)
        self.assertEqual(self.call("POST", "/api/route/save", {"id": "nope", "name": "ok1"})[0], 400)

    def test_03_build_points_and_validation(self):
        code, j = self.call("POST", "/api/route/build", {"mode": "points", "lat": 54.77, "lon": 56.03,
                                                         "end_lat": 54.78, "end_lon": 56.04})
        self.assertEqual(code, 200)
        self.assertEqual(self.call("POST", "/api/route/build", {"mode": "loop", "lat": 999, "lon": 1, "length_km": 4})[0], 400)
        self.assertEqual(self.call("POST", "/api/route/build", {"mode": "loop", "lat": 54, "lon": 56, "length_km": 9999})[0], 400)
        self.assertEqual(self.call("POST", "/api/route/build", {"mode": "zzz", "lat": 54, "lon": 56})[0], 400)
        code, j = self.call("POST", "/api/route/build", {"mode": "loop", "lat": 5, "lon": 5, "length_km": 4})
        self.assertEqual(code, 400)
        self.assertIn("out of bounds", j["error"])

    def test_04_challenge_report_lifecycle(self):
        code, j = self.call("POST", "/api/challenge/start", {"seed": 2, "source": "graphhopper",
                                                             "lat": 54.77, "lon": 56.03, "official": "health_connect"})
        self.assertEqual(code, 200)
        rid = j["report_id"]
        self.assertEqual(self.wait_challenge()["state"], "done")
        listed = self.call("GET", "/api/reports")[1]["reports"]
        meta = next(m for m in listed if m["id"] == rid)
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["summary"]["passed"], meta["summary"]["total"])
        self.assertEqual(meta["summary"]["geometry_source"], "graphhopper")
        d = self.call("GET", f"/api/reports/{rid}")[1]
        self.assertEqual(d["index"]["count"], len(d["index"]["walks"]))
        self.assertTrue(d["index"]["simulated"])
        self.assertEqual(d["final_check"]["official_adapters"][0]["connect"], False)
        z = self.call("GET", f"/api/reports/{rid}/zip", binary=True)[1]
        self.assertTrue(z.startswith(b"PK"))
        self.assertEqual(self.call("GET", "/api/reports/..%2F..%2Fetc")[0], 400)
        self.assertEqual(self.call("DELETE", f"/api/reports/{rid}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/reports/{rid}")[0], 400)

    def test_05_challenge_validation_and_gh_down(self):
        self.assertEqual(self.call("POST", "/api/challenge/start", {"total_km": 99999})[0], 400)
        self.assertEqual(self.call("POST", "/api/challenge/start", {"source": "xx"})[0], 400)
        saved = self.state.router.url
        self.state.router.url = "http://127.0.0.1:1"
        try:
            self.assertEqual(self.call("POST", "/api/challenge/start", {"source": "graphhopper"})[0], 200)
            st = self.wait_challenge()
            self.assertEqual(st["state"], "error")
            self.assertIn("GraphHopper недоступен", st["error"])
            meta = next(m for m in self.call("GET", "/api/reports")[1]["reports"] if m["id"] == st["report_id"])
            self.assertEqual(meta["status"], "error")
        finally:
            self.state.router.url = saved

    def test_06_synthetic_source_needs_no_gh(self):
        self.assertEqual(self.call("POST", "/api/challenge/start", {"source": "synthetic", "seed": 3})[0], 200)
        self.assertEqual(self.wait_challenge()["state"], "done")


if __name__ == "__main__":
    unittest.main()
