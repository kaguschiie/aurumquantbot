# KriptoQuantBot

Binance spot için yerel risk yöneticili al-sat botu. Ana strateji **Akıllı Portföy** (`portfoy.py`): kasanın %70'i BTC/ETH trend çekirdeği, %30'u haftalık ML sepeti; her gün 00:05 UTC'de yeniden dengelenir. Sanal kasa (PAPER) her zaman çalışır; gerçek hesap (LIVE) sadece admin izniyle.

- Windows: `exe_olustur.bat` ile masaüstü uygulaması (Releases'te hazır `.zip`).
- Linux sunucu (VDS): aşağıdaki kurulum, arayüz tarayıcıdan açılır.

## Ubuntu sunucu kurulumu

```bash
apt update && apt install -y python3 python3-venv python3-pip git
mkdir -p /opt/crypto_bot && cd /opt/crypto_bot
git clone https://github.com/kaguschiie/aurumquantbot.git
python3 -m venv venv
venv/bin/pip install -r aurumquantbot/requirements-sunucu.txt
```

Deneme çalıştırma (Ctrl+C ile durur):

```bash
cd /opt/crypto_bot/aurumquantbot
KQB_APP_DIR=/opt/crypto_bot/veri ../venv/bin/python app.py --sunucu
```

Sürekli çalışması (sunucu yeniden başlasa da açılır):

```bash
cp /opt/crypto_bot/aurumquantbot/kriptoquantbot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kriptoquantbot
systemctl status kriptoquantbot
journalctl -u kriptoquantbot -f
```

## Herkese açık güvenli adres (HTTPS) — önerilen

Tek komut: Caddy + ücretsiz `IP.sslip.io` adresi + otomatik sertifika + bot servisi + güvenlik duvarı.

```bash
bash /opt/crypto_bot/aurumquantbot/sunucu_kur.sh
```

Sonunda yazan `https://...sslip.io` adresi tarayıcıdan ve masaüstü uygulamasından açılır. Her kullanıcı kendi adı ve şifresiyle girer (Ayarlar → Kullanıcılar); oturum 30 gün açık kalır, güncellemede düşmez.

Claude aboneliği (sunucuda bir kez):

```bash
curl -fsSL https://claude.ai/install.sh | bash
~/.local/bin/claude
```

Açılan ekranda `/login` → çıkan bağlantıyı bilgisayarda aç, kodu yapıştır, `/exit`.

## Arayüze erişim (SSH tüneli, HTTPS kurmadan)

Bot arayüzü güvenlik için sadece sunucunun içinde (`127.0.0.1:8765`) açılır. Kendi bilgisayarından SSH tüneliyle bağlan:

```bash
ssh -L 8765:127.0.0.1:8765 root@SUNUCU_IP
```

Sonra tarayıcıda `http://127.0.0.1:8765` → ilk açılışta admin hesabını oluştur. API anahtarları ve Telegram sadece arayüzdeki **Ayarlar**'dan girilir; dosyaya veya koda yazılmaz.

## Güvenlik, yedek, bekçi

```bash
bash /opt/crypto_bot/aurumquantbot/sunucu_guvenlik.sh
```

Sistem güncellemesi, otomatik güvenlik yamaları, fail2ban; dakikada bir bot kontrolü (3 dakika cevap yoksa Telegram'dan haber verir, sunucunun tamamı kapanırsa haber veremez); her gece 03:30 veri yedeği (`/opt/crypto_bot/yedek`, 14 gün). SSH anahtarı ekledikten sonra şifreli girişi kapatmak için: `bash sunucu_guvenlik.sh sifre-kapat`. Yedeği bilgisayara indirmek: `yedek_indir.ps1`.

## Güncelleme

Arayüzde: Ayarlar → Güncelleme → **Tek tıkla güncelle** (admin). Sunucu GitHub'dan yeni kodu çeker ve kendini yeniden başlatır; hesaplar, oturumlar, kasa ve Telegram bağlantısı korunur. Elle:

```bash
cd /opt/crypto_bot/aurumquantbot && git pull && ../venv/bin/pip install -q -r requirements-sunucu.txt && systemctl restart kriptoquantbot kriptoquantbot-fatih
```

Veriler (`/opt/crypto_bot/veri`: kasa, ayarlar, anahtarlar, günlükler) güncellemede korunur ve depoya girmez.

> Bu bir yazılımdır, yatırım tavsiyesi değildir.
