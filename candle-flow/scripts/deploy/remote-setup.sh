#!/bin/bash
# Run on the Ubuntu 轻量 server as root after the project is unpacked to /opt/candle-flow
set -euo pipefail

APP=/opt/candle-flow
BACKEND="$APP/backend"

export DEBIAN_FRONTEND=noninteractive

if ! swapon --show | grep -q .; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

apt-get update
apt-get install -y python3 python3-venv python3-pip tar curl ca-certificates

id -u www-data >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin www-data

python3 -m venv "$BACKEND/.venv"
"$BACKEND/.venv/bin/pip" install -U pip wheel
"$BACKEND/.venv/bin/pip" install -r "$BACKEND/requirements.txt"
"$BACKEND/.venv/bin/pip" install httpx==0.27.2

mkdir -p "$BACKEND/data/pay/claims"
chown -R www-data:www-data "$APP"
chmod 640 "$BACKEND/.env" || true

install -m 644 "$APP/scripts/deploy/candle-flow.service" /etc/systemd/system/candle-flow.service
systemctl daemon-reload
systemctl enable --now candle-flow

mkdir -p /etc/cloudflared
CRED_SRC="$APP/scripts/deploy/tunnel-credentials.json"
if [ -f "$CRED_SRC" ]; then
  install -m 600 "$CRED_SRC" /etc/cloudflared/f3eb03d8-7017-488e-9176-79377668ea3f.json
fi
install -m 644 "$APP/scripts/deploy/cloudflared.yml" /etc/cloudflared/config.yml

if [ ! -x /usr/local/bin/cloudflared ]; then
  ARCH=$(uname -m)
  if [ "$ARCH" = "aarch64" ] || [ "$ARCH" = "arm64" ]; then
    CF_URL="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-arm64"
  else
    CF_URL="https://github.com/cloudflare/cloudflared/releases/download/2025.8.1/cloudflared-linux-amd64"
  fi
  if ! curl -fL --connect-timeout 20 --max-time 180 "https://ghfast.top/$CF_URL" -o /usr/local/bin/cloudflared; then
    curl -fL --connect-timeout 20 --max-time 180 "$CF_URL" -o /usr/local/bin/cloudflared
  fi
  chmod +x /usr/local/bin/cloudflared
fi

/usr/local/bin/cloudflared service install --config /etc/cloudflared/config.yml || true
systemctl enable --now cloudflared
systemctl restart candle-flow
systemctl restart cloudflared || true

sleep 2
curl -fsS http://127.0.0.1:8002/api/v1/health || true
echo "SETUP_OK"
