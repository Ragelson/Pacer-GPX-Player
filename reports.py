"""Хранилище отчётов: каждый прогон (челлендж или проигрыш) — своя папка с meta.json."""
import io
import json
import re
import shutil
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")


class ReportStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _dir(self, rid):
        if not isinstance(rid, str) or not ID_RE.match(rid) or rid in (".", ".."):
            raise ValueError("Недопустимый id отчёта")
        return self.root / rid

    def new(self, kind, title):
        with self._lock:
            base = f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}"
            rid, k = base, 1
            while (self.root / rid).exists():
                k += 1
                rid = f"{base}-{k}"
            (self.root / rid).mkdir(parents=True)
        self.update(rid, id=rid, type=kind, title=title, status="running",
                    created=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        return rid, self.root / rid

    def update(self, rid, **kw):
        d = self._dir(rid)
        f = d / "meta.json"
        with self._lock:
            meta = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
            meta.update(kw)
            f.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        return meta

    def list(self):
        out = []
        for d in self.root.iterdir():
            f = d / "meta.json"
            if d.is_dir() and f.exists():
                try:
                    out.append(json.loads(f.read_text(encoding="utf-8")))
                except json.JSONDecodeError:
                    pass
        return sorted(out, key=lambda m: m.get("created", ""), reverse=True)

    def get(self, rid):
        d = self._dir(rid)
        if not (d / "meta.json").exists():
            raise ValueError("Отчёт не найден")

        def load(name):
            f = d / name
            return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None

        def text(name):
            f = d / name
            return f.read_text(encoding="utf-8") if f.exists() else None

        return {"meta": load("meta.json"), "final_check": load("final_check.json"),
                "index": load("index.json"), "report_md": text("report.md"), "log": text("log.txt")}

    def zip_bytes(self, rid):
        d = self._dir(rid)
        if not d.exists():
            raise ValueError("Отчёт не найден")
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for p in sorted(d.rglob("*")):
                if p.is_file():
                    z.write(p, p.relative_to(d))
        return buf.getvalue()

    def delete(self, rid):
        d = self._dir(rid)
        if d.exists():
            shutil.rmtree(d)
