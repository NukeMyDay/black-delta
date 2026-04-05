#!/bin/bash
# Deploy script for BLACK DELTA on VPS (Docker)
# Usage: ./deploy.sh
set -e

echo "[DEPLOY] Pulling latest code..."
git pull origin main

echo "[DEPLOY] Rebuilding and restarting container..."
docker compose up -d --build

echo "[DEPLOY] Status:"
docker compose ps
echo "[DEPLOY] Done."
