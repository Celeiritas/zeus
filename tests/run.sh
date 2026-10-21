#!/bin/bash
# tests/run.sh - run the tests with the standard library's unittest. Needs Python 3, bash and git; nothing is installed.
#   bash tests/run.sh           every tests/test_*.py        bash tests/run.sh config      just tests/test_config.py
# Every test builds its own temporary HOME; nothing outside temporary folders is read or written.
# Python: $PYTHON if set, else python3, else python (Git Bash on a PC often has only `python`).
set -e
R=$(cd "$(dirname "$0")/.." && pwd); cd "$R"
PY=${PYTHON:-}
if [ -z "$PY" ]; then
  if python3 -c pass >/dev/null 2>&1; then PY=python3; elif python -c pass >/dev/null 2>&1; then PY=python
  else echo "tests/run.sh: neither python3 nor python runs; install Python 3" >&2; exit 1; fi
fi
export PYTHON="$PY"
if [ -n "$1" ]; then "$PY" -m unittest -v "tests.test_$1"; else "$PY" -m unittest discover -v -s tests -t .; fi
