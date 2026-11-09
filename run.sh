#!/usr/bin/env bash
# Wrapper invoked by cron / run by hand. Loads secrets from .env, then runs
# the pipeline. Fails loudly if anything is missing.
set -euo pipefail

cd "$(dirname "$0")"                 # always run from the project directory

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found. Copy .env.example to .env and fill it in." >&2
    exit 1
fi
source .env                          # exports AML_READ_* / AML_WRITE_* vars

# Interpreter: set AML_PYTHON in .env to pin a specific python (recommended for
# cron, e.g. a conda env's bin/python). Otherwise fall back to python3 on PATH.
PYTHON="${AML_PYTHON:-python3}"

exec "$PYTHON" aml_screening.py