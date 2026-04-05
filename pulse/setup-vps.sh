#!/bin/bash
# One-time VPS setup for BLACK DELTA
# Run as root on your Hostinger VPS
set -e

echo "=== BLACK DELTA VPS Setup ==="

# 1. Install dependencies
echo "[1/6] Installing packages..."
apt update -qq
apt install -y nginx certbot python3-certbot-nginx python3-pip apache2-utils

# 2. Setup Basic Auth
echo ""
echo "[2/6] Setting up authentication..."
read -p "Choose a username: " AUTH_USER
htpasswd -c /etc/nginx/.htpasswd-blackdelta "$AUTH_USER"
echo "Auth configured for user: $AUTH_USER"

# 3. Nginx config
echo "[3/6] Configuring Nginx..."
cp /root/black-delta/pulse/nginx-black-delta.conf /etc/nginx/sites-available/black-delta
ln -sf /etc/nginx/sites-available/black-delta /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default

# Temporarily disable SSL lines for initial certbot run
sed -i 's/listen 443/#listen 443/' /etc/nginx/sites-available/black-delta
sed -i 's/ssl_/#ssl_/' /etc/nginx/sites-available/black-delta
sed -i '/return 301/d' /etc/nginx/sites-available/black-delta
nginx -t && systemctl reload nginx

# 4. SSL Certificate
echo "[4/6] Getting SSL certificate..."
certbot --nginx -d black-delta.net -d www.black-delta.net --non-interactive --agree-tos --email admin@black-delta.net

# Restore full config with SSL
cp /root/black-delta/pulse/nginx-black-delta.conf /etc/nginx/sites-available/black-delta
nginx -t && systemctl reload nginx

# 5. Systemd service
echo "[5/6] Setting up systemd service..."
cp /root/black-delta/pulse/black-delta.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable black-delta
systemctl start black-delta

# 6. Firewall
echo "[6/6] Opening ports..."
ufw allow 80/tcp
ufw allow 443/tcp

echo ""
echo "=== Setup complete ==="
echo "Dashboard: https://black-delta.net"
echo "Auth: $AUTH_USER / (password you entered)"
echo ""
echo "Next step: Point black-delta.net DNS to this server's IP"
echo "Server IP: $(curl -s ifconfig.me)"
