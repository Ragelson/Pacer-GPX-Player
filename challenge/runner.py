import time
from pathlib import Path

from .adapters import MockPacerAdapter, OfficialHealthAdapter
from .engine import Engine
from .export import export_all, write_report
from .generator import generate_set


def run_challenge(out_dir, seed=1, n=None, total_m=100_000.0, accel=None, budget_s=1200.0,
                  distance_factor=1.0, official=None, router=None, start=None,
                  log=print, progress=None):
    """Полный прогон: генерация -> экспорт -> движок -> отчёт. Возвращает (summary, report, walks)."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    runlog = out / "run_log.jsonl"
    runlog.unlink(missing_ok=True)

    t0 = time.monotonic()
    walks = generate_set(seed, n, total_m, router=router, start=start, progress=progress)
    export_all(walks, out)
    gen_s = time.monotonic() - t0
    src = "GraphHopper" if router is not None else "синтетическая геометрия"
    log(f"[+] Сгенерировано {len(walks)} прогулок ({src}), "
        f"{sum(w.distance_m for w in walks)/1000:.4f} км за {gen_s:.2f} с")

    eng = Engine(MockPacerAdapter(distance_factor), str(runlog), accel or None, budget_s)
    extra = [OfficialHealthAdapter(official)] if official else []
    rep = eng.run(walks, extra)
    rep["wall_s"] += gen_s                       # в бюджет входит и генерация
    summary = write_report(rep, walks, out, seed)
    summary["geometry_source"] = "graphhopper" if router is not None else "synthetic"
    return summary, rep, walks
