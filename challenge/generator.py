import math
import random
from datetime import datetime, timedelta, timezone

import routing as R
from .models import Walk, haversine_m

M_PER_DEG = 111194.9266
START_LAT, START_LON = 54.7757, 56.0366        # синтетическая география: окрестности Уфы
DEFAULT_VIRTUAL_START = datetime(2030, 1, 1, 6, 0, tzinfo=timezone.utc)


def split_lengths(n, total_m, rng, lo=3000.0, hi=6000.0):
    """n длин в [lo, hi], сумма ровно total_m."""
    if not lo * n <= total_m <= hi * n:
        raise ValueError(f"{n} маршрутов по {lo/1000}–{hi/1000} км не дают {total_m/1000} км")
    lengths = [lo] * n
    remaining = total_m - lo * n
    free = set(range(n))
    while remaining > 1e-9 and free:
        w = {i: rng.random() + 0.05 for i in free}
        sw = sum(w.values())
        spent = 0.0
        for i in list(free):
            add = min(remaining * w[i] / sw, hi - lengths[i])
            lengths[i] += add
            spent += add
            if hi - lengths[i] < 1e-9:
                free.discard(i)
        remaining -= spent
    lengths[-1] += total_m - sum(lengths)       # убрать накопленную погрешность float
    return lengths


def _to_latlon(lat0, lon0, xy):
    k = M_PER_DEG * math.cos(math.radians(lat0))
    return [(lat0 + y / M_PER_DEG, lon0 + x / k) for x, y in xy]


def _length(ll):
    return sum(haversine_m(a[0], a[1], b[0], b[1]) for a, b in zip(ll, ll[1:]))


def make_route(rng, length_m, lat0, lon0, ele0, avg_kmh, step_m=10.0):
    """Гладкая траектория точно заданной длины с независимой вариацией скорости."""
    n = max(2, math.ceil(length_m / step_m))
    step = length_m / n
    heading = rng.uniform(0, 2 * math.pi)
    xy = [(0.0, 0.0)]
    for _ in range(n):
        heading += math.radians(rng.gauss(0, 5))
        if rng.random() < 0.01:                              # перекрёсток/поворот
            heading += math.radians(rng.choice((-1, 1)) * rng.uniform(60, 110))
        x, y = xy[-1]
        xy.append((x + step * math.sin(heading), y + step * math.cos(heading)))

    # подгоняем длину по гаверсинусу до миллиметров (масштабирование вокруг старта)
    scale = 1.0
    for _ in range(8):
        ll = _to_latlon(lat0, lon0, [(x * scale, y * scale) for x, y in xy])
        err = length_m - _length(ll)
        if abs(err) < 1e-3:
            break
        scale *= length_m / _length(ll)

    times = assign_times(rng, [step] * n, length_m, avg_kmh)
    ele = synthetic_elevation(rng, n, step, ele0)
    return [(p[0], p[1], e, tt) for p, e, tt in zip(ll, ele, times)]


def assign_times(rng, seglens, length_m, avg_kmh):
    """Скорость: AR(1) вокруг средней, затем нормировка под точную среднюю.
    seglens — длины сегментов между соседними точками, м."""
    x_ar, rel = 0.0, []
    for _ in seglens:
        x_ar = 0.95 * x_ar + rng.gauss(0, 0.04)
        rel.append(1 + max(-0.3, min(0.3, x_ar)))
    v0 = avg_kmh / 3.6
    dts = [d / (v0 * r) for d, r in zip(seglens, rel)]
    k = (length_m / v0) / sum(dts)
    t, times = 0.0, [0.0]
    for dt in dts:
        t += dt * k
        times.append(t)
    return times


def synthetic_elevation(rng, n, step, ele0):
    A, lam, ph = rng.uniform(4, 15), rng.uniform(150, 500), rng.uniform(0, 6.28)
    return [ele0 + A * (math.sin(i * step / lam + ph) - math.sin(ph)) for i in range(n + 1)]


def route_from_geometry(rng, coords, length_m, avg_kmh, ele0, step_m=10.0):
    """Реальная пешеходная геометрия (lat, lon, ele|None) -> точки прогулки ровно length_m.
    Ломаная обрезается по длине, каждый сегмент делится на части <= step_m, все вершины
    сохраняются: точки лежат строго на трассе, углы не срезаются."""
    coords = R.trim_polyline(coords, length_m)
    pts = [coords[0]]
    for a, b in zip(coords, coords[1:]):
        d = haversine_m(a[0], a[1], b[0], b[1])
        if d <= 0:
            continue
        k = max(1, math.ceil(d / step_m))
        for j in range(1, k + 1):
            f = j / k
            ele = None if a[2] is None or b[2] is None else a[2] + (b[2] - a[2]) * f
            pts.append((a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, ele))
    seglens = [haversine_m(p[0], p[1], q[0], q[1]) for p, q in zip(pts, pts[1:])]
    total = sum(seglens)
    times = assign_times(rng, seglens, total, avg_kmh)
    if all(p[2] is not None for p in pts):
        e0 = pts[0][2]
        ele = [p[2] - e0 + ele0 for p in pts]           # сшиваем высоту с предыдущей прогулкой
    else:
        step = total / (len(pts) - 1)
        ele = synthetic_elevation(rng, len(pts) - 1, step, ele0)
    return [(p[0], p[1], e, t) for p, e, t in zip(pts, ele, times)]


def generate_set(seed, n=None, total_m=100_000.0, virtual_start=DEFAULT_VIRTUAL_START,
                 speed_range=(4.2, 5.8), step_m=10.0, router=None, start=None, progress=None):
    """Набор связных прогулок: каждая начинается там, где закончилась предыдущая.
    router — GraphHopperClient: тогда геометрия берётся с реальных пешеходных путей,
    иначе синтетическая. start — (lat, lon) начальной точки."""
    rng = random.Random(seed)
    n_min = math.ceil(total_m / 5999.5)
    n_max = int(total_m // 3000.5)
    n = n if n is not None else rng.randint(n_min, n_max)
    lengths = split_lengths(n, total_m, rng, lo=3000.5, hi=5999.5)   # запас от границ ТЗ
    walks, (lat, lon), ele = [], (start or (START_LAT, START_LON)), 100.0
    vstart = virtual_start
    for i, L in enumerate(lengths, 1):
        wseed = rng.randrange(2**31)
        wrng = random.Random(wseed)
        avg = rng.uniform(*speed_range)
        if router is not None:
            geo = router.route_at_least(lat, lon, L, seed=wseed % 100000)
            pts = route_from_geometry(wrng, geo["coords"], L, avg, ele, step_m)
        else:
            pts = make_route(wrng, L, lat, lon, ele, avg, step_m)
        w = Walk(id=f"sim-{seed}-{i:02d}", index=i, seed=wseed, start=vstart, points=pts,
                 geometry_source="graphhopper" if router is not None else "synthetic")
        walks.append(w)
        lat, lon, ele = pts[-1][0], pts[-1][1], pts[-1][2]
        vstart = w.end + timedelta(minutes=rng.randint(10, 90))   # пауза между прогулками
        if progress:
            progress(i, n)
    return walks
