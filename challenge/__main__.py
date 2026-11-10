import argparse
import sys

from .runner import run_challenge


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m challenge",
                                description="100 км симулированных прогулок за ≤20 минут")
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--n", type=int, help="число маршрутов (17–33), по умолчанию случайно по seed")
    p.add_argument("--total-km", type=float, default=100.0)
    p.add_argument("--out", default="out")
    p.add_argument("--accel", type=float, default=0.0,
                   help="пейсинг воспроизведения: виртуальных сек на реальную (0 = максимально быстро)")
    p.add_argument("--budget-min", type=float, default=20.0)
    p.add_argument("--distance-factor", type=float, default=1.0,
                   help="mock: какую долю дистанции «показывает» приёмник (демо расхождения)")
    p.add_argument("--official", choices=["health_connect", "apple_health"],
                   help="добавить в отчёт проверку официального адаптера (вернёт понятную ошибку)")
    p.add_argument("--graphhopper", metavar="URL",
                   help="брать геометрию с GraphHopper (напр. http://127.0.0.1:8989), иначе синтетика")
    p.add_argument("--start", type=float, nargs=2, metavar=("LAT", "LON"), help="начальная точка")
    a = p.parse_args(argv)

    router = None
    if a.graphhopper:
        import routing
        router = routing.GraphHopperClient(a.graphhopper)
        if not router.health():
            print(f"[!] GraphHopper недоступен: {a.graphhopper}")
            return 2
    start = tuple(a.start) if a.start else None
    try:
        summary, rep, walks = run_challenge(a.out, a.seed, a.n, a.total_km * 1000, a.accel or None,
                                            a.budget_min * 60, a.distance_factor, a.official,
                                            router, start)
    except Exception as e:
        print(f"[!] {e}")
        return 2
    for k, c in summary["checks"].items():
        print(f"  {'OK ' if c['ok'] else 'FAIL'} {k} {c['detail']}")
    print(f"[+] Проверок пройдено: {summary['passed']} / {summary['total']}; "
          f"виртуальное время {rep['virtual_s']/3600:.2f} ч, реальное {rep['wall_s']:.2f} с")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
