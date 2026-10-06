#!/usr/bin/env bash
# Автоматическая установка и запуск сервиса «Переработка» (Linux / macOS).
#   ./install.sh                  установить, включить автозапуск и запустить
#   ./install.sh --no-autostart   только установить зависимости (без автозапуска и запуска)
#   PERER_PORT=9000 ./install.sh  использовать другой порт (по умолчанию 8080)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
PORT="${PERER_PORT:-8080}"
HOST="${PERER_HOST:-127.0.0.1}"
AUTOSTART=1
[ "${1:-}" = "--no-autostart" ] && AUTOSTART=0

say() { printf '\n==> %s\n' "$*"; }
die() { printf '\nОШИБКА: %s\n' "$*" >&2; exit 1; }

# ---------- 1. Python 3.8+ ----------
find_python() {
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
      echo "$c"; return 0
    fi
  done
  return 1
}

say "Проверка Python"
PY="$(find_python || true)"
if [ -z "$PY" ]; then
  say "Python 3 не найден — пробую установить автоматически"
  SUDO=""; [ "$(id -u)" -ne 0 ] && SUDO="sudo"
  if   command -v apt-get >/dev/null 2>&1; then $SUDO apt-get update && $SUDO apt-get install -y python3 python3-venv python3-pip
  elif command -v dnf     >/dev/null 2>&1; then $SUDO dnf install -y python3 python3-pip
  elif command -v pacman  >/dev/null 2>&1; then $SUDO pacman -S --noconfirm python python-pip
  elif command -v brew    >/dev/null 2>&1; then brew install python
  else die "Не удалось определить менеджер пакетов. Установите Python 3.8+ вручную: https://www.python.org/downloads/"
  fi
  PY="$(find_python || true)"
  [ -n "$PY" ] || die "Python не установился. Установите Python 3.8+ вручную и запустите скрипт снова."
fi
echo "Используется: $($PY --version) ($(command -v "$PY"))"

# ---------- 2. Виртуальное окружение и зависимости (офлайн) ----------
# Все библиотеки лежат в vendor/wheels — интернет не нужен.
WHEELS="$ROOT/vendor/wheels"
[ -d "$WHEELS" ] || die "Не найдена папка $WHEELS с библиотеками. Скопируйте проект целиком."
PIP_WHL="$(ls "$WHEELS"/pip-*.whl 2>/dev/null | head -n1 || true)"

say "Создание виртуального окружения (venv)"
if [ ! -x "venv/bin/python" ]; then
  if ! "$PY" -m venv venv >/dev/null 2>&1; then
    # Debian/Ubuntu без пакета python3-venv: нет ensurepip. Создаём venv без pip,
    # а pip берём из vendor/wheels.
    rm -rf venv
    "$PY" -m venv --without-pip venv || die "Не удалось создать venv. В Debian/Ubuntu установите пакет python3-venv и повторите."
  fi
fi
say "Установка зависимостей из vendor/wheels (без интернета)"
PIP_OPTS=(--disable-pip-version-check -q --no-index --find-links "$WHEELS")
if venv/bin/python -m pip --version >/dev/null 2>&1; then
  venv/bin/python -m pip install "${PIP_OPTS[@]}" -r requirements.txt
else
  [ -n "$PIP_WHL" ] || die "В $WHEELS нет pip-*.whl"
  venv/bin/python "$PIP_WHL/pip" install "${PIP_OPTS[@]}" -r requirements.txt
fi
mkdir -p data

# ---------- 3. Автозапуск ----------
OS="$(uname -s)"
if [ "$AUTOSTART" -eq 0 ]; then
  say "Установка завершена (автозапуск пропущен). Запуск вручную: ./start.sh"
  exit 0
fi

say "Настройка автозапуска"
if [ "$OS" = "Darwin" ]; then
  PLIST="$HOME/Library/LaunchAgents/ru.pererabotki.plist"
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>ru.pererabotki</string>
  <key>ProgramArguments</key><array><string>$ROOT/venv/bin/python</string><string>$ROOT/app/server.py</string></array>
  <key>EnvironmentVariables</key><dict>
    <key>PERER_PORT</key><string>$PORT</string><key>PERER_HOST</key><string>$HOST</string></dict>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>$ROOT/data/launchd.err</string>
</dict></plist>
EOF
  launchctl unload "$PLIST" >/dev/null 2>&1 || true
  launchctl load "$PLIST"
  echo "Автозапуск: launchd ($PLIST)"
elif command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
  UNIT_DIR="$HOME/.config/systemd/user"
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT_DIR/pererabotki.service" <<EOF
[Unit]
Description=Pererabotki (учет часов переработки)
After=network.target

[Service]
WorkingDirectory=$ROOT
Environment=PERER_PORT=$PORT
Environment=PERER_HOST=$HOST
ExecStart=$ROOT/venv/bin/python $ROOT/app/server.py
Restart=on-failure

[Install]
WantedBy=default.target
EOF
  systemctl --user daemon-reload
  systemctl --user enable pererabotki.service
  systemctl --user restart pererabotki.service   # restart, чтобы при обновлении подхватился новый код
  loginctl enable-linger "$USER" >/dev/null 2>&1 || echo "(Чтобы сервис стартовал и без входа в систему, выполните: sudo loginctl enable-linger $USER)"
  echo "Автозапуск: systemd --user (pererabotki.service)"
else
  # Запасной вариант без systemd: cron @reboot (если cron есть), иначе только запуск сейчас
  if command -v crontab >/dev/null 2>&1; then
    CRON_LINE="@reboot cd $ROOT && PERER_PORT=$PORT PERER_HOST=$HOST nohup $ROOT/venv/bin/python $ROOT/app/server.py >/dev/null 2>&1 # pererabotki"
    ( crontab -l 2>/dev/null | grep -v '# pererabotki$' || true; echo "$CRON_LINE" ) | crontab -
    echo "Автозапуск: cron @reboot"
  else
    echo "ВНИМАНИЕ: не найдены ни systemd, ни launchd, ни cron — автозапуск не настроен."
    echo "Сервис запущен сейчас; после перезагрузки запускайте ./start.sh вручную."
  fi
  nohup env PERER_PORT="$PORT" PERER_HOST="$HOST" "$ROOT/venv/bin/python" "$ROOT/app/server.py" >/dev/null 2>&1 &
fi

# ---------- 4. Проверка ----------
say "Проверка запуска"
URL="http://127.0.0.1:$PORT"
for _ in $(seq 1 20); do
  if curl -fs "$URL/api/me" >/dev/null 2>&1 || "$ROOT/venv/bin/python" -c "import urllib.request,sys; urllib.request.urlopen('$URL/api/me', timeout=1)" >/dev/null 2>&1; then
    printf '\nГотово! Сервис работает: %s\n' "$URL"
    command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || { [ "$OS" = "Darwin" ] && open "$URL" || true; }
    exit 0
  fi
  sleep 0.5
done
die "Сервис не ответил за 10 секунд. Смотрите data/server.log"
