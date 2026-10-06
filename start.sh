#!/usr/bin/env bash
# Ручной запуск сервиса в текущем терминале (Linux / macOS).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[ -x "$ROOT/venv/bin/python" ] || { echo "Сначала выполните ./install.sh" >&2; exit 1; }
exec "$ROOT/venv/bin/python" "$ROOT/app/server.py"
