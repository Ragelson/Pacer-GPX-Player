import math
import random
from datetime import datetime, timedelta, timezone

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

    # скорость: AR(1) вокруг средней, затем нормировка под точную среднюю
    x_ar, rel = 0.0, []
    for _ in range(n):
        x_ar = 0.95 * x_ar + rng.gauss(0, 0.04)
        rel.append(1 + max(-0.3, min(0.3, x_ar)))
    v0 = avg_kmh / 3.6
    dts = [step / (v0 * r) for r in rel]
    k = (length_m / v0) / sum(dts)
    t, times = 0.0, [0.0]
    for dt in dts:
        t += dt * k
        times.append(t)

    A, lam, ph = rng.uniform(4, 15), rng.uniform(150, 500), rng.uniform(0, 6.28)
    s = [i * step for i in range(n + 1)]
    ele = [ele0 + A * (math.sin(si / lam + ph) - math.sin(ph)) for si in s]
    return [(p[0], p[1], e, tt) for p, e, tt in zip(ll, ele, times)]


def generate_set(seed, n=None, total_m=100_000.0, virtual_start=DEFAULT_VIRTUAL_START,
                 speed_range=(4.2, 5.8), step_m=10.0):
    """Набор связных прогулок: каждая начинается там, где закончилась предыдущая."""
    rng = random.Random(seed)
    n_min = math.ceil(total_m / 6000)
    n_max = int(total_m // 3000)
    n = n if n is not None else rng.randint(n_min, n_max)
    lengths = split_lengths(n, total_m, rng)
    walks, lat, lon, ele = [], START_LAT, START_LON, 100.0
    start = virtual_start
    for i, L in enumerate(lengths, 1):
        wseed = rng.randrange(2**31)
        wrng = random.Random(wseed)
        avg = rng.uniform(*speed_range)
        pts = make_route(wrng, L, lat, lon, ele, avg, step_m)
        w = Walk(id=f"sim-{seed}-{i:02d}", index=i, seed=wseed, start=start, points=pts)
        walks.append(w)
        lat, lon, ele = pts[-1][0], pts[-1][1], pts[-1][2]
        start = w.end + timedelta(minutes=rng.randint(10, 90))   # пауза между прогулками
    return walks
