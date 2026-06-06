#!/bin/bash
# install_city_hall.sh — City Hall Server
# Fixed: dotfile copy uses explicit cp for .env.template,
#        validates API key was changed from default.
# Run as: sudo bash install_city_hall.sh

set -e
APP_DIR="/opt/bioko_server"

echo "========================================================"
echo " Bioko Transit — City Hall Server Installer"
echo "========================================================"

read -p "Database password (min 12 chars): "                 DB_PASS
read -p "API key (shared with all Pi buses): "               API_KEY_IN
read -p "Mechanic phone number (+2406XXXXXXX): "             MECHANIC_PHONE
read -p "Backbone network IP for this server (10.10.0.1): "  BACKBONE_IP
read -p "Backbone ethernet interface (e.g. eth1): "          BACKBONE_IFACE

# Validate inputs
if [ ${#DB_PASS} -lt 12 ]; then
    echo "ERROR: Database password must be at least 12 characters."
    exit 1
fi
if [ "$API_KEY_IN" = "BIOKO_BUS_KEY_CHANGE_ME" ] || [ -z "$API_KEY_IN" ]; then
    echo "ERROR: You must set a real API key — do not use the default."
    exit 1
fi
if [ -z "$BACKBONE_IP" ]; then
    echo "ERROR: Backbone IP cannot be empty."
    exit 1
fi

# ── System packages ───────────────────────────────────────────────────────────
apt-get update -y
apt-get install -y \
    python3 python3-pip python3-venv \
    mariadb-server mariadb-client \
    nginx git curl ufw

# ── MariaDB ───────────────────────────────────────────────────────────────────
systemctl enable --now mariadb
mysql -u root << SQL
CREATE DATABASE IF NOT EXISTS bus_tracking_gq_bioko
    CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'bioko_app'@'localhost'
    IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON bus_tracking_gq_bioko.* TO 'bioko_app'@'localhost';
FLUSH PRIVILEGES;
SQL

# ── Application directory ─────────────────────────────────────────────────────
mkdir -p "$APP_DIR/logs" "$APP_DIR/offline_data" "$APP_DIR/data"

# Copy all files including dotfiles explicitly
cp -r * "$APP_DIR/" 2>/dev/null || true
[ -f .env.template ] && cp .env.template "$APP_DIR/.env.template"
[ -d templates ]     && cp -r templates "$APP_DIR/"
[ -d data ]          && cp -r data      "$APP_DIR/"

# Create .env from template
cp "$APP_DIR/.env.template" "$APP_DIR/.env"
sed -i "s|CHANGE_TO_STRONG_PASSWORD|${DB_PASS}|g"   "$APP_DIR/.env"
sed -i "s|BIOKO_BUS_KEY_CHANGE_ME|${API_KEY_IN}|g"  "$APP_DIR/.env"
sed -i "s|+2406XXXXXXX|${MECHANIC_PHONE}|g"          "$APP_DIR/.env"
sed -i "s|BACKBONE_IP=10.10.0.1|BACKBONE_IP=${BACKBONE_IP}|g" "$APP_DIR/.env"

# ── Python virtual environment ────────────────────────────────────────────────
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip -q
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt"

# ── Static backbone IP ────────────────────────────────────────────────────────
if [ -n "$BACKBONE_IFACE" ]; then
    cat >> /etc/dhcpcd.conf << DHCP

interface ${BACKBONE_IFACE}
    static ip_address=${BACKBONE_IP}/16
    nolink
DHCP
fi

# ── Gunicorn systemd service ──────────────────────────────────────────────────
cat > /etc/systemd/system/bioko-server.service << SERVICE
[Unit]
Description=Bioko Transit City Hall Server
After=network.target mariadb.service
Requires=mariadb.service

[Service]
Type=simple
User=www-data
Group=www-data
WorkingDirectory=${APP_DIR}
EnvironmentFile=${APP_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/gunicorn \
    --workers 4 \
    --bind 0.0.0.0:5000 \
    --timeout 60 \
    --access-logfile ${APP_DIR}/logs/access.log \
    --error-logfile  ${APP_DIR}/logs/error.log \
    "app:create_app()"
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SERVICE

# ── Nginx ─────────────────────────────────────────────────────────────────────
cat > /etc/nginx/sites-available/bioko << NGINX
server {
    listen 80;
    server_name _;
    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host \$host;
        proxy_set_header   X-Real-IP \$remote_addr;
        proxy_set_header   X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_read_timeout 60;
    }
    location /static/ {
        alias ${APP_DIR}/static/;
        expires 1d;
    }
}
NGINX
ln -sf /etc/nginx/sites-available/bioko /etc/nginx/sites-enabled/bioko
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

# ── Firewall ──────────────────────────────────────────────────────────────────
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 5000/tcp
ufw limit ssh
ufw --force enable

chown -R www-data:www-data "$APP_DIR"
systemctl daemon-reload
systemctl enable bioko-server
systemctl start bioko-server

echo ""
echo "========================================================"
echo " Installation complete."
echo " Admin:        http://${BACKBONE_IP}/admin/"
echo " Map:          http://${BACKBONE_IP}/map/"
echo " Fleet health: http://${BACKBONE_IP}/admin/fleet/"
echo ""
echo " Next: import stops and create routes:"
echo "   cd ${APP_DIR}"
echo "   python admin_cli.py import-stops data/bioko_stops_sample.csv"
echo "   python admin_cli.py add-route --help"
echo ""
echo " Check logs: sudo journalctl -u bioko-server -f"
echo "========================================================"
