from .models import haversine_m

MIN_LEN_M, MAX_LEN_M = 3000.0, 6000.0
MIN_AVG_KMH, MAX_AVG_KMH = 4.0, 6.0
MAX_INSTANT_KMH = 10.0
MAX_JUMP_M = 50.0
TOTAL_TOLERANCE_M = 10.0


def validate_walk(w):
    """Список ошибок для одной прогулки (пусто = ок)."""
    errs = []
    if not w.simulated:
        errs.append("not_simulated: прогулка не помечена simulated=true")
    if w.timestamps_kind != "virtual":
        errs.append("timestamps_not_virtual: метки времени должны быть помечены как виртуальные")
    p = w.points
    if len(p) < 2:
        return errs + ["too_few_points"]
    for i, (a, b) in enumerate(zip(p, p[1:]), 1):
        d, dt = haversine_m(a[0], a[1], b[0], b[1]), b[3] - a[3]
        if dt <= 0:
            errs.append(f"time_not_monotonic: точка {i}")
            break
        if d > MAX_JUMP_M:
            errs.append(f"gps_jump: {d:.0f} м между точками {i - 1} и {i}")
            break
        if d / dt * 3.6 > MAX_INSTANT_KMH:
            errs.append(f"instant_speed: {d / dt * 3.6:.1f} км/ч в точке {i}")
            break
    if not MIN_LEN_M <= w.distance_m <= MAX_LEN_M:
        errs.append(f"length_out_of_range: {w.distance_m:.0f} м")
    if not MIN_AVG_KMH <= w.avg_kmh <= MAX_AVG_KMH:
        errs.append(f"avg_speed_out_of_range: {w.avg_kmh:.2f} км/ч")
    return errs


def find_overlaps(walks):
    ws = sorted(walks, key=lambda w: w.start)
    return [(a.id, b.id) for a, b in zip(ws, ws[1:]) if b.start < a.end]


def validate_set(walks, total_m=100_000.0, n_range=(17, 33)):
    """Контрольные проверки набора. Возвращает {имя: (ok, подробности)}."""
    ids = [w.id for w in walks]
    dist = sum(w.distance_m for w in walks)
    bad = {w.id: validate_walk(w) for w in walks if validate_walk(w)}
    return {
        "count_17_33": (n_range[0] <= len(walks) <= n_range[1], f"{len(walks)} маршрутов"),
        "total_100km": (abs(dist - total_m) <= TOTAL_TOLERANCE_M, f"{dist:.3f} м"),
        "each_3_6km": (all(MIN_LEN_M <= w.distance_m <= MAX_LEN_M for w in walks), ""),
        "each_speed_4_6": (all(MIN_AVG_KMH <= w.avg_kmh <= MAX_AVG_KMH for w in walks), ""),
        "unique_ids": (len(ids) == len(set(ids)), ""),
        "no_overlaps": (not find_overlaps(walks), str(find_overlaps(walks))),
        "walks_valid": (not bad, str(bad) if bad else ""),
        "all_simulated": (all(w.simulated and w.timestamps_kind == "virtual" for w in walks), ""),
    }
