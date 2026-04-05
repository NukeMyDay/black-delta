#!/bin/bash
# Deploy script for BLACK DELTA on VPS
# Usage: ./deploy.sh

set -e

echo "[DEPLOY] Pulling latest code..."
git pull origin main

echo "[DEPLOY] Installing dependencies..."
pip install -r requirements.txt --quiet

echo "[DEPLOY] Restarting service..."
sudo systemctl restart black-delta

echo "[DEPLOY] Status:"
sudo systemctl status black-delta --no-pager -l
echo "[DEPLOY] Done."
