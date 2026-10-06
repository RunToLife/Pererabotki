#!/usr/bin/env bash
# Обновление офлайн-библиотек в vendor/wheels (нужен интернет; запускать на машине разработчика).
# Скачивает колёса для Python 3.8–3.14 под Windows (x64/x86), Linux (x86_64/aarch64) и macOS.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$ROOT/vendor/wheels"
PY="${PY:-python3}"
rm -rf "$DEST"; mkdir -p "$DEST"
for v in 3.8 3.9 3.10 3.11 3.12 3.13 3.14; do
  for p in win_amd64 win32 manylinux2014_x86_64 manylinux2014_aarch64 macosx_11_0_arm64 macosx_10_12_x86_64 macosx_10_13_x86_64; do
    "$PY" -m pip download -q --only-binary=:all: --python-version "$v" --platform "$p" -d "$DEST" -r "$ROOT/requirements.txt" || echo "пропуск: Python $v / $p"
  done
done
# Зависимости, которые ставятся только на части платформ (маркеры окружения)
for v in 3.8 3.9; do
  "$PY" -m pip download -q --only-binary=:all: --python-version "$v" --platform any -d "$DEST" "importlib-metadata>=3.6" colorama
done
"$PY" -m pip download -q --only-binary=:all: -d "$DEST" colorama
# pip для Linux без пакета python3-venv (venv создаётся --without-pip)
"$PY" -m pip download -q --no-deps --only-binary=:all: -d "$DEST" "pip==25.0.1"
echo "Готово: $(ls "$DEST" | wc -l) файлов в $DEST"
