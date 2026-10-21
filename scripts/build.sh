#!/bin/bash
# scripts/build.sh [out] - build Zeus as one file: a Python zipapp (standard library only) with a python3 shebang.
#   bash scripts/build.sh              -> dist/zeus          bash scripts/build.sh /tmp/zeus   -> that path
# Install it as ~/.local/bin/zeus on the server (scripts/install.sh). Needs only python3 (on a PC where only `python`
# answers, that is used to build; the file's shebang still says python3, for the server).
set -e
R=$(cd "$(dirname "$0")/.." && pwd); V=$(cat "$R/VERSION"); OUT=${1:-$R/dist/zeus}
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if python3 -c pass >/dev/null 2>&1; then PY=python3; elif python -c pass >/dev/null 2>&1; then PY=python
  else echo "build.sh: neither python3 nor python runs; install Python 3" >&2; exit 1; fi
fi
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/app" "$(dirname "$OUT")"
cp -r "$R/zeus" "$T/app/zeus"; find "$T/app" -name __pycache__ -type d -exec rm -rf {} +
echo "$V" > "$T/app/zeus/VERSION"               # zeus.version() reads it from inside the archive
cp "$R/zeus/__main__.py" "$T/app/__main__.py"   # the zipapp's entry point: sys.exit(main())
SRC="$T/app"; DST="$OUT"
if command -v cygpath >/dev/null 2>&1; then   # Git Bash on a PC: hand Python Windows paths and keep the shebang as is,
  SRC=$(cygpath -w "$SRC"); DST=$(cygpath -w "$DST"); export MSYS_NO_PATHCONV=1   # or /usr/bin/env becomes C:/...
fi
"$PY" -m zipapp "$SRC" -o "$DST" -p "/usr/bin/env python3" -c
unset MSYS_NO_PATHCONV
chmod +x "$OUT"
echo "built $OUT (zeus $V, $(du -h "$OUT" | cut -f1))"
