#!/usr/bin/env bash
# ComfyLocal - start na Linuxu (jakákoliv distribuce s Python 3.10+).
# Funguje stejně jako START_WINDOWS.bat: první spuštění si samo připraví
# config.json a virtuální prostředí, další spuštění jen doinstaluje závislosti
# pokud se změnil requirements.txt.
set -euo pipefail
cd "$(dirname "$0")"

echo "=================================================================="
echo " ComfyLocal - start"
echo "=================================================================="

# 1) Python 3.10+
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    ver="$("$cand" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")' 2>/dev/null || echo "0.0")"
    major="${ver%%.*}"; minor="${ver#*.}"
    if [ "$major" -eq 3 ] && [ "$minor" -ge 10 ] 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "[CHYBA] Python 3.10+ nenalezen."
  echo "Nainstaluj ho balíčkovým manažerem distribuce, např.:"
  echo "  Debian/Ubuntu: sudo apt install python3 python3-venv python3-pip"
  echo "  Fedora:        sudo dnf install python3 python3-pip"
  echo "  Arch:          sudo pacman -S python python-pip"
  exit 1
fi
echo "[INFO] Používám $PY ($($PY --version))."

# 2) Konfigurace
if [ ! -f "config.json" ]; then
  echo "[INFO] config.json neexistuje - kopíruji z config.example.json"
  cp config.example.json config.json
  echo "[INFO] Zkontroluj v config.json adresu comfy_url."
fi

# 3) Virtuální prostředí
if [ ! -x ".venv/bin/python" ]; then
  echo "[INFO] Vytvářím virtuální prostředí .venv ..."
  "$PY" -m venv .venv
fi
VENV_PY=".venv/bin/python"

# 4) Závislosti - přeinstalují se jen když se změnil requirements.txt
REQ_STAMP="$(stat -c '%Y' requirements.txt 2>/dev/null || stat -f '%m' requirements.txt)"
NEED_INSTALL=1
if [ -f ".venv/.deps_ok" ]; then
  DEPS_STAMP="$(cat .venv/.deps_ok 2>/dev/null || echo '')"
  if [ "$DEPS_STAMP" = "$REQ_STAMP" ]; then
    NEED_INSTALL=0
  fi
fi
if [ "$NEED_INSTALL" = "1" ]; then
  echo "[INFO] Instaluji závislosti ..."
  "$VENV_PY" -m pip install --upgrade pip >/dev/null 2>&1 || true
  if ! "$VENV_PY" -m pip install -r requirements.txt; then
    echo
    echo "[CHYBA] Instalace závislostí selhala."
    echo "Nejčastější důvod je firemní proxy nebo blokovaný přístup na pypi.org."
    echo "Zkus ručně: $VENV_PY -m pip install -r requirements.txt"
    exit 1
  fi
  echo "$REQ_STAMP" > .venv/.deps_ok
fi

# 5) Start
echo "[INFO] Spouštím ComfyLocal ... (ukončení: Ctrl+C)"
echo "[INFO] Log se zapisuje do data/logs/comfylocal.log"
echo
set +e
"$VENV_PY" -m comfylocal
EXITCODE=$?
set -e
echo
if [ "$EXITCODE" != "0" ]; then
  echo "[CHYBA] ComfyLocal skončil s chybou $EXITCODE."
  echo "Celý výpis včetně chyby najdeš v souboru: data/logs/comfylocal.log"
else
  echo "[INFO] ComfyLocal skončil."
fi
