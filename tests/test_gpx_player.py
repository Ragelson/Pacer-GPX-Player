import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import gpx_player as g

GPX_TIMED = """<?xml version="1.0"?>
<gpx version="1.1" xmlns="http://www.topografix.com/GPX/1.1">
<trk><trkseg>
<trkpt lat="54.7757" lon="56.0366"><ele>100</ele><time>2025-05-12T09:30:00Z</time></trkpt>
<trkpt lat="54.7767" lon="56.0366"><ele>110</ele><time>2025-05-12T09:30:10.500Z</time></trkpt>
</trkseg></trk></gpx>"""

GPX_NO_TIME = """<?xml version="1.0"?>
<gpx version="1.0"><trk><trkseg>
<trkpt lat="54.7757" lon="56.0366"><ele>100</ele></trkpt>
<trkpt lat="54.7767" lon="56.0366"><ele>101</ele></trkpt>
</trkseg></trk></gpx>"""


def write(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".gpx", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    self_cleanup.append(f.name)
    return f.name


self_cleanup = []


class Tests(unittest.TestCase):
    def tearDown(self):
        while self_cleanup:
            os.remove(self_cleanup.pop())

    def test_parse_with_and_without_millis(self):
        pts = g.parse_gpx(write(GPX_TIMED))
        self.assertEqual(len(pts), 2)
        self.assertAlmostEqual((pts[1][3] - pts[0][3]).total_seconds(), 10.5)

    def test_parse_no_namespace_no_time(self):
        pts = g.parse_gpx(write(GPX_NO_TIME), walk_speed_kmh=3.6)  # 1 м/с
        self.assertEqual(len(pts), 2)
        dist = g.haversine_m(*pts[0][:2], *pts[1][:2])
        self.assertAlmostEqual((pts[1][3] - pts[0][3]).total_seconds(), dist, delta=0.01)

    def test_interpolate(self):
        pts = g.interpolate(g.parse_gpx(write(GPX_TIMED)), 2.0)
        self.assertEqual(pts[0][3], 0)
        self.assertAlmostEqual(pts[-1][3], 10.5)
        self.assertEqual(len(pts), 5 + 1)
        self.assertTrue(all(a[3] < b[3] for a, b in zip(pts, pts[1:])))

    def test_pressure(self):
        self.assertAlmostEqual(g.elevation_to_pressure_hpa(0), 1013.25)
        self.assertAlmostEqual(g.elevation_to_pressure_hpa(200), 989.5, delta=0.5)

    def test_dry_run(self):
        g.play(write(GPX_TIMED), dry_run=True)


if __name__ == "__main__":
    unittest.main()
