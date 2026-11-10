import json
import time
from datetime import datetime, timezone

from .validation import validate_set


class Engine:
    """Два независимых времени: виртуальное (то, что описывают данные) и реальное
    (wall-clock выполнения программы). Ускорение влияет только на пейсинг воспроизведения
    внутри симулятора, но не на системные часы и не на метки времени в данных."""

    def __init__(self, adapter, log_path=None, accel=None, budget_s=1200.0):
        self.adapter, self.accel, self.budget_s = adapter, accel, budget_s
        self.log_path = log_path
        self.t0 = None
        self.entries = []

    def _log(self, event, **kw):
        e = {"event": event, "wall_s": round(time.monotonic() - self.t0, 4),
             "wall_utc": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **kw}
        self.entries.append(e)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def run(self, walks, extra_adapters=()):
        self.t0 = time.monotonic()
        self._log("run_start", walks=len(walks), accel=self.accel, budget_s=self.budget_s)
        results, virtual_total = [], 0.0
        self.adapter.connect()
        for w in walks:
            w_t0 = time.monotonic()
            if time.monotonic() - self.t0 > self.budget_s:
                self._log("budget_exceeded", walk=w.id)
                break
            res = self.adapter.submit_walk(w)
            ver = self.adapter.verify_import(w.id) if res.ok else None
            virtual_total += w.duration_s
            self._log("walk", walk=w.id, ok=res.ok, error=res.error,
                      virtual_start=w.start.isoformat(), virtual_end=w.end.isoformat(),
                      virtual_duration_s=round(w.duration_s, 1),
                      distance_m=round(w.distance_m, 3), avg_kmh=round(w.avg_kmh, 3),
                      verify=ver.details if ver else None)
            results.append((w.id, res, ver))
            if self.accel:                       # пейсинг: virtual_s / accel секунд реального времени
                left = w.duration_s / self.accel - (time.monotonic() - w_t0)
                if left > 0:
                    time.sleep(left)
        unsupported = []
        for ad in extra_adapters:
            ok = ad.connect()
            r = ad.submit_walk(walks[0]) if walks else None
            unsupported.append({"adapter": type(ad).__name__, "connect": ok,
                                "capabilities": ad.get_capabilities(),
                                "result": r.error if r else None})
        wall = time.monotonic() - self.t0
        self._log("run_end", wall_s_total=round(wall, 3), virtual_s_total=round(virtual_total, 1),
                  speedup=round(virtual_total / wall, 1) if wall else None)
        checks = validate_set(walks)
        checks["time_le_20min"] = (wall <= 1200.0, f"{wall:.2f} с")
        checks["time_budget"] = (wall <= self.budget_s, f"{wall:.2f} с из {self.budget_s:.0f}")
        checks["all_imported"] = (all(r.ok for _, r, _ in results) and len(results) == len(walks), "")
        return {"wall_s": wall, "virtual_s": virtual_total, "results": results,
                "checks": checks, "official": unsupported}
