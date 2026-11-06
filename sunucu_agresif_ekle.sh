#!/usr/bin/env bash
# Ucuncu bot: agresif (SADECE sanal kasa, ayri linux kullanicisi, ayri veri klasoru). Root ile, SSH'tan calistirilir:
#   cd /opt/crypto_bot/aurumquantbot && git pull --ff-only origin main && bash sunucu_agresif_ekle.sh
# Melik ve fatih yeniden baslatilmaz. Tekrar calistirmak guvenlidir (veri ve sifre korunur). Sifre depoya girmez, yazdirilmaz.
set -euo pipefail
DIR=/opt/crypto_bot
REPO=$DIR/aurumquantbot
DATA=$DIR/veri-agresif
PY=$DIR/venv/bin/python
PORT=8767
AD=kqb-agresif
UNIT=kriptoquantbot-agresif
RULE="OUTPUT -o lo -p tcp -m multiport --dports 8765,8766,2019 -m owner --uid-owner $AD -j REJECT"

[ "$(id -u)" = 0 ] || { echo "Root ile calistirin."; exit 1; }
[ -f "$REPO/agresif.py" ] || { echo "$REPO/agresif.py yok: once git pull."; exit 1; }
[ -x "$PY" ] || { echo "$PY yok: once sunucu_kur.sh."; exit 1; }
HOST=$(grep -oE '^melik\.[^ ]+' /etc/caddy/Caddyfile | head -n1 | cut -d. -f2- || true)
[ -n "$HOST" ] || { echo "Caddyfile'dan adres okunamadi."; exit 1; }
if ! systemctl is-active --quiet "$UNIT"; then
  ss -ltn | grep -q ":$PORT " && { echo "$PORT portu dolu."; exit 1; }
  FREE=$(awk '/MemAvailable/{print $2}' /proc/meminfo)
  [ "$FREE" -ge 1048576 ] || { echo "Bos bellek 1 GB altinda: durduruldu."; exit 1; }
fi

id -u "$AD" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$AD"
install -d -o "$AD" -g "$AD" -m 700 "$DATA"
if [ -e "$DATA/settings.json" ]; then echo "$DATA/settings.json var: agresif bunu kabul etmez, kaldirin."; exit 1; fi

# kullanici kodu ve venv'i okuyabiliyor mu
ad_py() { ( cd "$REPO" && runuser -u "$AD" -- env KQB_APP_DIR="$DATA" PYTHONDONTWRITEBYTECODE=1 "$PY" -c "$1" ); }
if ! ad_py 'import ccxt, numpy, pandas' ; then
  echo "$AD kullanicisi $REPO veya venv'i okuyamiyor (klasor izinleri)."; exit 1
fi

# hesap: servis baslamadan once; varsa atlanir
if ad_py 'import os, sys, config as C, auth
if os.path.basename(C.APP_DIR) != "veri-agresif": sys.exit("yanlis klasor: " + C.APP_DIR)
sys.exit(0 if auth.has_users() else 3)'; then
  echo "Agresif hesabi zaten var, sifreye dokunulmadi."
else
  rc=$?
  [ "$rc" = 3 ] || { echo "Hesap kontrolu basarisiz."; exit 1; }
  while true; do
    read -rs -p "Agresif hesabi sifresi (en az 6 karakter): " P1; echo
    read -rs -p "Sifre tekrar: " P2; echo
    [ "$P1" = "$P2" ] && [ "${#P1}" -ge 6 ] && break
    echo "Sifreler ayni degil veya kisa."
  done
  printf '%s' "$P1" | ad_py 'import os, sys, config as C, auth
if os.path.basename(C.APP_DIR) != "veri-agresif": sys.exit("yanlis klasor: " + C.APP_DIR)
sys.exit(auth.create("agresif", sys.stdin.read(), "admin", first=True) or 0)'
  unset P1 P2
  echo "Agresif hesabi olusturuldu (kullanici adi: agresif)."
fi

TMPU=$(mktemp)
cat > "$TMPU" <<EOF
[Unit]
Description=KriptoQuantBot (agresif, sadece sanal)
After=network-online.target
Wants=network-online.target

