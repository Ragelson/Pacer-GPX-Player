import argparse
import math
import os
import random
import socket
import sys
import threading
import time
import xml.etree.ElementTree as ET
from bisect import bisect_right
from datetime import datetime, timedelta, timezone

# === НАСТРОЙКИ (значения по умолчанию; всё можно переопределить через CLI) ===
EMULATOR_HOST = "localhost"
EMULATOR_PORT = 5554
AUTH_TOKEN = None               # None -> читается из ~/.emulator_console_auth_token

WALK_SPEED_KMH = 4.5            # если в GPX нет <time>, идём с такой скоростью
SPEED_MULTIPLIER = 1.0
SENSOR_INTERVAL = 0.05          # 20 Гц для акселерометра
CADENCE_HZ = 1.9                # ~114 шагов/мин
INTERP_STEP_SEC = 2.0           # шаг интерполяции GPX-точек


# ---------- Утилиты ----------

def elevation_to_pressure_hpa(h_meters):
    """Стандартная атмосфера: высота в метрах → давление в hPa.
    Для высот 0–11 км. На 200 м над уровнем моря даёт ~989 hPa."""
    return 1013.25 * (1 - 0.0065 * h_meters / 288.15) ** 5.255


def load_token(explicit=None):
    """Токен консоли: аргумент → переменная EMULATOR_AUTH_TOKEN → файл в домашней папке."""
    if explicit:
        return explicit
    env = os.environ.get("EMULATOR_AUTH_TOKEN")
    if env:
        return env.strip()
    path = os.path.join(os.path.expanduser("~"), ".emulator_console_auth_token")
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        raise SystemExit(
            f"[!] Токен не найден. Запусти эмулятор (он создаст {path}) "
            "или передай --token / EMULATOR_AUTH_TOKEN.")


from humanize import humanize  # noqa: E402


# ---------- Подключение к эмулятору ----------

class EmulatorConsole:
    def __init__(self, host, port, token):
        self._lock = threading.Lock()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.settimeout(10)
        self._read_until_ok()
        self._send(f"auth {token}")
        self._read_until_ok()

    def _send(self, line):
        self.sock.sendall((line + "\n").encode())

    def _read_until_ok(self):
        buf = b""
        while True:
            last = buf.rstrip().splitlines()[-1:] or [b""]
            if last[0].strip() in (b"OK", b"KO") or last[0].startswith(b"KO:"):
                break
            chunk = self.sock.recv(4096)
            if not chunk:
                break
            buf += chunk
        return buf.decode(errors="ignore")

    def command(self, line):
        with self._lock:
            self._send(line)
            return self._read_until_ok()

    def close(self):
        try:
            self._send("quit")
        except Exception:
            pass
        self.sock.close()


# ---------- Парсинг и интерполяция ----------

def _local(tag):
    return tag.rsplit("}", 1)[-1]


