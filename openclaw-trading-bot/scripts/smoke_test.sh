#!/usr/bin/env bash
# scripts/smoke_test.sh — smoke test local (pas de Docker requis).
#
# Valide en < 5 s que :
#   - la suite pytest passe (498 tests)
#   - `python -m src.main --smoke` exit 0 (wiring FastAPI + SQLite + scheduler)
#
# Utilisation :
#   ./scripts/smoke_test.sh
#   # ou en one-shot sans toucher au repo :
#   bash scripts/smoke_test.sh
#
# Exit code : 0 = tout OK, 1 = échec.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$HERE"

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "[smoke] 1/2 pytest — suite unitaire"
python -m pytest -q

echo "[smoke] 2/2 --smoke — wiring end-to-end"
python -m src.main --smoke \
    --data-dir "$TMP/data" \
    --log-dir "$TMP/logs"

echo "[smoke] OK"
