#!/usr/bin/env python3
"""Веб-интерфейс управления Pacer GPX Player (только стандартная библиотека).

Запуск:
    python3 web_server.py --bind 10.8.0.1 --port 8080
Токен доступа: переменная PACER_WEB_TOKEN или файл ~/.pacer-gpx/web_token
(создаётся автоматически при первом запуске).

Слушай только адрес WireGuard-интерфейса (или 127.0.0.1) — консоль эмулятора
и этот интерфейс не должны быть доступны из интернета.
"""
import argparse
import collections
import re as _re
import hmac
import json
import os
import re
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import gpx_player as gp
import routing
from challenge.runner import run_challenge
from reports import ReportStore

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("PACER_DATA_DIR", Path.home() / ".pacer-gpx"))
TRACKS_DIR = DATA_DIR / "tracks"
MAX_UPLOAD = 10 * 1024 * 1024
MAX_JSON = 64 * 1024
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}\.gpx$")

# допустимые параметры: имя -> (тип, минимум, максимум, по умолчанию)
PARAMS = {
    "speed": (float, 0.1, 10.0, gp.SPEED_MULTIPLIER),
    "cadence": (float, 0.5, 4.0, gp.CADENCE_HZ),
    "step": (float, 0.5, 10.0, gp.INTERP_STEP_SEC),
    "walk_kmh": (float, 1.0, 15.0, gp.WALK_SPEED_KMH),
    "stop_prob": (float, 0.0, 1.0, 0.5),
    "gps_noise": (float, 0.0, 20.0, 2.5),
    "seed": (int, 0, 2**31 - 1, None),
}


class State:
    def __init__(self, emu_host, emu_port, gh_url=None, reports_dir=None):
        self.emu_host, self.emu_port = emu_host, emu_port
        self.router = routing.GraphHopperClient(gh_url)
        self.reports = ReportStore(reports_dir or (DATA_DIR / "reports"))
        self.built = collections.OrderedDict()          # id -> координаты построенного маршрута
        self.challenge = {"state": "idle"}
        self.challenge_job = None
        self.lock = threading.Lock()
        self.log = collections.deque(maxlen=500)
        self.job = None
        self.cancel = threading.Event()
        self.status = {"state": "idle"}
        self.emu_busy = False

    def add_log(self, line):
        with self.lock:
            self.log.append(f"{time.strftime('%H:%M:%S')} {line}")

    def set_status(self, **kw):
        with self.lock:
            self.status.update(kw)

    def snapshot(self, log_from=0):
        with self.lock:
            logs = list(self.log)
            return dict(self.status), logs[log_from:], len(logs)

    def emulator_up(self):
        try:
            with socket.create_connection((self.emu_host, self.emu_port), timeout=0.5):
                return True
        except OSError:
            return False


def clean_params(raw):
    out = {}
    for key, (typ, lo, hi, default) in PARAMS.items():
        val = raw.get(key, default)
        if val in (None, ""):
            out[key] = default
            continue
        try:
            val = typ(val)
        except (TypeError, ValueError):
            raise ValueError(f"Параметр {key}: неверное значение")
        if not lo <= val <= hi:
            raise ValueError(f"Параметр {key}: допустимо {lo}…{hi}")
        out[key] = val
    out["human"] = bool(raw.get("human"))
    return out


def _num(raw, key, typ, lo, hi, default=None, required=False):
    val = raw.get(key, default)
    if val in (None, ""):
        if required:
            raise ValueError(f"Поле {key} обязательно")
        return default
    try:
        val = typ(val)
    except (TypeError, ValueError):
        raise ValueError(f"Поле {key}: неверное значение")
    if not lo <= val <= hi:
        raise ValueError(f"Поле {key}: допустимо {lo}…{hi}")
    return val


def clean_challenge(raw):
    src = raw.get("source", "graphhopper")
    if src not in ("graphhopper", "synthetic"):
        raise ValueError("source: graphhopper|synthetic")
    off = raw.get("official") or None
    if off not in (None, "health_connect", "apple_health"):
        raise ValueError("official: health_connect|apple_health")
    return {
        "seed": _num(raw, "seed", int, 0, 2**31 - 1, 1),
        "n": _num(raw, "n", int, 1, 200),
        "total_km": _num(raw, "total_km", float, 1.0, 500.0, 100.0),
        "source": src,
        "accel": _num(raw, "accel", float, 0.0, 1e6, 0.0),
        "budget_min": _num(raw, "budget_min", float, 0.1, 120.0, 20.0),
        "distance_factor": _num(raw, "distance_factor", float, 0.1, 1.5, 1.0),
        "official": off,
        "lat": _num(raw, "lat", float, -90, 90, 54.7757),
        "lon": _num(raw, "lon", float, -180, 180, 56.0366),
    }


