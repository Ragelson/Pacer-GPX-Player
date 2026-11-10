"""Мини-имитация GraphHopper для тестов: /health и /route (A->B и round_trip).
Кольцо намеренно получается КОРОЧЕ запрошенного (60%), как и бывает у настоящего."""
import json
import math
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


def _square(lat, lon, perimeter_m):
    side = perimeter_m / 4
    dlat = side / 111195
    dlon = side / (111195 * math.cos(math.radians(lat)))
    pts = [(lat, lon), (lat + dlat, lon), (lat + dlat, lon + dlon), (lat, lon + dlon), (lat, lon)]
    return [[p[1], p[0]] for p in pts]


def _dist(a, b):
    r = 6371000.0
    p1, p2 = math.radians(a[1]), math.radians(b[1])
    dl = math.radians(b[0] - a[0])
    x = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(x))


class _H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, obj, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/health":
            return self._send(200, None, b"OK")
        q = parse_qs(u.query)
        pts = [tuple(map(float, p.split(","))) for p in q.get("point", [])]
        if not pts or any(not (50 <= la <= 60 and 50 <= lo <= 60) for la, lo in pts):
            return self._send(400, {"message": "Point 0 is out of bounds"})
        if q.get("algorithm") == ["round_trip"]:
            line = _square(pts[0][0], pts[0][1], 0.6 * float(q["round_trip.distance"][0]))
        else:
            line = [[lo, la] for la, lo in pts]
        dist = sum(_dist(a, b) for a, b in zip(line, line[1:]))
        self._send(200, {"paths": [{"distance": dist, "time": int(dist / 1.3 * 1000),
                                    "points": {"type": "LineString", "coordinates": line}}]})


def start():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"
