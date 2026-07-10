#!/bin/bash
# Build KalikotAISW.app on macOS.
#   Run from the kalikot-aisw folder:   bash build_macos.sh
#
# Prereqs: Python 3 (brew install python), and the aisw engine on PATH
#   (brew tap burakdede/tap && brew install aisw).
set -e
cd "$(dirname "$0")"

echo "==> Installing build dependencies (pyinstaller, pystray, Pillow)…"
python3 -m pip install --quiet --upgrade pyinstaller pystray Pillow

echo "==> Building app bundle…"
python3 -m PyInstaller \
  --noconfirm --clean --windowed \
  --name KalikotAISW \
  --add-data "app.ico:." \
  --collect-submodules pystray \
  gui/app.py

echo
echo "==> Done:  dist/KalikotAISW.app"
echo "    Launch: open dist/KalikotAISW.app"
echo "    (First launch: right-click the app → Open, to get past Gatekeeper on an unsigned build.)"
