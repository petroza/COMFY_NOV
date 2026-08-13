#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "[CHYBA] Python 3.10 nebo novější nebyl nalezen."
  exit 1
fi
"$PY" INSTALL.py
exec bash START_LINUX.sh
