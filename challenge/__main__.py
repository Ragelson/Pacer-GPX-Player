import argparse
import os
import sys
from pathlib import Path

from .adapters import MockPacerAdapter, OfficialHealthAdapter
from .engine import Engine
from .export import export_all, write_report
from .generator import generate_set


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
    a = p.parse_args(argv)

    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    log = out / "run_log.jsonl"
    log.unlink(missing_ok=True)

    import time
    t0 = time.monotonic()
    walks = generate_set(a.seed, a.n, a.total_km * 1000)
    export_all(walks, out)
    gen_s = time.monotonic() - t0
    print(f"[+] Сгенерировано {len(walks)} прогулок, {sum(w.distance_m for w in walks)/1000:.4f} км "
          f"за {gen_s:.2f} с -> {out}/")

    eng = Engine(MockPacerAdapter(a.distance_factor), str(log), a.accel or None, a.budget_min * 60)
    extra = [OfficialHealthAdapter(a.official)] if a.official else []
    rep = eng.run(walks, extra)
    rep["wall_s"] += gen_s                       # в бюджет входит и генерация
    summary = write_report(rep, walks, out, a.seed)
    for k, c in summary["checks"].items():
        print(f"  {'OK ' if c['ok'] else 'FAIL'} {k} {c['detail']}")
    print(f"[+] Проверок пройдено: {summary['passed']} / {summary['total']}; "
          f"виртуальное время {rep['virtual_s']/3600:.2f} ч, реальное {rep['wall_s']:.2f} с")
    return 0 if summary["passed"] == summary["total"] else 1


if __name__ == "__main__":
    sys.exit(main())
