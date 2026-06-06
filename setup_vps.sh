#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# VPS setup script for Barcode Tools API
# Run this on a fresh Ubuntu 22.04/24.04 HostVDS server
#
# Usage: ssh root@YOUR_VPS_IP 'bash -s' < setup_vps.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

echo "=== Barcode Tools VPS Setup ==="

# ── System packages ──────────────────────────────────────────────────
apt-get update -y
apt-get install -y python3 python3-pip python3-venv git ufw curl

# ── Firewall: allow SSH + API port ───────────────────────────────────
ufw allow 22/tcp
ufw allow 8000/tcp
ufw --force enable

# ── Clone or update repo ─────────────────────────────────────────────
APP_DIR="/opt/barcode-tools"

if [ -d "$APP_DIR" ]; then
    cd "$APP_DIR"
    git pull origin main
else
    git clone https://github.com/irdotai/barcode-tools-scraper-back-end.git "$APP_DIR"
    cd "$APP_DIR"
fi

# ── Python virtual env ───────────────────────────────────────────────
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# ── Environment file ─────────────────────────────────────────────────
if [ ! -f .env ]; then
    cp .env.example .env
    echo ""
    echo "╔══════════════════════════════════════════════════════════════╗"
    echo "║  IMPORTANT: Edit /opt/barcode-tools/.env with your         ║"
    echo "║  FlashProxy credentials before starting the server!        ║"
    echo "║                                                            ║"
    echo "║  nano /opt/barcode-tools/.env                              ║"
    echo "╚══════════════════════════════════════════════════════════════╝"
    echo ""
fi

# ── Systemd service (auto-start on boot, auto-restart on crash) ──────
cat > /etc/systemd/system/barcode-api.service << 'EOF'
[Unit]
Description=Barcode Tools API
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/barcode-tools
EnvironmentFile=/opt/barcode-tools/.env
ExecStart=/opt/barcode-tools/venv/bin/uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable barcode-api

echo ""
echo "=== Setup complete! ==="
echo ""
echo "Next steps:"
echo "  1. Edit your proxy credentials:  nano /opt/barcode-tools/.env"
echo "  2. Start the server:             systemctl start barcode-api"
echo "  3. Check status:                 systemctl status barcode-api"
echo "  4. View logs:                    journalctl -u barcode-api -f"
echo ""
echo "API will be at:  http://YOUR_VPS_IP:8000"
echo ""
