#!/usr/bin/env bash
# Установка Pacer GPX Player как systemd-сервиса на Ubuntu 22.04/24.04.
# Использование (от root):
#   sudo ./deploy/install_ubuntu.sh [BIND_IP] [PORT]
#   BIND_IP — адрес wg-интерфейса сервера (по умолчанию 10.8.0.1), PORT — 8080.
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запусти через sudo"; exit 1; }

BIND_IP="${1:-10.8.0.1}"
PORT="${2:-8080}"
APP_DIR=/opt/pacer-gpx
APP_USER=pacer
HOME_DIR=/var/lib/pacer
SRC="$(cd "$(dirname "$0")/.." && pwd)"

apt-get update
apt-get install -y python3 openjdk-17-jre-headless curl unzip qemu-kvm

id "$APP_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir "$HOME_DIR" --shell /usr/sbin/nologin "$APP_USER"
usermod -aG kvm "$APP_USER"          # аппаратное ускорение эмулятора

install -d -o root -g root "$APP_DIR" "$APP_DIR/web"
install -m 0644 "$SRC"/gpx_player.py "$SRC"/humanize.py "$SRC"/web_server.py "$SRC"/routing.py "$SRC"/reports.py "$APP_DIR/"
install -d "$APP_DIR/challenge"
install -m 0644 "$SRC"/challenge/*.py "$APP_DIR/challenge/"
install -m 0644 "$SRC"/web/index.html "$APP_DIR/web/"
install -m 0755 "$SRC"/setup_headless.sh "$APP_DIR/"

sed -e "s|@BIND_IP@|$BIND_IP|g" -e "s|@PORT@|$PORT|g" \
  "$SRC/deploy/pacer-gpx.service" > /etc/systemd/system/pacer-gpx.service
systemctl daemon-reload
systemctl enable --now pacer-gpx

# Устанавливаем SDK и AVD от имени сервисного пользователя (скачивание ~3-5 ГБ).
if [ ! -d "$HOME_DIR/android-sdk/emulator" ]; then
  sudo -u "$APP_USER" -H env HOME="$HOME_DIR" "$APP_DIR/setup_headless.sh" install
fi

sleep 1
echo
echo "[+] Готово: http://$BIND_IP:$PORT"
echo "[+] Токен доступа: sudo cat $HOME_DIR/.pacer-gpx/web_token"
echo "[+] GraphHopper (вкладка «Создание маршрута»): sudo ./deploy/graphhopper.sh"
echo "[+] Закрой порт для всех, кроме wg-интерфейса, например:"
echo "      ufw allow in on wg0 to any port $PORT proto tcp"