def track_path(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ValueError("Недопустимое имя файла")
    return TRACKS_DIR / name


def run_job(state, path, params, rid=None):
    t_start = time.time()

    def on_progress(i, n, lat, lon, ele, t_off):
        state.set_status(i=i, n=n, lat=lat, lon=lon, ele=ele, t=t_off)

    ok = False
    try:
        ok = gp.play(str(path), state.emu_host, state.emu_port, None,
                     params["speed"], params["cadence"], params["step"], params["walk_kmh"],
                     False, params["human"], params["seed"], params["stop_prob"],
                     params["gps_noise"], cancel=state.cancel,
                     on_progress=on_progress, log=state.add_log)
        final = "done" if ok else ("stopped" if state.cancel.is_set() else "error")
    except SystemExit as e:           # load_token() завершает работу через SystemExit
        state.add_log(str(e))
        final = "error"
    except Exception as e:
        state.add_log(f"[!] Ошибка: {e}")
        final = "error"
    state.set_status(state=final)
    if rid:
        st, logs, _ = state.snapshot()
        try:
            (state.reports.root / rid / "log.txt").write_text("\n".join(logs), encoding="utf-8")
            state.reports.update(rid, status=final, summary={
                "file": path.name, "params": {k: v for k, v in params.items()},
                "points_played": st.get("i", 0), "points_total": st.get("n", 0),
                "wall_s": round(time.time() - t_start, 1)})
        except Exception:
            pass


def run_challenge_job(state, rid, rdir, prm):
    def log(line):
        with state.lock:
            state.challenge.setdefault("log", []).append(line)

    def progress(i, n):
        with state.lock:
            state.challenge.update(i=i, n=n, stage="generate")

    try:
        router = state.router if prm["source"] == "graphhopper" else None
        if router is not None and not router.health():
            raise routing.RouterError(
                f"GraphHopper недоступен ({router.url}). Запусти его или выбери «синтетика».")
        summary, rep, walks = run_challenge(
            rdir, prm["seed"], prm["n"], prm["total_km"] * 1000, prm["accel"] or None,
            prm["budget_min"] * 60, prm["distance_factor"], prm["official"], router,
            (prm["lat"], prm["lon"]), log=log, progress=progress)
        ok = summary["passed"] == summary["total"]
        status = "done" if ok else "checks_failed"
        state.reports.update(rid, status=status, summary={
            "walks": len(walks), "distance_km": round(sum(w.distance_m for w in walks) / 1000, 4),
            "passed": summary["passed"], "total": summary["total"],
            "wall_s": round(rep["wall_s"], 2), "virtual_h": round(rep["virtual_s"] / 3600, 2),
            "geometry_source": summary["geometry_source"], "params": prm})
        with state.lock:
            state.challenge.update(state="done", stage="done", report_id=rid, ok=ok)
    except Exception as e:
        state.reports.update(rid, status="error", error=str(e), summary={"params": prm})
        with state.lock:
            state.challenge.update(state="error", stage="error", report_id=rid, error=str(e))


def thin(points, limit=400):
    k = max(1, len(points) // limit)
    return [[round(p[0], 6), round(p[1], 6)] for p in points[::k]] + \
           [[round(points[-1][0], 6), round(points[-1][1], 6)]]


def emulator_action(state, action):
    script = ROOT / "setup_headless.sh"

    def work():
        state.add_log(f"[emu] {action}…")
        try:
            r = subprocess.run([str(script), action], capture_output=True, text=True, timeout=600)
            for line in (r.stdout + r.stderr).strip().splitlines()[-8:]:
                state.add_log(f"[emu] {line}")
            state.add_log(f"[emu] {action}: код {r.returncode}")
        except Exception as e:
            state.add_log(f"[emu] ошибка: {e}")
        finally:
            state.emu_busy = False

    if state.emu_busy:
        raise ValueError("Операция с эмулятором уже выполняется")
    state.emu_busy = True
    threading.Thread(target=work, daemon=True).start()


def make_handler(state, web_token):
    index_html = (ROOT / "web" / "index.html").read_bytes()

    class Handler(BaseHTTPRequestHandler):
        server_version = "PacerGPX"

        def log_message(self, fmt, *args):
            pass

        def _send(self, code, body, ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "text" in ctype or "json" in ctype else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'")
            self.end_headers()
            self.wfile.write(body)

        def _authed(self):
            tok = self.headers.get("X-Token", "")
            return hmac.compare_digest(tok.encode(), web_token.encode())

        def _body(self, limit):
            n = int(self.headers.get("Content-Length") or 0)
            if n > limit:
                raise ValueError("Слишком большой запрос")
            return self.rfile.read(n)

        def _json(self):
            data = self._body(MAX_JSON)
            return json.loads(data or b"{}")

        def _route(self, method):
            u = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(u.query).items()}
            if method == "GET" and u.path in ("/", "/index.html"):
                return self._send(200, index_html, "text/html")
            if not u.path.startswith("/api/"):
                return self._send(404, {"error": "not found"})
            if not self._authed():
                return self._send(401, {"error": "Неверный токен"})
            try:
                self._api(method, u.path, q)
            except (ValueError, json.JSONDecodeError) as e:
                self._send(400, {"error": str(e)})
            except Exception as e:
                self._send(500, {"error": f"Внутренняя ошибка: {e}"})

        def _api(self, method, path, q):
            if method == "GET" and path == "/api/status":
                st, logs, total = state.snapshot(int(q.get("log_from", 0)))
                return self._send(200, {"status": st, "log": logs, "log_total": total,
                                        "emulator": {"up": state.emulator_up(), "busy": state.emu_busy,
                                                     "port": state.emu_port}})
            if method == "GET" and path == "/api/tracks":
                files = sorted(p.name for p in TRACKS_DIR.glob("*.gpx"))
                return self._send(200, {"tracks": files, "defaults":
                                        {k: v[3] for k, v in PARAMS.items()}})
            if method == "POST" and path == "/api/upload":
                p = track_path(q.get("name", ""))
                data = self._body(MAX_UPLOAD)
                head = data.lower()
                if b"<!doctype" in head or b"<!entity" in head:
                    raise ValueError("DOCTYPE/ENTITY в GPX запрещены")
                tmp = p.with_suffix(".tmp")
                tmp.write_bytes(data)
                try:
                    pts = gp.parse_gpx(str(tmp))
                    if not pts:
                        raise ValueError("В файле нет точек trkpt")
                except Exception as e:
                    tmp.unlink(missing_ok=True)
                    raise ValueError(f"Некорректный GPX: {e}")
                tmp.replace(p)
                return self._send(200, {"ok": True, "name": p.name, "points": len(pts)})
            if method == "DELETE" and path == "/api/tracks":
                track_path(q.get("name", "")).unlink(missing_ok=True)
                return self._send(200, {"ok": True})
            if method == "POST" and path == "/api/preview":
                body = self._json()
                p = track_path(body.get("file"))
                prm = clean_params(body.get("params") or {})
                if not p.exists():
                    raise ValueError("Файл не найден")
                points, _, sm = gp.prepare(str(p), prm["step"], prm["walk_kmh"], prm["speed"],
                                           prm["human"], prm["seed"], prm["stop_prob"], prm["gps_noise"])
                return self._send(200, {"summary": sm, "route": thin(points)})
            if method == "POST" and path == "/api/start":
                body = self._json()
                p = track_path(body.get("file"))
                prm = clean_params(body.get("params") or {})
                if not p.exists():
                    raise ValueError("Файл не найден")
                with state.lock:
                    if state.job and state.job.is_alive():
                        raise ValueError("Проигрыш уже идёт")
                    state.cancel = threading.Event()
                    state.log.clear()
                    state.status = {"state": "running", "file": p.name, "i": 0, "n": 0,
                                    "started": time.time()}
                    rid, _ = state.reports.new("playback", f"Проигрыш {p.name}")
                    state.status["report_id"] = rid
                    state.job = threading.Thread(target=run_job, args=(state, p, prm, rid), daemon=True)
                    state.job.start()
                return self._send(200, {"ok": True})
            if method == "POST" and path == "/api/stop":
                state.cancel.set()
                return self._send(200, {"ok": True})
            if method == "GET" and path == "/api/gh/status":
                return self._send(200, {"up": state.router.health(), "url": state.router.url,
                                        "profile": state.router.profile})
            if method == "POST" and path == "/api/route/build":
                return self._send(200, self._build_route(self._json()))
            if method == "POST" and path == "/api/route/save":
                body = self._json()
                rid, name = body.get("id"), str(body.get("name", "")).strip()
                if not name.endswith(".gpx"):
                    name += ".gpx"
                p = track_path(name)
                coords = state.built.get(rid)
                if coords is None:
                    raise ValueError("Маршрут не найден: построй его заново")
                if p.exists():
                    raise ValueError(f"Файл {p.name} уже существует, выбери другое имя")
                p.write_text(routing.route_to_gpx(coords, p.stem), encoding="utf-8")
                return self._send(200, {"ok": True, "name": p.name})
            if method == "POST" and path == "/api/challenge/start":
                prm = clean_challenge(self._json())
                with state.lock:
                    if state.challenge_job and state.challenge_job.is_alive():
                        raise ValueError("Прогон челленджа уже идёт")
                    rid, rdir = state.reports.new("challenge", f"Челлендж {prm['total_km']:g} км, seed {prm['seed']}")
                    state.challenge = {"state": "running", "stage": "generate", "i": 0, "n": 0,
                                       "report_id": rid, "log": [], "started": time.time()}
                    state.challenge_job = threading.Thread(
                        target=run_challenge_job, args=(state, rid, rdir, prm), daemon=True)
                    state.challenge_job.start()
                return self._send(200, {"ok": True, "report_id": rid})
            if method == "GET" and path == "/api/challenge/status":
                with state.lock:
                    return self._send(200, dict(state.challenge))
            if method == "GET" and path == "/api/reports":
                return self._send(200, {"reports": state.reports.list()})
            if path.startswith("/api/reports/"):
                parts = path.split("/")[3:]
                rid = parts[0]
                if method == "GET" and len(parts) == 1:
                    return self._send(200, state.reports.get(rid))
                if method == "GET" and len(parts) == 2 and parts[1] == "zip":
                    return self._send(200, state.reports.zip_bytes(rid), "application/zip",
                                      {"Content-Disposition": f'attachment; filename="{rid}.zip"'})
                if method == "DELETE" and len(parts) == 1:
                    state.reports.delete(rid)
                    return self._send(200, {"ok": True})
            if method == "POST" and path == "/api/emulator":
                action = self._json().get("action")
                if action not in ("start", "stop"):
                    raise ValueError("action: start|stop")
                emulator_action(state, action)
                return self._send(200, {"ok": True})
            self._send(404, {"error": "not found"})


        def _build_route(self, body):
            mode = body.get("mode", "loop")
            lat = _num(body, "lat", float, -90, 90, required=True)
            lon = _num(body, "lon", float, -180, 180, required=True)
            if mode == "loop":
                length_km = _num(body, "length_km", float, 0.3, 30.0, required=True)
                seed = _num(body, "seed", int, 0, 2**31 - 1, 1)
                if body.get("exact"):
                    r = state.router.route_at_least(lat, lon, length_km * 1000, seed)
                    coords = routing.trim_polyline(r["coords"], length_km * 1000)
                else:
                    r = state.router.round_trip(lat, lon, length_km * 1000, seed)
                    coords = r["coords"]
            elif mode == "points":
                end_lat = _num(body, "end_lat", float, -90, 90, required=True)
                end_lon = _num(body, "end_lon", float, -180, 180, required=True)
                r = state.router.route([(lat, lon), (end_lat, end_lon)])
                coords = r["coords"]
            else:
                raise ValueError("mode: loop|points")
            rid = secrets.token_hex(6)
            state.built[rid] = coords
            while len(state.built) > 20:
                state.built.popitem(last=False)
            stats = routing.elevation_stats(coords)
            k = max(1, len(coords) // 500)
            thin_c = coords[::k] + ([coords[-1]] if (len(coords) - 1) % k else [])
            gap = routing.haversine_m(coords[0][0], coords[0][1], coords[-1][0], coords[-1][1])
            return {"id": rid,
                    "route": [[round(c[0], 6), round(c[1], 6), None if c[2] is None else round(c[2], 1)]
                              for c in thin_c],
                    "summary": {"distance_m": routing.polyline_length(coords),
                                "gh_distance_m": r["distance_m"], "gh_time_s": r["time_s"],
                                "points": len(coords), "start_end_gap_m": gap, **stats}}

        def do_GET(self):
            self._route("GET")

        def do_POST(self):
            self._route("POST")

        def do_DELETE(self):
            self._route("DELETE")

    return Handler


def load_web_token():
    env = os.environ.get("PACER_WEB_TOKEN")
    if env:
        return env.strip()
    f = DATA_DIR / "web_token"
    if f.exists():
        return f.read_text().strip()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tok = secrets.token_urlsafe(24)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok)
    print(f"[+] Создан токен доступа: {f}")
    return tok


def main(argv=None):
    ap = argparse.ArgumentParser(description="Веб-интерфейс Pacer GPX Player")
    ap.add_argument("--bind", default="127.0.0.1", help="адрес (лучше IP wg-интерфейса, напр. 10.8.0.1)")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--emu-host", default="localhost")
    ap.add_argument("--emu-port", type=int, default=5554)
    ap.add_argument("--gh-url", default=None, help="адрес GraphHopper (по умолчанию GRAPHHOPPER_URL или http://127.0.0.1:8989)")
    a = ap.parse_args(argv)
    if a.bind in ("0.0.0.0", "::"):
        print("[!] ВНИМАНИЕ: --bind 0.0.0.0 открывает интерфейс на всех сетях. "
              "Используй адрес WireGuard-интерфейса.")
    TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    token = load_web_token()
    state = State(a.emu_host, a.emu_port, a.gh_url)
    srv = ThreadingHTTPServer((a.bind, a.port), make_handler(state, token))
    print(f"[+] http://{a.bind}:{a.port}  (токен: {DATA_DIR / 'web_token'} или PACER_WEB_TOKEN)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.cancel.set()
        srv.server_close()


if __name__ == "__main__":
    main()
