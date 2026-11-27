#!/usr/bin/env bash
set -euo pipefail

# Bootstrap AutoAgentHub as an always-on systemd service on Ubuntu/Debian cloud VM
APP_DIR="${1:-$HOME/AutoAgentHub}"

sudo apt-get update -y
sudo apt-get install -y git curl python3 python3-venv python3-pip

if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

cd "$APP_DIR"
uv sync

if [ ! -f .env ]; then
  cp .env.example .env
  echo "Created .env from .env.example — edit $APP_DIR/.env with your API keys before starting."
fi

SERVICE_FILE="/etc/systemd/system/autoagenthub.service"
sudo tee "$SERVICE_FILE" >/dev/null <<EOF
[Unit]
Description=AutoAgentHub 24/7 Concurrent Agent Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$USER
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/.venv/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$APP_DIR/.venv/bin/autoagenthub-server
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable autoagenthub
sudo systemctl restart autoagenthub
sudo systemctl status autoagenthub --no-pager
