#!/usr/bin/env bash
# Bir bota (varsayilan: fatih) KENDI kod kopyasini ve uygulama ici Gelistirme sayfasini acar (gelistir.py). Ubuntu, root.
# - Kod kopyasi: /opt/crypto_bot/aurumquantbot-<ad>, dal <ad>. Melik'in botu ve agresif eski klasorden calismaya devam eder.
# - Bu botun "Tek tikla guncelle"si ana surumu kendi dalina birlestirir (cakisirsa uygulamaz).
# - Onay kodu: korunan koda (emir/stop/risk/giris) dokunan degisiklikleri uygulamak icin. Sadece ozeti saklanir; kodu bot sahibine VERMEYIN.
# Tekrar calistirmak guvenlidir. Kullanim:  bash /opt/crypto_bot/aurumquantbot/sunucu_fatih_gelistirme.sh [ad]
# Geri alma: rm -r /etc/systemd/system/kriptoquantbot-<ad>.service.d/gelistirme.conf && systemctl daemon-reload && systemctl restart kriptoquantbot-<ad>
set -e
AD=${1:-fatih}
DIR=/opt/crypto_bot
SRC=$DIR/aurumquantbot
DST=$DIR/aurumquantbot-$AD
DATA=$DIR/veri-$AD
UNIT=kriptoquantbot-$AD
[ "$(id -u)" = 0 ] || { echo "root olarak calistirin"; exit 1; }
systemctl cat $UNIT >/dev/null 2>&1 || { echo "$UNIT servisi yok (once sunucu_kur.sh)"; exit 1; }
[ "$AD" != melik ] || { echo "melik botu ana kodda kalir"; exit 1; }

if [ ! -d "$DST/.git" ]; then
  git clone -q "$SRC" "$DST"
  git -C "$DST" remote set-url origin "$(git -C "$SRC" remote get-url origin)"
  git -C "$DST" fetch -q origin
  git -C "$DST" checkout -q -B "$AD" origin/main
  echo "Kod kopyasi acildi: $DST (dal $AD)"
else
  echo "Kod kopyasi zaten var: $DST (dal $(git -C "$DST" rev-parse --abbrev-ref HEAD))"
fi

mkdir -p "$DATA"
if [ ! -f "$DATA/gelistirme_onay.sha256" ] || [ "${YENI_KOD:-0}" = 1 ]; then
  echo
  echo "Yonetici onay kodu belirleyin (korunan koda dokunan degisiklik icin; en az 8 karakter, ekranda gorunmez):"
  read -rs KOD; echo
  [ ${#KOD} -ge 8 ] || { echo "Kod en az 8 karakter olmali"; exit 1; }
  printf '%s' "$KOD" | sha256sum | cut -d' ' -f1 > "$DATA/gelistirme_onay.sha256"
  chmod 600 "$DATA/gelistirme_onay.sha256"
  unset KOD
  echo "Onay kodu kaydedildi (sadece ozeti). Degistirmek icin: YENI_KOD=1 bash $0 $AD"
fi

mkdir -p /etc/systemd/system/$UNIT.service.d
cat > /etc/systemd/system/$UNIT.service.d/gelistirme.conf <<EOF
[Service]
WorkingDirectory=$DST
ExecStart=
ExecStart=$DIR/venv/bin/python $DST/app.py --sunucu
Environment=KQB_KOD_DALI=$AD
Environment=KQB_GELISTIRME=1
EOF
systemctl daemon-reload
systemctl restart $UNIT
sleep 3
systemctl is-active --quiet $UNIT && echo "HAZIR: $AD botu kendi kodundan calisiyor. Admin girisinde menude 'Gelistirme' sayfasi var." \
  || { echo "Servis baslamadi: journalctl -u $UNIT -n 50"; exit 1; }
echo "Not: Gelistirme icin bu botun Ayarlar > Claude kismina API anahtari girilmeli (gunluk butce: GELISTIRME_GUNLUK_USD, varsayilan 5 \$)."
echo "Not: Bu botun kod degisiklikleri sadece bu sunucudadir (dal $AD). Yedek: git -C $DST bundle create /root/$AD-kod.bundle --all"
