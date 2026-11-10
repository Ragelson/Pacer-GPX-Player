#!/usr/bin/env bash
# Эмулятор Android без Android Studio: только консольные инструменты SDK.
# Использование:
#   ./setup_headless.sh install     # один раз: скачать SDK, образ, создать AVD
#   ./setup_headless.sh start       # запустить эмулятор без окна
#   ./setup_headless.sh stop
# Нужны: Java 17+, curl, unzip. Для скорости — KVM (Linux) / HAXM / Hypervisor.framework.
set -euo pipefail

SDK="${ANDROID_SDK_ROOT:-$HOME/android-sdk}"
AVD_NAME="${AVD_NAME:-pacer}"
API="${API_LEVEL:-34}"
ABI="${ABI:-x86_64}"                      # на Apple Silicon: ABI=arm64-v8a
IMAGE="system-images;android-${API};google_apis_playstore;${ABI}"
# Версию cmdline-tools при необходимости обнови на developer.android.com/studio#command-tools
CMDLINE_URL="${CMDLINE_URL:-https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip}"
export ANDROID_SDK_ROOT="$SDK"
export PATH="$SDK/cmdline-tools/latest/bin:$SDK/platform-tools:$SDK/emulator:$PATH"

install() {
  mkdir -p "$SDK/cmdline-tools"
  if [ ! -x "$SDK/cmdline-tools/latest/bin/sdkmanager" ]; then
    tmp="$(mktemp -d)"
    curl -fL "$CMDLINE_URL" -o "$tmp/cmd.zip"
    unzip -q "$tmp/cmd.zip" -d "$tmp"
    mv "$tmp/cmdline-tools" "$SDK/cmdline-tools/latest"
    rm -rf "$tmp"
  fi
  yes | sdkmanager --licenses >/dev/null || true
  sdkmanager "platform-tools" "emulator" "platforms;android-${API}" "$IMAGE"
  echo no | avdmanager create avd -n "$AVD_NAME" -k "$IMAGE" -d pixel_8 --force

  # Включаем барометр (иначе не будет рельефа) и Google Play.
  cfg="$HOME/.android/avd/${AVD_NAME}.avd/config.ini"
  grep -q '^hw.sensors.pressure' "$cfg" \
    && sed -i.bak 's/^hw.sensors.pressure.*/hw.sensors.pressure=yes/' "$cfg" \
    || echo 'hw.sensors.pressure=yes' >> "$cfg"
  echo "[+] Готово. Запуск: ./setup_headless.sh start"
}

start() {
  nohup emulator -avd "$AVD_NAME" -no-window -no-audio -no-boot-anim \
    -gpu swiftshader_indirect -no-snapshot-save >"$SDK/emulator.log" 2>&1 &
  echo "[+] Эмулятор стартует (лог: $SDK/emulator.log), жду загрузки..."
  adb wait-for-device
  until [ "$(adb shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ]; do sleep 3; done
  echo "[+] Загружен. Токен: ~/.emulator_console_auth_token, порт 5554."
  echo "    APK ставится так: adb install app.apk"
}

stop() { adb emu kill || true; }

case "${1:-}" in
  install) install ;;
  start)   start ;;
  stop)    stop ;;
  *) echo "Использование: $0 {install|start|stop}"; exit 1 ;;
esac