[Service]
User=$AD
Group=$AD
WorkingDirectory=$REPO
Environment=KQB_APP_DIR=$DATA
Environment=KQB_PORT=$PORT
Environment=KQB_PUBLIC_HOST=agresif.$HOST
Environment=KQB_UNIT=$UNIT.service
Environment=HOME=$DATA
Environment=PYTHONIOENCODING=utf-8
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStartPre=-+/bin/sh -c 'iptables -C $RULE 2>/dev/null || iptables -I $RULE'
ExecStart=$PY agresif.py --sunucu
Restart=always
RestartSec=10
RestartPreventExitStatus=77
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=$DATA
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
InaccessiblePaths=-$DIR/veri -$DIR/veri-fatih -$DIR/yedek -$DIR/bekci.json -$REPO/.git -/etc/caddy
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes
RestrictNamespaces=yes
LockPersonality=yes
CapabilityBoundingSet=
RestrictAddressFamilies=AF_INET AF_INET6 AF_UNIX
SystemCallArchitectures=native
MemoryHigh=700M
MemoryMax=900M
MemorySwapMax=0
OOMScoreAdjust=500
CPUQuota=50%
Nice=10
TasksMax=128
LimitNOFILE=1024
LimitFSIZE=256M

[Install]
WantedBy=multi-user.target
EOF
DEGISTI=0
if ! cmp -s "$TMPU" "/etc/systemd/system/$UNIT.service"; then
  install -m 644 "$TMPU" "/etc/systemd/system/$UNIT.service"
  DEGISTI=1
fi
rm -f "$TMPU"
ESKI=0
systemctl is-active --quiet "$UNIT" && ESKI=1
systemctl daemon-reload  # calisan servisleri yeniden baslatmaz
systemctl enable --now "$UNIT"
if [ "$DEGISTI" = 1 ] && [ "$ESKI" = 1 ]; then systemctl restart "$UNIT"; fi  # sadece agresifin kendisi

echo "Motor bekleniyor (3 dk'ya kadar)..."
HAZIR=0
for _ in $(seq 1 90); do
  if curl -fsS -m 3 -H "Host: 127.0.0.1:$PORT" "http://127.0.0.1:$PORT/api/health" 2>/dev/null | grep -q '"ok": true'; then HAZIR=1; break; fi
  sleep 2
done
if [ "$HAZIR" != 1 ]; then
  echo "Motor hazir olmadi; adres eklenmedi. Bakin: journalctl -u $UNIT -n 50"
  exit 1
fi

# Caddy: tek paylasilan bilesen. Gecerli degilse eski hali geri konur, melik/fatih etkilenmez.
mkdir -p /etc/caddy/kqb.d
CF=/etc/caddy/Caddyfile
BLOK=/etc/caddy/kqb.d/agresif.caddy
YEDEK=$(mktemp)
cp "$CF" "$YEDEK"
ESKI_BLOK=$(mktemp)
HAD_BLOK=0
if [ -f "$BLOK" ]; then cp "$BLOK" "$ESKI_BLOK"; HAD_BLOK=1; fi
geri_al() {
  cp "$YEDEK" "$CF"
  if [ "$HAD_BLOK" = 1 ]; then cp "$ESKI_BLOK" "$BLOK"; else rm -f "$BLOK"; fi
  echo "Caddy yapilandirmasi gecersiz: eski hali geri kondu. Agresif servisi calisiyor ama adres eklenmedi."
  exit 1
}
cat > "$BLOK.tmp" <<EOF
agresif.$HOST {
    request_body {
        max_size 16KB
    }
    reverse_proxy 127.0.0.1:$PORT
    handle_errors {
        header Content-Type "text/plain; charset=utf-8"
        header Refresh "5"
        respond "Bot yeniden basliyor, bu sayfa kendiliginden yenilenecek..." 503
    }
}
EOF
mv "$BLOK.tmp" "$BLOK"
if ! grep -qF 'import /etc/caddy/kqb.d/*.caddy' "$CF"; then
  cp "$CF" "$CF.bak.$(date +%Y%m%d%H%M%S)"
  TMPC=$(mktemp)
  cat "$CF" > "$TMPC"
  echo 'import /etc/caddy/kqb.d/*.caddy' >> "$TMPC"
  caddy validate --adapter caddyfile --config "$TMPC" >/dev/null 2>&1 || { rm -f "$TMPC"; geri_al; }
  cat "$TMPC" > "$CF"
  rm -f "$TMPC"
fi
caddy validate --adapter caddyfile --config "$CF" >/dev/null 2>&1 || geri_al
systemctl reload caddy || geri_al  # restart degil: acik baglantilar kesilmez
rm -f "$YEDEK" "$ESKI_BLOK"

if [ -d "$DIR/secim" ]; then cp "$REPO/secim.html" "$DIR/secim/index.html"; fi

curl -fsSI -m 15 "https://agresif.$HOST/" >/dev/null 2>&1 || echo "Sertifika henuz hazir degil (ilk acilista 1 dk surebilir)."

echo
echo "HAZIR: https://agresif.$HOST  (kullanici: agresif, sadece sanal kasa)"
echo "Veri: $DATA"
echo "Geri alma: systemctl disable --now $UNIT; rm /etc/systemd/system/$UNIT.service $BLOK; systemctl daemon-reload; systemctl reload caddy; iptables -D $RULE"
