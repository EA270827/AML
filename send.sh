#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
source .env
exec "${AML_PYTHON:-python3}" send_notifications.py --send
