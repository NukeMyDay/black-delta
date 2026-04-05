#!/bin/bash
# One-time VPS setup for BLACK DELTA (Docker)
# Run as root on your Hostinger VPS
set -e

echo "=== BLACK DELTA VPS Setup ==="

# 1. Install nginx + certbot
echo "[1/5] Installing packages..."
apt update -qq
apt install -y nginx certbot python3-certbot-nginx apache2-utils

# 2. Setup Basic Auth
echo ""
echo "[2/5] Setting up authentication..."
read -p "Choose a username: " AUTH_USER
htpasswd -c /etc/nginx/.htpasswd-blackdelta "$AUTH_USER"
echo "Auth configured for user: $AUTH_USER"

# 3. Nginx config
echo "[3/5] Configuring Nginx..."
cp /root/black-delta/nginx-black-delta.conf /etc/nginx/sites-available/black-delta
ln -sf /etc/nginx/sites-available/black-delta /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Temp config for certbot (no SSL yet)
cat > /etc/nginx/sites-available/black-delta <<'TMPCONF'
server {
    listen 80;
    server_name black-delta.net www.black-delta.net;
    location / {
        proxy_pass http://127.0.0.1:3000;
        auth_basic "Black Delta";
        auth_basic_user_file /etc/nginx/.htpasswd-blackdelta;
    }
}
TMPCONF
nginx -t && systemctl reload nginx

# 4. SSL Certificate
echo "[4/5] Getting SSL certificate..."
certbot --nginx -d black-delta.net -d www.black-delta.net --non-interactive --agree-tos --email admin@black-delta.net

# Restore full config with SSL
cp /root/black-delta/nginx-black-delta.conf /etc/nginx/sites-available/black-delta
nginx -t && systemctl reload nginx

# 5. Start Black Delta
echo "[5/5] Starting Black Delta..."
cd /root/black-delta
cp .env.example .env
echo ""
echo ">>> Edit .env with your credentials: nano .env"
echo ">>> Then start with: docker compose up -d --build"

# Firewall
ufw allow 80/tcp 2>/dev/null || true
ufw allow 443/tcp 2>/dev/null || true

echo ""
echo "=== Setup complete ==="
echo "Dashboard: https://black-delta.net"
echo "Server IP: $(curl -s ifconfig.me)"
echo ""
echo "Next steps:"
echo "  1. Point black-delta.net DNS (A-Record) to this IP"
echo "  2. Edit .env: nano /root/black-delta/.env"
echo "  3. Start: cd /root/black-delta && docker compose up -d --build"
