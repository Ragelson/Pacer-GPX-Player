"""Клиент GraphHopper (самохостинг, профиль foot) и работа с геометрией маршрута.

GraphHopper отдаёт координаты как [lon, lat(, ele)]; здесь они приводятся к (lat, lon, ele|None).
"""
import json
import math
import os
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_URL = os.environ.get("GRAPHHOPPER_URL", "http://127.0.0.1:8989")
R_EARTH = 6371000.0


class RouterError(ValueError):
    """Понятная ошибка маршрутизатора (для показа пользователю)."""


def haversine_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def cumulative(coords):
    cum = [0.0]
    for a, b in zip(coords, coords[1:]):
        cum.append(cum[-1] + haversine_m(a[0], a[1], b[0], b[1]))
    return cum


def polyline_length(coords):
    return cumulative(coords)[-1] if len(coords) > 1 else 0.0


def point_at(coords, cum, s):
    """Точка на ломаной на расстоянии s от начала (линейная интерполяция, ele тоже)."""
    s = min(max(s, 0.0), cum[-1])
    lo, hi = 0, len(cum) - 1
    while lo < hi - 1:
        mid = (lo + hi) // 2
        if cum[mid] <= s:
            lo = mid
        else:
            hi = mid
    seg = cum[hi] - cum[lo]
    f = 0.0 if seg <= 0 else (s - cum[lo]) / seg
    a, b = coords[lo], coords[hi]
    ele = None if a[2] is None or b[2] is None else a[2] + (b[2] - a[2]) * f
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, ele)


def trim_polyline(coords, length_m):
    """Обрезает ломаную ровно до length_m (по гаверсинусу)."""
    cum = cumulative(coords)
    if cum[-1] < length_m - 1e-6:
        raise RouterError(f"Маршрут короче запрошенного: {cum[-1]:.0f} м < {length_m:.0f} м")
    out = [c for c, d in zip(coords, cum) if d < length_m]
    out.append(point_at(coords, cum, length_m))
    return out


def elevation_stats(coords):
    ele = [c[2] for c in coords if c[2] is not None]
    if not ele:
        return {"has_elevation": False}
    up = down = 0.0
    for a, b in zip(ele, ele[1:]):
        d = b - a
        up += max(d, 0)
        down += max(-d, 0)
    return {"has_elevation": True, "ele_min": min(ele), "ele_max": max(ele),
            "ascent_m": up, "descent_m": down}


class GraphHopperClient:
    def __init__(self, url=None, profile="foot", timeout=30.0):
        self.url = (url or DEFAULT_URL).rstrip("/")
        self.profile = profile
        self.timeout = timeout

    # -- низкий уровень --
    def _get(self, path, params, timeout=None):
        q = urllib.parse.urlencode(params, doseq=True)
        try:
            with urllib.request.urlopen(f"{self.url}{path}?{q}", timeout=timeout or self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("message", e.reason)
            except Exception:
                msg = e.reason
            raise RouterError(f"GraphHopper: {msg}")
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise RouterError(f"GraphHopper недоступен по адресу {self.url}: {getattr(e, 'reason', e)}")
        except json.JSONDecodeError:
            raise RouterError("GraphHopper вернул не JSON")

    def health(self):
        try:
            with urllib.request.urlopen(f"{self.url}/health", timeout=1.5) as r:
                return r.status == 200
        except Exception:
            return False

    def _route(self, extra):
        params = {"profile": self.profile, "points_encoded": "false", "instructions": "false",
                  "elevation": "true", **extra}
        try:
            j = self._get("/route", params)
        except RouterError as e:
            if "elevation" in str(e).lower():          # нет провайдера высот — повторим без них
                params["elevation"] = "false"
                j = self._get("/route", params)
            else:
                raise
        paths = j.get("paths") or []
        if not paths:
            raise RouterError("GraphHopper не вернул маршрут")
        p = paths[0]
        coords = [(c[1], c[0], c[2] if len(c) > 2 else None) for c in p["points"]["coordinates"]]
        return {"coords": coords, "distance_m": float(p["distance"]),
                "time_s": float(p.get("time", 0)) / 1000.0}

    # -- публичные методы --
    def route(self, points):
        """Маршрут через последовательность точек [(lat, lon), ...] (>= 2)."""
        if len(points) < 2:
            raise RouterError("Нужно минимум две точки")
        return self._route({"point": [f"{lat},{lon}" for lat, lon in points]})

    def round_trip(self, lat, lon, distance_m, seed=0):
        """Кольцо от точки; длина у GraphHopper приближённая."""
        return self._route({"point": f"{lat},{lon}", "algorithm": "round_trip",
                            "round_trip.distance": int(distance_m), "round_trip.seed": int(seed)})

    def route_at_least(self, lat, lon, length_m, seed=0, tries=8):
        """Маршрут по реальным дорожкам длиной >= length_m (кольцо, при необходимости с запасом).
        Затем вызывающий обрезает его до точной длины через trim_polyline."""
        factor, last = 1.15, None
        for k in range(tries):
            r = self.round_trip(lat, lon, length_m * factor, seed + k)
            last = r
            if r["distance_m"] >= length_m * 1.002:
                return r
            ratio = max(r["distance_m"], 1.0) / (length_m * factor)
            factor = min(factor / ratio * 1.08, 4.0)
        raise RouterError(
            f"Не удалось построить маршрут длиной {length_m:.0f} м от ({lat:.5f}, {lon:.5f}); "
            f"максимум получилось {last['distance_m']:.0f} м. Проверь, что точка в зоне карты "
            "GraphHopper и рядом есть пешеходные дорожки.")


def route_to_gpx(coords, name, creator="pacer-gpx-router"):
    """GPX-трек только с геометрией (без <time>): время задаёт проигрыватель."""
    from xml.sax.saxutils import escape
    pts = "\n".join(
        f'<trkpt lat="{c[0]:.7f}" lon="{c[1]:.7f}">' + (f"<ele>{c[2]:.2f}</ele>" if c[2] is not None else "")
        + "</trkpt>" for c in coords)
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<gpx version="1.1" creator="{creator}" xmlns="http://www.topografix.com/GPX/1.1">\n'
            f'<metadata><name>{escape(name)}</name><keywords>route,graphhopper,geometry-only</keywords></metadata>\n'
            f'<trk><name>{escape(name)}</name><type>walking</type><trkseg>\n{pts}\n</trkseg></trk>\n</gpx>\n')
