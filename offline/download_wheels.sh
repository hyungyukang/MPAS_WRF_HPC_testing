#!/bin/bash
# Run on a machine WITH internet access. Produces wheels/ for the offline HPC.
#
#   ./download_wheels.sh            # wheels for CPython 3.14, x86_64 Linux (HPC python/3.14.5)
#   PYVER=3.12 ./download_wheels.sh # if you will use a different Python on the HPC
#   WITH_PLOT=0 ./download_wheels.sh # skip matplotlib (plots can be made elsewhere)
#
# The online machine does not need Python 3.14: pip fetches wheels for the
# target interpreter/platform given below. Copy the resulting wheels/ directory
# (or perfsonde-wheels.tar.gz) to the HPC and run offline/install_reframe.sh.
set -euo pipefail

PYVER=${PYVER:-3.14}
ABI="cp${PYVER/./}"
OUT=${OUT:-wheels}
REFRAME_SPEC=${REFRAME_SPEC:-reframe-hpc}
WITH_PLOT=${WITH_PLOT:-1}

PKGS=("$REFRAME_SPEC")
[ "$WITH_PLOT" = 1 ] && PKGS+=(matplotlib)

PY=${PYTHON:-python3}
mkdir -p "$OUT"
"$PY" -m pip download "${PKGS[@]}" pip setuptools wheel \
  --dest "$OUT" \
  --only-binary=:all: \
  --python-version "$PYVER" --implementation cp --abi "$ABI" --abi abi3 --abi none \
  --platform manylinux_2_28_x86_64 --platform manylinux_2_17_x86_64 \
  --platform manylinux2014_x86_64 --platform any

ls -1 "$OUT" | sed 's/^/  /'
tar czf perfsonde-wheels.tar.gz "$OUT"
echo "OK: $(ls "$OUT" | wc -l) wheels -> $OUT/ and perfsonde-wheels.tar.gz"
echo "If a package has no wheel for Python $PYVER, rerun with REFRAME_SPEC='reframe-hpc==<older>'"
echo "or PYVER set to the Python you will use on the HPC (e.g. from miniforge3)."
