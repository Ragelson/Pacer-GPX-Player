"""Превращает «идеальный» маршрут в более человеческий проход.

Берётся только геометрия (lat, lon, ele). Время строится заново:
  * скорость плавает вокруг средней (коррелированный шум, не белый);
  * перед остановкой человек притормаживает;
  * на поворотах/перекрёстках с вероятностью stop_prob ждёт (светофор, поток машин);
  * иногда короткая случайная остановка (шнурок, телефон);
  * GPS-позиция шумит и «дрейфует» (AR(1)), на стоянке тоже.
Результат детерминирован при заданном seed.
"""
import bisect
import math
import random

R_EARTH = 6371000.0


def _dist_m(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R_EARTH * math.asin(math.sqrt(a))


def _bearing(lat1, lon1, lat2, lon2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return math.degrees(math.atan2(y, x)) % 360


def _angle_diff(a, b):
    return abs((a - b + 180) % 360 - 180)


def _cumulative(pts):
    cum = [0.0]
    for p, q in zip(pts, pts[1:]):
        cum.append(cum[-1] + _dist_m(p[0], p[1], q[0], q[1]))
    return cum


def find_turns(pts, cum, turn_deg=35.0, window_m=15.0, min_gap_m=60.0):
    """Расстояния (м от старта), где маршрут заметно поворачивает."""
    turns = []
    n = len(pts)
    for i in range(1, n - 1):
        j = bisect.bisect_left(cum, cum[i] - window_m)
        k = bisect.bisect_left(cum, cum[i] + window_m)
        k = min(k, n - 1)
        if j >= i or k <= i:
            continue
        b_in = _bearing(pts[j][0], pts[j][1], pts[i][0], pts[i][1])
        b_out = _bearing(pts[i][0], pts[i][1], pts[k][0], pts[k][1])
        if _angle_diff(b_in, b_out) >= turn_deg:
            if not turns or cum[i] - turns[-1] >= min_gap_m:
                turns.append(cum[i])
    return turns


def _position(pts, cum, s):
    s = min(max(s, 0.0), cum[-1])
    i = max(1, bisect.bisect_left(cum, s))
    i = min(i, len(pts) - 1)
    seg = cum[i] - cum[i - 1]
    f = 0.0 if seg <= 0 else (s - cum[i - 1]) / seg
    a, b = pts[i - 1], pts[i]
    return (a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, a[2] + (b[2] - a[2]) * f)


def densify(pts, max_gap_m=5.0):
    """Вставляет точки, чтобы расстояние между соседними было <= max_gap_m."""
    out = [pts[0]]
    for p, q in zip(pts, pts[1:]):
        n = int(_dist_m(p[0], p[1], q[0], q[1]) // max_gap_m)
        for k in range(1, n + 1):
            f = k / (n + 1)
            out.append((p[0] + (q[0] - p[0]) * f, p[1] + (q[1] - p[1]) * f,
                        p[2] + (q[2] - p[2]) * f))
        out.append(q)
    return out


def humanize(raw_points, seed=None, mean_kmh=4.5, speed_var=0.15, gps_noise_m=2.5,
             stop_prob=0.5, stop_range=(5.0, 40.0), random_stop_per_km=0.7,
             turn_deg=35.0, step=1.0):
    """raw_points: [(lat, lon, ele, *_)]. Возвращает (points, stops):
    points — [(lat, lon, ele, t_offset_sec)] с шагом ~step сек,
    stops  — [(t_start, t_end)] интервалы, когда человек стоит."""
    pts = [(p[0], p[1], p[2]) for p in raw_points]
    if len(pts) < 2:
        return [(pts[0][0], pts[0][1], pts[0][2], 0.0)] if pts else [], []
    pts = densify(pts)
    rng = random.Random(seed)
    cum = _cumulative(pts)
    total = cum[-1]
    mean_v = mean_kmh / 3.6

    # план остановок: (расстояние, длительность)
    plan = [(d, rng.uniform(*stop_range)) for d in find_turns(pts, cum, turn_deg)
            if rng.random() < stop_prob]
    for _ in range(rng.randint(0, int(round(random_stop_per_km * total / 1000)))):
        plan.append((rng.uniform(0.1, 0.9) * total, rng.uniform(3.0, 10.0)))
    plan.sort()

    lat0 = math.radians(pts[0][0])
    m_per_deg_lat = math.pi * R_EARTH / 180
    m_per_deg_lon = m_per_deg_lat * max(math.cos(lat0), 1e-6)

    out, stops = [], []
    t, s, x = 0.0, 0.0, 0.0
    ne, nn = 0.0, 0.0                      # GPS-смещение, м
    rho_v, rho_g = 0.9, 0.85
    k_v = speed_var * math.sqrt(1 - rho_v ** 2)
    k_g = gps_noise_m * math.sqrt(1 - rho_g ** 2)

    def emit():
        lat, lon, ele = _position(pts, cum, s)
        out.append((lat + nn / m_per_deg_lat, lon + ne / m_per_deg_lon, ele, t))

    def noise_tick():
        nonlocal ne, nn
        ne = rho_g * ne + rng.gauss(0, k_g)
        nn = rho_g * nn + rng.gauss(0, k_g)

    emit()
    plan_i = 0
    while s < total:
        x = rho_v * x + rng.gauss(0, k_v)
        v = mean_v * (1 + x)
        if plan_i < len(plan):
            d_stop = plan[plan_i][0] - s
            if d_stop < 12:                # притормаживаем перед светофором
                v *= 0.4 + 0.6 * max(d_stop, 0) / 12
        v = max(v, 0.4)
        s = min(s + v * step, total)
        t += step
        noise_tick()
        emit()
        if plan_i < len(plan) and s >= plan[plan_i][0] and s < total:
            wait = plan[plan_i][1]
            t_start = t
            for _ in range(max(1, int(wait / step))):
                t += step
                noise_tick()
                emit()
            stops.append((t_start, t))
            plan_i += 1
    return out, stops
