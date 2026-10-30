#!/usr/bin/env bash
# One command for the drills: investigate a single case from a clean clone.
#   ./run_case.sh HHF-003            # DuckDB lane
#   ./run_case.sh HHF-003 tg         # TigerGraph lane (TG_HOST and TG_SECRET in .env)
# First run on a clean clone performs the setup: venv, requirements, organizer downloads, index (about 10 minutes).
# Exit codes: 0 answer written, 2 the investigation failed closed (see incidents/), 1 setup error.
set -euo pipefail
cd "$(dirname "$0")"
CASE="${1:?usage: ./run_case.sh <case_id> [duck|tg|mcp] [extra kavach flags]}"
BACKEND="${2:-duck}"
shift $(( $# >= 2 ? 2 : 1 ))
if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
if [ ! -x "$PY" ]; then
  echo "setup: creating .venv and installing requirements"
  for cand in "py -3.11" "py -3.12" python3.11 python3.12 python python3; do
    if $cand -m venv .venv 2>/dev/null; then break; fi
  done
  if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
  "$PY" -m pip install --quiet -r requirements.txt || exit 1
fi
[ -f .env ] || { cp .env.example .env; echo "setup: created .env from .env.example"; }
if [ ! -f data/kavach_r2.duckdb ]; then
  echo "setup: downloading organizer files and building the index"
  "$PY" -m kavach download --round 1 && "$PY" -m kavach download --round 2 && "$PY" -m kavach index || exit 1
fi
if [ ! -f data/vectors.npz ]; then
  echo "setup: embedding the closed-case notes and policy sections (one time, a few minutes)"
  "$PY" -m kavach vectors || exit 1
fi
exec "$PY" -m kavach investigate "$CASE" --backend "$BACKEND" "$@"
