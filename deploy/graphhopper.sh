#!/usr/bin/env bash
# Установка GraphHopper (профиль foot) как сервиса на Ubuntu. От root:
#   sudo ./deploy/graphhopper.sh
# Переменные (необязательно):
#   OSM_URL  — OSM-экстракт .osm.pbf (по умолчанию Приволжский ФО с Geofabrik, ~1 ГБ)
#   BBOX     — вырезать область "minlon,minlat,maxlon,maxlat" (по умолчанию окрестности Уфы),
#              иначе импорт всего экстракта потребует несколько ГБ памяти
#   GH_XMX   — память JVM (по умолчанию 1g; для большого региона 4g и больше)
#   GH_VER   — версия GraphHopper (по умолчанию 11.0)
set -euo pipefail
[ "$(id -u)" -eq 0 ] || { echo "Запусти через sudo"; exit 1; }

GH_VER="${GH_VER:-11.0}"
GH_XMX="${GH_XMX:-1g}"
OSM_URL="${OSM_URL:-https://download.geofabrik.de/russia/volga-fed-district-latest.osm.pbf}"
BBOX="${BBOX:-55.6,54.4,56.5,55.1}"
DIR=/opt/graphhopper
SRC="$(cd "$(dirname "$0")" && pwd)"

apt-get update
apt-get install -y openjdk-17-jre-headless osmium-tool curl
id pacer >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/pacer --shell /usr/sbin/nologin pacer

install -d "$DIR"
[ -f "$DIR/graphhopper-web-$GH_VER.jar" ] || curl -fL -o "$DIR/graphhopper-web-$GH_VER.jar" \
  "https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/$GH_VER/graphhopper-web-$GH_VER.jar"

if [ ! -f "$DIR/region.osm.pbf" ]; then
  tmp="$(mktemp --suffix=.osm.pbf)"
  curl -fL -o "$tmp" "$OSM_URL"
  if [ -n "$BBOX" ]; then
    osmium extract --bbox "$BBOX" "$tmp" -o "$DIR/region.osm.pbf" --overwrite
    rm -f "$tmp"
  else
    mv "$tmp" "$DIR/region.osm.pbf"
  fi
fi

install -m 0644 "$SRC/graphhopper-config.yml" "$DIR/config.yml"
sed -e "s|@XMX@|$GH_XMX|g" -e "s|@VER@|$GH_VER|g" "$SRC/graphhopper.service" > /etc/systemd/system/graphhopper.service
systemctl daemon-reload
systemctl enable --now graphhopper

echo "[+] Первый запуск строит граф (минуты, зависит от размера региона): journalctl -u graphhopper -f"
echo "[+] Проверка: curl http://127.0.0.1:8989/health  (должно быть OK)"
echo "[+] Пример: curl 'http://127.0.0.1:8989/route?profile=foot&point=54.7757,56.0366&point=54.78,56.04&points_encoded=false'"
