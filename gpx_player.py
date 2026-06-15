import socket
import time
import math
import random
import threading
import sys
import xml.etree.ElementTree as ET
from datetime import datetime

# === НАСТРОЙКИ ===
EMULATOR_HOST = "localhost"
EMULATOR_PORT = 5554
AUTH_TOKEN = "подставь сюда токен"

SPEED_MULTIPLIER = 1.0
SENSOR_INTERVAL = 0.05          # 20 Гц для акселерометра
CADENCE_HZ = 1.9                # ~114 шагов/мин
INTERP_STEP_SEC = 2.0           # шаг интерполяции GPX-точек


# ---------- Утилиты ----------

def elevation_to_pressure_hpa(h_meters):
    """Стандартная атмосфера: высота в метрах → давление в hPa.
    Для высот 0–11 км. На 200 м над уровнем моря даёт ~989 hPa."""
    return 1013.25 * (1 - 0.0065 * h_meters / 288.15) ** 5.255


# ---------- Подключение к эмулятору ----------

class EmulatorConsole:
    def __init__(self, host, port, token):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((host, port))
        self.sock.settimeout(10)
        self._read_until_ok()
        self._send(f"auth {token}")
        self._read_until_ok()
        self._lock = threading.Lock()

    def _send(self, line):
        self.sock.sendall((line + "\n").encode())

    def _read_until_ok(self):
        buf = b""
        while b"OK" not in buf and b"KO" not in buf:
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

def parse_gpx(path):
    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    tree = ET.parse(path)
    points = []
    for trkpt in tree.iter("{http://www.topografix.com/GPX/1/1}trkpt"):
        lat = float(trkpt.get("lat"))
        lon = float(trkpt.get("lon"))
        time_el = trkpt.find("gpx:time", ns)
        ele_el = trkpt.find("gpx:ele", ns)
        ele = float(ele_el.text) if ele_el is not None else 0.0
        ts = time_el.text
        dt = datetime.strptime(ts.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S.%f%z")
        points.append((lat, lon, ele, dt))
    return points


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

def sensor_loop(console, stop_event):
    t = 0.0
    while not stop_event.is_set():
        phase = 2 * math.pi * CADENCE_HZ * t
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
        t += SENSOR_INTERVAL
        stop_event.wait(SENSOR_INTERVAL)


# ---------- Основной цикл ----------

def play(gpx_path):
    raw_points = parse_gpx(gpx_path)
    if not raw_points:
        print("[!] В файле нет точек.")
        return

    points = interpolate(raw_points, INTERP_STEP_SEC)
    total = points[-1][3]
    elevations = [p[2] for p in raw_points]
    print(f"[+] Исходно в GPX: {len(raw_points)} точек.")
    print(f"[+] После интерполяции: {len(points)} точек.")
    print(f"[+] Высоты: {min(elevations):.1f} – {max(elevations):.1f} м, "
          f"перепад {max(elevations) - min(elevations):.1f} м.")
    print(f"[+] Длительность: {total/60:.1f} мин "
          f"(проигрыш ~{total/SPEED_MULTIPLIER/60:.1f} мин)")

    geo = EmulatorConsole(EMULATOR_HOST, EMULATOR_PORT, AUTH_TOKEN)
    sens = EmulatorConsole(EMULATOR_HOST, EMULATOR_PORT, AUTH_TOKEN)
    print("[+] Подключено к эмулятору.")

    # Проверим, поддерживает ли эмулятор pressure-сенсор
    pressure_supported = True
    test_resp = sens.command("sensor set pressure 1013.25")
    if "KO" in test_resp or "not available" in test_resp.lower():
        pressure_supported = False
        print("[!] Pressure-сенсор недоступен в этом эмуляторе.")
        print("[!] Высоты в Pacer не появятся. Проверь AVD config "
              "(hw.sensors.pressure=yes).")
    else:
        print("[+] Pressure-сенсор доступен, барометр будет симулироваться.")

    stop_event = threading.Event()
    t = threading.Thread(target=sensor_loop, args=(sens, stop_event), daemon=True)
    t.start()

    try:
        start_real = time.time()
        for i, (lat, lon, ele, t_offset) in enumerate(points):
            geo.command(f"geo fix {lon} {lat} {ele}")
            if pressure_supported:
                pressure = elevation_to_pressure_hpa(ele)
                geo.command(f"sensor set pressure {pressure:.2f}")

            if i % 20 == 0 or i == len(points) - 1:
                print(f"[{i+1}/{len(points)}] {lat:.6f}, {lon:.6f}  "
                      f"ele={ele:.1f}м  t+{t_offset:.0f}s")

            if i < len(points) - 1:
                target_real = start_real + points[i + 1][3] / SPEED_MULTIPLIER
                delta = target_real - time.time()
                if delta > 0:
                    time.sleep(delta)
        print("[+] Трек проигран.")
    finally:
        stop_event.set()
        t.join(timeout=2)
        geo.close()
        sens.close()
        print("[+] Соединения закрыты.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Использование: python gpx_player.py путь/к/файлу.gpx")
        sys.exit(1)
    play(sys.argv[1])
