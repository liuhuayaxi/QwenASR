#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
exec venvs/gateway/bin/python scripts/probe.py "$@"
