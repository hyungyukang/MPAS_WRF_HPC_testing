#!/bin/bash
# Run on the offline HPC (login node). Creates <perfsonde>/.venv from the wheels.
#
#   module load python/3.14.5
#   ./offline/install_reframe.sh /path/to/wheels      (or perfsonde-wheels.tar.gz)
set -euo pipefail

SRC=${1:?usage: install_reframe.sh <wheels-dir | perfsonde-wheels.tar.gz>}
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV=${PERFSONDE_VENV:-$ROOT/.venv}

if [ -f "$SRC" ]; then
  tmp=$(mktemp -d); tar xzf "$SRC" -C "$tmp"; SRC=$(find "$tmp" -name '*.whl' -printf '%h\n' | head -1)
fi
[ -d "$SRC" ] || { echo "no wheel directory: $SRC" >&2; exit 1; }

PY=${PYTHON:-python3}
echo "python: $(command -v "$PY") ($("$PY" --version 2>&1))"
"$PY" -m venv "$VENV"
"$VENV/bin/python" -m pip install --no-index --find-links "$SRC" --upgrade pip >/dev/null || true
"$VENV/bin/python" -m pip install --no-index --find-links "$SRC" reframe-hpc
"$VENV/bin/python" -m pip install --no-index --find-links "$SRC" matplotlib \
  || echo "(matplotlib not installed: plots can be drawn on another machine from results/summary.csv)"

"$VENV/bin/reframe" --version
echo "OK: venv at $VENV  (perfsonde activates it automatically)"
echo "next: ./perfsonde discover   (writes config/sites/<name>/), then ./perfsonde selftest"
