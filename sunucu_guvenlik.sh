#!/usr/bin/env bash
# Sunucu guvenligi ve surekliligi (Ubuntu, root). Tekrar calistirmak guvenlidir. Kullanim:
#   bash sunucu_guvenlik.sh              -> sistem guncellemesi, otomatik guvenlik yamalari, fail2ban, bekci, gunluk yedek
#   bash sunucu_guvenlik.sh sifre-kapat  -> SSH sifreli girisi kapatir (once SSH anahtariyla giris denenmis olmali)
set -e
DIR=/opt/crypto_bot
REPO=$DIR/aurumquantbot
PY=$DIR/venv/bin/python
AK=/root/.ssh/authorized_keys
# Yedek indirme anahtari: sadece yedek dosyasini okuyabilir, sunucuda baska hicbir sey yapamaz
YEDEK_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIK8nBE1/6oi7VamGjJJqvLj2SGKGI9GO8c1+es26jNDU kqb-yedek"

if [ "$1" = "sifre-kapat" ]; then
  # Kisitli yedek anahtari sayilmaz: satir basinda duz anahtar olmali, yoksa sunucuya girisiniz kapanir
  if ! grep -qE '^(ssh-ed25519|ssh-rsa|ecdsa-sha2)' "$AK" 2>/dev/null; then
    echo "DURDURULDU: giris icin SSH anahtari bulunamadi. Sifre KAPATILMADI. Once bilgisayarinizdan anahtari ekleyin."
    exit 1
  fi
  cat > /etc/ssh/sshd_config.d/00-kqb.conf <<EOF
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
  if ! sshd -t; then
    rm -f /etc/ssh/sshd_config.d/00-kqb.conf
    echo "DURDURULDU: SSH ayari gecersiz, geri alindi."
    exit 1
  fi
  systemctl reload ssh
  sshd -T | grep -E '^(passwordauthentication|permitrootlogin) '
  echo
  echo "Sifreli giris kapandi. BU PENCEREYI KAPATMAYIN: yeni bir pencerede 'ssh root@sunucu' ile girisi deneyin."
  echo "Giris olmazsa bu pencerede geri alin: rm /etc/ssh/sshd_config.d/00-kqb.conf && systemctl reload ssh"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=a
apt-get update -y
apt-get upgrade -y -o Dpkg::Options::=--force-confold
apt-get install -y unattended-upgrades fail2ban
cat > /etc/apt/apt.conf.d/20auto-upgrades <<EOF
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
systemctl enable --now fail2ban >/dev/null 2>&1

mkdir -p /root/.ssh && chmod 700 /root/.ssh && touch "$AK" && chmod 600 "$AK"
grep -qF "${YEDEK_KEY% *}" "$AK" || echo "command=\"cat $DIR/yedek/son.tar.gz\",restrict $YEDEK_KEY" >> "$AK"

unit() {  # ad, is, zamanlama satiri
  cat > /etc/systemd/system/kqb-$1.service <<EOF
[Unit]
Description=KriptoQuantBot $1

[Service]
Type=oneshot
ExecStart=$PY $REPO/sunucu_bakim.py $1
EOF
  cat > /etc/systemd/system/kqb-$1.timer <<EOF
[Unit]
Description=KriptoQuantBot $1 zamanlayici

[Timer]
$2
Persistent=true

[Install]
WantedBy=timers.target
EOF
}
unit bekci "OnBootSec=2min
OnUnitActiveSec=1min"
unit yedek "OnCalendar=*-*-* 03:30:00"
systemctl daemon-reload
systemctl enable --now kqb-bekci.timer kqb-yedek.timer >/dev/null 2>&1
systemctl start kqb-yedek.service
$PY $REPO/sunucu_bakim.py bekci

echo
echo "HAZIR:"
systemctl list-timers 'kqb-*' --no-pager | head -4
ls -la $DIR/yedek | tail -3
fail2ban-client status sshd | head -3 || true
[ -f /var/run/reboot-required ] && echo "NOT: cekirdek guncellendi; uygun bir zamanda 'reboot' yapin (botlar kendiliginden acilir)."
exit 0