def parse_time(text):
    """ISO-8601 с/без миллисекунд, 'Z' или смещением."""
    text = text.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"Не удалось разобрать время: {text!r}")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def haversine_m(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def parse_gpx(path, walk_speed_kmh=WALK_SPEED_KMH):
    """Читает trkpt (GPX 1.0/1.1, с namespace или без).
    Если у точек нет <time>, время строится из длины пути и walk_speed_kmh."""
    tree = ET.parse(path)
    raw = []
    for el in tree.iter():
        if _local(el.tag) != "trkpt":
            continue
        lat, lon = float(el.get("lat")), float(el.get("lon"))
        ele, ts = 0.0, None
        for child in el:
            name = _local(child.tag)
            if name == "ele" and child.text:
                ele = float(child.text)
            elif name == "time" and child.text:
                ts = parse_time(child.text)
        raw.append((lat, lon, ele, ts))

    if raw and any(p[3] is None for p in raw):
        print(f"[!] В GPX нет времени у части точек — считаю по скорости {walk_speed_kmh} км/ч.")
        speed = walk_speed_kmh / 3.6
        t = datetime.now(timezone.utc)
        points = [(raw[0][0], raw[0][1], raw[0][2], t)]
        for prev, cur in zip(raw, raw[1:]):
            t += timedelta(seconds=haversine_m(prev[0], prev[1], cur[0], cur[1]) / speed)
            points.append((cur[0], cur[1], cur[2], t))
        return points
    return raw


def interpolate(points, step_sec):
    if not points:
        return []
    t0 = points[0][3]
    result = []
    for i in range(len(points) - 1):
        lat1, lon1, ele1, t1 = points[i]
        lat2, lon2, ele2, t2 = points[i + 1]
        dt = (t2 - t1).total_seconds()
        n_steps = max(1, int(dt / step_sec))
        for k in range(n_steps):
            frac = k / n_steps
            lat = lat1 + (lat2 - lat1) * frac
            lon = lon1 + (lon2 - lon1) * frac
            ele = ele1 + (ele2 - ele1) * frac
            t_offset = (t1 - t0).total_seconds() + dt * frac
            result.append((lat, lon, ele, t_offset))
    last = points[-1]
    result.append((last[0], last[1], last[2], (last[3] - t0).total_seconds()))
    return result


# ---------- Акселерометр+гироскоп: ритмичная ходьба ----------

def sensor_loop(console, stop_event, cadence_hz=CADENCE_HZ, moving=None):
    start = time.monotonic()
    while not stop_event.is_set():
        t = time.monotonic() - start    # фаза по реальному времени, не дрейфует
        if moving is not None and not moving.is_set():
            # стоим на месте: только гравитация и лёгкий шум, без шагов
            try:
                console.command(f"sensor set acceleration {random.uniform(-0.05, 0.05):.3f}:"
                                f"{random.uniform(-0.05, 0.05):.3f}:{9.8 + random.uniform(-0.05, 0.05):.3f}")
                console.command(f"sensor set gyroscope {random.uniform(-0.02, 0.02):.3f}:"
                                f"{random.uniform(-0.02, 0.02):.3f}:{random.uniform(-0.02, 0.02):.3f}")
            except Exception as e:
                print(f"[!] Сенсоры: {e}")
                break
            stop_event.wait(SENSOR_INTERVAL)
            continue
        phase = 2 * math.pi * cadence_hz * t
        az = 9.8 + 3.0 * math.sin(phase) + random.uniform(-0.3, 0.3)
        ax = 1.5 * math.sin(phase / 2) + random.uniform(-0.2, 0.2)
        ay = 1.0 * math.cos(phase) + random.uniform(-0.2, 0.2)
        gx = 0.3 * math.sin(phase) + random.uniform(-0.1, 0.1)
        gy = 0.3 * math.cos(phase) + random.uniform(-0.1, 0.1)
        gz = random.uniform(-0.1, 0.1)
        try:
            console.command(f"sensor set acceleration {ax:.3f}:{ay:.3f}:{az:.3f}")
            console.command(f"sensor set gyroscope {gx:.3f}:{gy:.3f}:{gz:.3f}")
        except Exception as e:
            print(f"[!] Сенсоры: {e}")
            break
        stop_event.wait(SENSOR_INTERVAL)


# ---------- Основной цикл ----------

def prepare(gpx_path, step=INTERP_STEP_SEC, walk_kmh=WALK_SPEED_KMH, speed=SPEED_MULTIPLIER,
            human=False, seed=None, stop_prob=0.5, gps_noise=2.5):
    """Разбирает GPX и строит точки проигрыша. Возвращает (points, stops, summary)."""
    raw_points = parse_gpx(gpx_path, walk_kmh)
    if not raw_points:
        raise ValueError("В файле нет точек.")
    stops = []
    if human:
        points, stops = humanize(raw_points, seed=seed, mean_kmh=walk_kmh,
                                 stop_prob=stop_prob, gps_noise_m=gps_noise, step=step)
    else:
        points = interpolate(raw_points, step)
    total = points[-1][3]
    ele = [p[2] for p in raw_points]
    dist = sum(haversine_m(p[0], p[1], q[0], q[1]) for p, q in zip(raw_points, raw_points[1:]))
    summary = {
        "raw_points": len(raw_points), "points": len(points),
        "ele_min": min(ele), "ele_max": max(ele),
        "duration_s": total, "play_s": total / speed,
        "distance_m": dist, "avg_kmh": (dist / total * 3.6) if total else 0.0,
        "stops": len(stops), "stops_wait_s": sum(b - a for a, b in stops),
    }
    return points, stops, summary


def format_summary(sm):
    return [
        f"[+] Исходно в GPX: {sm['raw_points']} точек, после обработки: {sm['points']}.",
        f"[+] Высоты: {sm['ele_min']:.1f} – {sm['ele_max']:.1f} м, "
        f"перепад {sm['ele_max'] - sm['ele_min']:.1f} м.",
        f"[+] Длительность: {sm['duration_s']/60:.1f} мин (проигрыш ~{sm['play_s']/60:.1f} мин)",
        f"[+] Дистанция: {sm['distance_m']/1000:.2f} км, ~{sm['avg_kmh']:.1f} км/ч.",
        f"[+] Остановок: {sm['stops']}, ожидание суммарно {sm['stops_wait_s']:.0f} с.",
    ]


def play(gpx_path, host=EMULATOR_HOST, port=EMULATOR_PORT, token=None,
         speed=SPEED_MULTIPLIER, cadence=CADENCE_HZ, step=INTERP_STEP_SEC,
         walk_kmh=WALK_SPEED_KMH, dry_run=False, human=False, seed=None,
         stop_prob=0.5, gps_noise=2.5, cancel=None, on_progress=None, log=print):
    """cancel — threading.Event для досрочной остановки;
    on_progress(i, n, lat, lon, ele, t_offset) вызывается на каждой точке."""
    try:
        points, stops, sm = prepare(gpx_path, step, walk_kmh, speed, human, seed,
                                    stop_prob, gps_noise)
    except ValueError as e:
        log(f"[!] {e}")
        return False
    for line in format_summary(sm):
        log(line)
    if dry_run:
        log("[+] dry-run: эмулятор не используется, ничего не отправлено.")
        return True

    token = load_token(token)
    geo = EmulatorConsole(host, port, token)
    sens = EmulatorConsole(host, port, token)
    log("[+] Подключено к эмулятору.")

    pressure_supported = True
    test_resp = sens.command("sensor set pressure 1013.25")
    if "KO" in test_resp or "not available" in test_resp.lower():
        pressure_supported = False
        log("[!] Pressure-сенсор недоступен (проверь hw.sensors.pressure=yes в AVD).")
    else:
        log("[+] Pressure-сенсор доступен, барометр будет симулироваться.")

    stop_event = threading.Event()
    moving = threading.Event()
    moving.set()
    stop_starts = [a for a, _ in stops]
    t = threading.Thread(target=sensor_loop, args=(sens, stop_event, cadence, moving), daemon=True)
    t.start()

    completed = False
    try:
        start_real = time.time()
        for i, (lat, lon, ele, t_offset) in enumerate(points):
            if cancel is not None and cancel.is_set():
                log("[!] Остановлено пользователем.")
                break
            k = bisect_right(stop_starts, t_offset) - 1
            if k >= 0 and stops[k][0] < t_offset <= stops[k][1]:
                moving.clear()
            else:
                moving.set()
            geo.command(f"geo fix {lon} {lat} {ele}")
            if pressure_supported:
                geo.command(f"sensor set pressure {elevation_to_pressure_hpa(ele):.2f}")
            if on_progress:
                on_progress(i + 1, len(points), lat, lon, ele, t_offset)
            if i % 20 == 0 or i == len(points) - 1:
                log(f"[{i+1}/{len(points)}] {lat:.6f}, {lon:.6f}  "
                    f"ele={ele:.1f}м  t+{t_offset:.0f}s")
            if i < len(points) - 1:
                delta = start_real + points[i + 1][3] / speed - time.time()
                if delta > 0:
                    # ждём с возможностью прервать
                    if cancel is not None:
                        cancel.wait(delta)
                    else:
                        time.sleep(delta)
        else:
            completed = True
            log("[+] Трек проигран.")
    finally:
        stop_event.set()
        t.join(timeout=2)
        geo.close()
        sens.close()
        log("[+] Соединения закрыты.")
    return completed


def main(argv=None):
    p = argparse.ArgumentParser(description="Проигрывает GPX-трек в Android-эмулятор.")
    p.add_argument("gpx", help="путь к GPX-файлу")
    p.add_argument("--host", default=EMULATOR_HOST)
    p.add_argument("--port", type=int, default=EMULATOR_PORT)
    p.add_argument("--token", help="токен консоли (по умолчанию из ~/.emulator_console_auth_token)")
    p.add_argument("--speed", type=float, default=SPEED_MULTIPLIER, help="множитель скорости")
    p.add_argument("--cadence", type=float, default=CADENCE_HZ, help="частота шагов, Гц")
    p.add_argument("--step", type=float, default=INTERP_STEP_SEC, help="шаг интерполяции, сек")
    p.add_argument("--walk-kmh", type=float, default=WALK_SPEED_KMH,
                   help="скорость, если в GPX нет времени")
    p.add_argument("--humanize", action="store_true",
                   help="заново пройти маршрут по геометрии: остановки, шум скорости и GPS")
    p.add_argument("--seed", type=int, help="seed для воспроизводимости --humanize")
    p.add_argument("--stop-prob", type=float, default=0.5,
                   help="вероятность остановки на повороте/перекрёстке (0..1)")
    p.add_argument("--gps-noise", type=float, default=2.5, help="шум GPS, м")
    p.add_argument("--dry-run", action="store_true", help="только разобрать трек, без эмулятора")
    a = p.parse_args(argv)
    play(a.gpx, a.host, a.port, a.token, a.speed, a.cadence, a.step, a.walk_kmh, a.dry_run,
         a.humanize, a.seed, a.stop_prob, a.gps_noise)


if __name__ == "__main__":
    main()
