import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fake_graphhopper  # noqa: E402
import routing  # noqa: E402
from challenge.generator import generate_set  # noqa: E402
from challenge.validation import validate_set, validate_walk  # noqa: E402


class RoutingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv, cls.url = fake_graphhopper.start()
        cls.gh = routing.GraphHopperClient(cls.url)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_health_and_down(self):
        self.assertTrue(self.gh.health())
        self.assertFalse(routing.GraphHopperClient("http://127.0.0.1:1").health())

    def test_route_a_to_b(self):
        r = self.gh.route([(54.77, 56.03), (54.78, 56.04)])
        self.assertEqual(len(r["coords"]), 2)
        self.assertGreater(r["distance_m"], 1000)

    def test_errors_are_readable(self):
        with self.assertRaises(routing.RouterError) as cm:
            self.gh.route([(10, 10), (11, 11)])
        self.assertIn("out of bounds", str(cm.exception))
        with self.assertRaises(routing.RouterError) as cm:
            routing.GraphHopperClient("http://127.0.0.1:1").route([(54, 56), (54.1, 56.1)])
        self.assertIn("недоступен", str(cm.exception))

    def test_round_trip_is_approximate_and_route_at_least_compensates(self):
        raw = self.gh.round_trip(54.77, 56.03, 4000)
        self.assertLess(raw["distance_m"], 3000)                # фейк занижает длину
        r = self.gh.route_at_least(54.77, 56.03, 4000)
        self.assertGreaterEqual(r["distance_m"], 4000)
        t = routing.trim_polyline(r["coords"], 4000)
        self.assertAlmostEqual(routing.polyline_length(t), 4000, delta=0.01)

    def test_trim_too_short_raises(self):
        with self.assertRaises(routing.RouterError):
            routing.trim_polyline([(54.0, 56.0, None), (54.001, 56.0, None)], 5000)

    def test_gpx_export_parses_back(self):
        import gpx_player
        import tempfile
        gpx = routing.route_to_gpx([(54.77, 56.03, 100.0), (54.78, 56.04, 105.0)], "t <x>")
        with tempfile.NamedTemporaryFile("w", suffix=".gpx", delete=False, encoding="utf-8") as f:
            f.write(gpx)
        try:
            pts = gpx_player.parse_gpx(f.name)
        finally:
            os.remove(f.name)
        self.assertEqual(len(pts), 2)
        self.assertEqual(pts[0][2], 100.0)

    def test_challenge_with_router_geometry_is_exact_and_valid(self):
        for seed in (1, 2):
            ws = generate_set(seed, router=self.gh, start=(54.77, 56.03))
            self.assertAlmostEqual(sum(w.distance_m for w in ws), 100_000, delta=0.05)
            self.assertTrue(all(validate_walk(w) == [] for w in ws))
            self.assertTrue(all(w.geometry_source == "graphhopper" for w in ws))
            checks = validate_set(ws)
            self.assertTrue(all(ok for ok, _ in checks.values()),
                            {k: v for k, v in checks.items() if not v[0]})

    def test_router_failure_propagates(self):
        with self.assertRaises(routing.RouterError):
            generate_set(1, router=self.gh, start=(10.0, 10.0))


@unittest.skipUnless(os.environ.get("GRAPHHOPPER_URL"), "нужен настоящий GraphHopper (GRAPHHOPPER_URL)")
class RealGraphHopperTests(unittest.TestCase):
    """Запуск: GRAPHHOPPER_URL=http://127.0.0.1:8989 GH_LAT=.. GH_LON=.. python -m unittest tests.test_routing"""

    def test_real_challenge(self):
        gh = routing.GraphHopperClient(os.environ["GRAPHHOPPER_URL"])
        start = (float(os.environ.get("GH_LAT", 54.7770)), float(os.environ.get("GH_LON", 56.0390)))
        ws = generate_set(1, router=gh, start=start)
        self.assertAlmostEqual(sum(w.distance_m for w in ws), 100_000, delta=0.05)
        self.assertTrue(all(ok for ok, _ in validate_set(ws).values()))


if __name__ == "__main__":
    unittest.main()
