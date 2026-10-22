#!/usr/bin/env bash
set -euo pipefail
NODE_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
NODE_USER="${AI_INFER_USER:-$(stat -c %U "$NODE_ROOT")}"
NODE_HOST="$(python3 -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["server"]["host"])' "$NODE_ROOT/config.toml")"
NODE_PORT="$(python3 -c 'import sys,tomllib;print(tomllib.load(open(sys.argv[1],"rb"))["server"]["port"])' "$NODE_ROOT/config.toml")"
[[ $EUID -eq 0 && "$NODE_USER" != root ]] || { echo 'Run as WSL root; project must be owned by a non-root user.' >&2; exit 1; }
if systemctl is-active --quiet ai-infer-gateway.service; then
  echo 'Stop ai-infer-gateway.service before replacing its unit, then rerun.' >&2
  exit 1
fi
NODE_UNIT=/etc/systemd/system/ai-infer-gateway.service
if [[ -e "$NODE_UNIT" ]] && ! cmp -s "$NODE_UNIT" "$NODE_ROOT/ai-infer-gateway.service"; then
  cp -a "$NODE_UNIT" "$NODE_ROOT/private/service-backup-$(date +%Y%m%d-%H%M%S)"
fi
cat > "$NODE_ROOT/ai-infer-gateway.service" <<EOF
[Unit]
Description=On-demand AI inference gateway (no resident models)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$NODE_USER
WorkingDirectory=$NODE_ROOT
Environment=AI_INFER_CONFIG=$NODE_ROOT/config.toml
Environment=PYTHONUNBUFFERED=1
Environment=PATH=$NODE_ROOT/venvs/gateway/bin:/usr/local/bin:/usr/bin:/bin:/usr/lib/wsl/lib
ExecStart=$NODE_ROOT/venvs/gateway/bin/python -m uvicorn server.app:app --host $NODE_HOST --port $NODE_PORT --workers 1 --no-access-log --no-proxy-headers --timeout-keep-alive 15
Restart=on-failure
RestartSec=5
KillMode=control-group
Delegate=yes
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
PrivateTmp=true

[Install]
WantedBy=multi-user.target
EOF
install -m 644 "$NODE_ROOT/ai-infer-gateway.service" "$NODE_UNIT"
systemctl daemon-reload
systemctl enable --now ai-infer-gateway.service
runuser -u "$NODE_USER" -- "$NODE_ROOT/venvs/gateway/bin/python" "$NODE_ROOT/scripts/check_health.py" --config "$NODE_ROOT/config.toml"
systemctl --no-pager status ai-infer-gateway.service
