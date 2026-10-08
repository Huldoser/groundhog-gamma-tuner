#!/bin/sh
# Open the Groundhog Tuner window on Linux or macOS.
# Uses .venv/bin/python when the folder has one, otherwise python3 on PATH.
cd "$(dirname "$0")" || exit 1
if [ -x .venv/bin/python ]; then
  PYTHON=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
  PYTHON=python3
else
  echo "Could not find Python 3. Install it, then run: python3 -m pip install -r requirements.txt" >&2
  exit 1
fi
exec "$PYTHON" main.py "$@"
