import json
from pathlib import Path
from xml.sax.saxutils import escape

from . import SOURCE


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def walk_to_gpx(w):
    pts = "\n".join(
        f'<trkpt lat="{p[0]:.7f}" lon="{p[1]:.7f}"><ele>{p[2]:.2f}</ele>'
        f'<time>{_iso(w.point_time(p))}</time></trkpt>' for p in w.points)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<gpx version="1.1" creator="{SOURCE}" xmlns="http://www.topografix.com/GPX/1.1">\n'
        f'<metadata><name>SIMULATED {escape(w.id)}</name>'
        '<desc>SIMULATED DATA. Timestamps are virtual, not device measurements.</desc>'
        '<keywords>simulated,virtual-time</keywords></metadata>\n'
        f'<trk><name>SIMULATED {escape(w.id)}</name><type>walking</type><trkseg>\n{pts}\n</trkseg></trk>\n</gpx>\n')


def export_all(walks, out_dir):
    out = Path(out_dir)
    (out / "walks").mkdir(parents=True, exist_ok=True)
    for w in walks:
        (out / "walks" / f"{w.id}.json").write_text(
            json.dumps(w.to_dict(), ensure_ascii=False), encoding="utf-8")
        (out / "walks" / f"{w.id}.gpx").write_text(walk_to_gpx(w), encoding="utf-8")
    index = {"simulated": True, "source": SOURCE, "count": len(walks),
             "total_distance_m": round(sum(w.distance_m for w in walks), 3),
             "walks": [w.to_dict(with_points=False) for w in walks]}
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    return index


def write_report(report, walks, out_dir, seed):
    out = Path(out_dir)
    checks = {k: {"ok": ok, "detail": d} for k, (ok, d) in report["checks"].items()}
    passed = sum(c["ok"] for c in checks.values())
    summary = {"seed": seed, "passed": passed, "total": len(checks), "checks": checks,
               "wall_s": round(report["wall_s"], 3), "virtual_s": round(report["virtual_s"], 1),
               "official_adapters": report["official"],
               "verified": [{"id": i, **(v.details if v else {"error": r.error})}
                            for i, r, v in report["results"]]}
    (out / "final_check.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    lines = [f"# Итог прогона (seed={seed})", "",
             f"- Прогулок: {len(walks)}, дистанция: {sum(w.distance_m for w in walks)/1000:.4f} км",
             f"- Виртуальное время: {report['virtual_s']/3600:.2f} ч, реальное: {report['wall_s']:.2f} с",
             f"- Проверок пройдено: {passed} / {len(checks)}", ""]
    lines += [f"- {'✅' if c['ok'] else '❌'} `{k}` {c['detail']}" for k, c in checks.items()]
    if report["official"]:
        lines += ["", "## Официальные адаптеры"]
        for o in report["official"]:
            lines.append(f"- `{o['adapter']}`: connect={o['connect']}, возможности={o['capabilities']}")
            lines.append(f"  - {o['result']}")
    lines += ["", "Все данные симулированы (`simulated: true`), временные метки виртуальные."]
    (out / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary
