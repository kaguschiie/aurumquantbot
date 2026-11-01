#!/usr/bin/env bash
# KriptoQuantBot sunucu kurulumu (Ubuntu 22.04/24.04, root). Her kisiye AYRI bot (kendi Binance API'si, kasasi,
# ayarlari, Telegram'i); kod ortak, tek guncelleme hepsini gunceller. HTTPS: Caddy + ucretsiz sslip.io adresleri.
# Tekrar calistirmak guvenlidir (veriler korunur). Kullanim:
#   bash /opt/crypto_bot/aurumquantbot/sunucu_kur.sh
set -e
DIR=/opt/crypto_bot
REPO=$DIR/aurumquantbot
IP=$(curl -fsS4 https://api.ipify.org || hostname -I | awk '{print $1}')
HOST="${IP//./-}.sslip.io"
# ad:port:veri klasoru (melik = ilk kurulan bot, mevcut verisiyle)
BOTS="melik:8765:$DIR/veri fatih:8766:$DIR/veri-fatih"

apt-get update -y
apt-get install -y caddy ufw python3-venv git curl
[ -x $DIR/venv/bin/python ] || python3 -m venv $DIR/venv
$DIR/venv/bin/pip install -q -r $REPO/requirements-sunucu.txt

mkdir -p $DIR/secim && cp $REPO/secim.html $DIR/secim/index.html
{
  echo "$HOST {"
  echo "    root * $DIR/secim"
  echo "    file_server"
  echo "}"
  for b in $BOTS; do
    IFS=: read -r NAME PORT DATA <<< "$b"
    echo "$NAME.$HOST {"
    echo "    encode gzip"
    echo "    reverse_proxy 127.0.0.1:$PORT"
    echo "    handle_errors {"   # bot yeniden baslarken hata sayfasi yerine kendini yenileyen bilgi
    echo "        header Content-Type \"text/plain; charset=utf-8\""
    echo "        header Refresh \"5\""
    echo "        respond \"Bot yeniden basliyor, bu sayfa kendiliginden yenilenecek...\" 503"
    echo "    }"
    echo "}"
  done
} > /tmp/kqb.Caddyfile
caddy validate --adapter caddyfile --config /tmp/kqb.Caddyfile   # gecersizse calisan yapilandirmaya dokunulmaz
mv /tmp/kqb.Caddyfile /etc/caddy/Caddyfile
systemctl enable caddy >/dev/null 2>&1 || true
systemctl restart caddy

for b in $BOTS; do
  IFS=: read -r NAME PORT DATA <<< "$b"
  UNIT=kriptoquantbot$([ "$NAME" = melik ] || echo "-$NAME")
  mkdir -p "$DATA"
  cat > /etc/systemd/system/$UNIT.service <<EOF
[Unit]
Description=KriptoQuantBot ($NAME)
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=$REPO
Environment=KQB_APP_DIR=$DATA
Environment=KQB_PORT=$PORT
Environment=KQB_PUBLIC_HOST=$NAME.$HOST
Environment=KQB_UNIT=$UNIT.service
Environment=PYTHONIOENCODING=utf-8
ExecStart=$DIR/venv/bin/python app.py --sunucu
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF
  systemctl daemon-reload
  systemctl enable $UNIT >/dev/null 2>&1
  systemctl restart $UNIT
  echo "Bot hazir: $NAME -> https://$NAME.$HOST"
done

ufw allow OpenSSH >/dev/null
ufw allow 80/tcp >/dev/null
ufw allow 443/tcp >/dev/null
ufw --force enable >/dev/null

echo
echo "HAZIR. Uygulama / tarayici: https://$HOST  (bot secim ekrani)"
echo "(Sertifikalar ilk acilista 10-60 sn surebilir.)"
