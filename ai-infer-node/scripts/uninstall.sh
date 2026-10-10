#!/usr/bin/env bash
set -euo pipefail
# No model, data, token or source deletion. Root is needed for service removal.
[[ $EUID -eq 0 ]] || { echo 'Run as WSL root to remove the system service.' >&2; exit 1; }
systemctl stop ai-infer-gateway.service || true
systemctl disable ai-infer-gateway.service || true
rm -f /etc/systemd/system/ai-infer-gateway.service
systemctl daemon-reload
echo 'Service removed. Models, job data and source retained. Remove Windows task AI Infer WSL Gateway separately.'
