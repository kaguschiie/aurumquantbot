#!/usr/bin/env bash
# KriptoQuantBot sunucu kurulumu (Ubuntu 22.04/24.04, root): HTTPS (Caddy + ucretsiz sslip.io adresi + otomatik
# sertifika), bot servisi, guvenlik duvari. Tekrar calistirmak guvenlidir. Kullanim:
#   bash /opt/crypto_bot/aurumquantbot/sunucu_kur.sh
set -e
DIR=/opt/crypto_bot
REPO=$DIR/aurumquantbot
IP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"
echo "Sunucu IP: $IP  ->  adres: https://$HOST"

apt-get update -y
apt-get install -y caddy ufw python3-venv git curl
[ -x $DIR/venv/bin/python ] || python3 -m venv $DIR/venv
$DIR/venv/bin/pip install -q -r $REPO/requirements-sunucu.txt

cat > /etc/caddy/Caddyfile <<EOF
$HOST {
    encode gzip
    reverse_proxy 127.0.0.1:8765
}
EOF
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

sed "s|^Environment=PYTHONIOENCODING=utf-8|Environment=PYTHONIOENCODING=utf-8\nEnvironment=KQB_PUBLIC_HOST=$HOST|" \
    $REPO/kriptoquantbot.service > /etc/systemd/system/kriptoquantbot.service
systemctl daemon-reload
systemctl enable kriptoquantbot >/dev/null 2>&1
systemctl restart kriptoquantbot

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo
echo "HAZIR. Tarayicida ac:  https://$HOST"
echo "(Sertifika ilk acilista 10-60 sn surebilir.)"
