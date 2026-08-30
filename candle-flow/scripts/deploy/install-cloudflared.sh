#!/bin/bash
set -euo pipefail
pkill -f "github.com/cloudflare/cloudflared" || true
rm -f /usr/local/bin/cloudflared
ok=0
for url in \
  "https://ghfast.top/https://github.com/cloudflare/cloudflared/releases/download/2025.8.1/cloudflared-linux-amd64" \
  "https://github.com/cloudflare/cloudflared/releases/download/2025.8.1/cloudflared-linux-amd64"
do
  echo "TRY $url"
  if curl -fL --retry 2 --connect-timeout 20 --max-time 180 "$url" -o /usr/local/bin/cloudflared; then
    ok=1
    break
  fi
done
if [ "$ok" != 1 ]; then
  echo DOWNLOAD_FAIL
  exit 1
fi
chmod +x /usr/local/bin/cloudflared
/usr/local/bin/cloudflared --version
mkdir -p /etc/cloudflared
install -m 600 /opt/candle-flow/scripts/deploy/tunnel-credentials.json /etc/cloudflared/f3eb03d8-7017-488e-9176-79377668ea3f.json
install -m 644 /opt/candle-flow/scripts/deploy/cloudflared.yml /etc/cloudflared/config.yml
if [ ! -f /etc/systemd/system/cloudflared.service ]; then
  /usr/local/bin/cloudflared --config /etc/cloudflared/config.yml service install
fi
systemctl enable --now cloudflared
sleep 3
systemctl is-active cloudflared
journalctl -u cloudflared -n 25 --no-pager
echo CF_OK
