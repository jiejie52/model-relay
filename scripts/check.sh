#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

printf '%s\n' '[1/2] compileall'
python -m compileall -q app
printf '%s\n' '[2/2] unit tests'
PYTHONPATH=. python -m unittest discover -s tests -v
printf '%s\n' 'OK'
