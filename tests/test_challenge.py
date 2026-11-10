import copy
import os
import sys
import tempfile
import time
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from challenge.adapters import MockPacerAdapter, OfficialHealthAdapter  # noqa: E402
from challenge.engine import Engine  # noqa: E402
from challenge.export import export_all, walk_to_gpx  # noqa: E402
from challenge.generator import generate_set, split_lengths  # noqa: E402
from challenge.validation import find_overlaps, validate_set, validate_walk  # noqa: E402
import gpx_player  # noqa: E402
import random  # noqa: E402


class GeneratorTests(unittest.TestCase):
    def test_total_exact_for_many_seeds(self):
        for seed in range(1, 9):
            ws = generate_set(seed)
            self.assertTrue(17 <= len(ws) <= 33, seed)
            self.assertAlmostEqual(sum(w.distance_m for w in ws), 100_000, delta=1.0)
            for w in ws:
                self.assertTrue(3000 <= w.distance_m <= 6000)
                self.assertTrue(4.0 <= w.avg_kmh <= 6.0)
                self.assertEqual(validate_walk(w), [])

    def test_unique_params_and_determinism(self):
        a, b = generate_set(5), generate_set(5)
        self.assertEqual([w.sha256() for w in a], [w.sha256() for w in b])
        self.assertEqual(len({round(w.avg_kmh, 4) for w in a}), len(a))
        self.assertEqual(len({w.sha256() for w in a}), len(a))

    def test_split_bounds(self):
        ls = split_lengths(17, 100_000, random.Random(1))
        self.assertAlmostEqual(sum(ls), 100_000)
        self.assertTrue(all(3000 <= x <= 6000 + 1e-6 for x in ls))
        with self.assertRaises(ValueError):
            split_lengths(10, 100_000, random.Random(1))

    def test_routes_are_connected_and_sequential(self):
        ws = generate_set(3)
        for a, b in zip(ws, ws[1:]):
            self.assertAlmostEqual(a.points[-1][0], b.points[0][0], places=7)
            self.assertLess(a.end, b.start)
        self.assertEqual(find_overlaps(ws), [])


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.walk = generate_set(2)[0]

    def test_gps_jump_rejected(self):
        w = copy.deepcopy(self.walk)
        p = list(w.points[100]); p[0] += 0.01          # ~1.1 км
        w.points[100] = tuple(p)
        self.assertTrue(any(e.startswith("gps_jump") for e in validate_walk(w)))

    def test_speed_out_of_range_rejected(self):
        w = copy.deepcopy(self.walk)
        w.points = [(p[0], p[1], p[2], p[3] / 3) for p in w.points]
        self.assertTrue(any("avg_speed" in e or "instant_speed" in e for e in validate_walk(w)))

    def test_not_simulated_rejected(self):
        w = copy.deepcopy(self.walk); w.simulated = False
        self.assertTrue(any(e.startswith("not_simulated") for e in validate_walk(w)))

    def test_non_virtual_timestamps_rejected(self):
        w = copy.deepcopy(self.walk); w.timestamps_kind = "device"
        self.assertTrue(any("timestamps_not_virtual" in e for e in validate_walk(w)))

    def test_set_checks_fail_on_wrong_total(self):
        checks = validate_set(generate_set(2)[:-1])
        self.assertFalse(checks["total_100km"][0])


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.ws = generate_set(4)
        self.ad = MockPacerAdapter()
        self.ad.connect()

    def test_duplicate_blocked(self):
        self.assertTrue(self.ad.submit_walk(self.ws[0]).ok)
        r = self.ad.submit_walk(self.ws[0])
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith("duplicate_id"))

    def test_time_conflict_detected(self):
        self.assertTrue(self.ad.submit_walk(self.ws[0]).ok)
        w = copy.deepcopy(self.ws[1]); w.start = self.ws[0].start + timedelta(minutes=5)
        r = self.ad.submit_walk(w)
        self.assertFalse(r.ok)
        self.assertTrue(r.error.startswith("time_conflict"))

    def test_source_preserved_and_discrepancy_recorded(self):
        ad = MockPacerAdapter(distance_factor=0.9)
        ad.connect()
        ad.submit_walk(self.ws[0])
        d = ad.verify_import(self.ws[0].id).details
        self.assertTrue(d["simulated"])
        self.assertEqual(d["timestamps_kind"], "virtual")
        self.assertTrue(d["sha256_match"])
        self.assertAlmostEqual(d["discrepancy_m"], self.ws[0].distance_m * 0.1, delta=0.01)

    def test_invalid_walk_rejected_by_mock(self):
        w = copy.deepcopy(self.ws[0]); w.simulated = False
        self.assertFalse(self.ad.submit_walk(w).ok)

    def test_official_adapter_returns_clear_error(self):
        off = OfficialHealthAdapter("apple_health")
        self.assertFalse(off.connect())
        caps = off.get_capabilities()
        self.assertFalse(caps["supportsSimulatedData"])
        r = off.submit_walk(self.ws[0])
        self.assertFalse(r.ok)
        self.assertIn("unsupported", r.error)


class EngineAndExportTests(unittest.TestCase):
    def test_full_run_fast_and_logged(self):
        ws = generate_set(1)
        with tempfile.TemporaryDirectory() as d:
            log = os.path.join(d, "log.jsonl")
            t0 = time.monotonic()
            rep = Engine(MockPacerAdapter(), log).run(ws, [OfficialHealthAdapter()])
            self.assertLess(time.monotonic() - t0, 30)
            self.assertTrue(all(ok for ok, _ in rep["checks"].values()),
                            {k: v for k, v in rep["checks"].items() if not v[0]})
            self.assertGreater(rep["virtual_s"] / rep["wall_s"], 1000)
            with open(log, encoding="utf-8") as fh:
                lines = fh.read().splitlines()
            self.assertEqual(len(lines), len(ws) + 2)

    def test_budget_stops_run(self):
        rep = Engine(MockPacerAdapter(), None, budget_s=-1).run(generate_set(1))
        self.assertFalse(rep["checks"]["all_imported"][0])

    def test_accel_pacing(self):
        ws = generate_set(1, n=17)[:1]
        t0 = time.monotonic()
        Engine(MockPacerAdapter(), None, accel=ws[0].duration_s / 0.3).run(ws)
        self.assertGreaterEqual(time.monotonic() - t0, 0.28)

    def test_export_marks_simulated_and_gpx_parses(self):
        ws = generate_set(1)[:2]
        gpx = walk_to_gpx(ws[0])
        self.assertIn("SIMULATED", gpx)
        with tempfile.TemporaryDirectory() as d:
            idx = export_all(ws, d)
            self.assertTrue(idx["simulated"])
            pts = gpx_player.parse_gpx(os.path.join(d, "walks", f"{ws[0].id}.gpx"))
            self.assertEqual(len(pts), len(ws[0].points))


if __name__ == "__main__":
    unittest.main()
