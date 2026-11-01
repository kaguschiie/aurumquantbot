"""KriptoQuantBot ayarlari. Arayuzdeki Ayarlar sayfasi bu degerleri settings.json uzerinden ezer."""
import os
import sys

# Iki hesap ayni anda calisir: PAPER (admin'in sanal kasasi, her zaman) ve LIVE (kullanicinin gercek Binance hesabi).
# LIVE yeni islemi sadece admin izin verdiyse ve Binance anahtari girildiyse acar; acik pozisyonlari her zaman korur.
LIVE_ALLOWED = False

# LIVE icin: sadece "Spot Trading" izni verin, "Withdraw" iznini KAPALI tutun
API_KEY = ""
API_SECRET = ""

# Telegram bildirim + komutlar (bos birakilirsa kapali). Bot sadece bu CHAT_ID'den gelen mesajlara cevap verir.
TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID = ""

# Claude: al-sat kararlarini ve sohbeti yonetir (console.anthropic.com API anahtari, kullandikca ucretli)
CLAUDE_API_KEY = ""
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "medium"      # karar dusunme derinligi: low / medium / high
CLAUDE_DAILY_BUDGET = 3.0     # gunluk ust sinir (USD); asilirsa yeni karar istenmez, pozisyonlar korunur
DECISION_COOLDOWN_MIN = 10    # olay tetikli iki karar arasi en az sure
AGENT_NOTES = ""              # admin'in Claude'a strateji talimati (or. "sadece buyuk coinler, temkinli")
TV_POLL_SEC = 60              # TradingView teknik reyting / firsat taramasi araligi
# Kota koruma: 7/24 tarama, olay ve saatlik kararlar yerel quant + hibrit ML ile (sifir kota). Claude sadece elle
# "Simdi karar ver"de, sohbette/Telegram'da ve REVIEW_HOURS saatte bir REVIEW_MODEL ile strateji incelemesinde.
QUOTA_SAVER = True
REVIEW_HOURS = 4
REVIEW_MODEL = "claude-sonnet-5-5"
LOCAL_EVERY_MIN = 15          # yerel motor olay olmasa da bu aralikla tum piyasayi degerlendirir (ucretsiz)
CLAUDE_CLI_PATH = ""          # bos = otomatik bul; doluysa elle secilen claude.exe / claude.cmd yolu

# Uzaktan guncelleme (GitHub Releases): guncelleme_yayinla.bat yayinlar, uygulama tek tikla kurar (veri korunur)
UPDATE_REPO = ""              # "kullanici/depo"
UPDATE_TOKEN = ""             # depo gizliyse salt okunur GitHub erisim anahtari (istege bagli)
AUTO_UPDATE_CHECK = True      # 6 saatte bir yeni surum kontrolu (kurulum her zaman elle onayla)
# Sunucu modu: 0.0.0.0'a baglanir; ayni agdan / Tailscale / Cloudflare tuneliyle tarayici veya telefondan erisim
SERVER_MODE = False
WHALE_ALERT_KEY = ""          # istege bagli: zincir ustu balina transferleri (whale-alert.io); yoksa borsa balinalari

# Parite evreni: Binance'teki USDT paritelerinden 24s hacmi en yuksek UNIVERSE_SIZE tanesi (haftalik yenilenir)
UNIVERSE_SIZE = 40
MIN_QUOTE_VOL = 3_000_000   # 24s USDT hacmi alt siniri (likidite); pozisyon ayrica 24s hacmin %0,2'siyle sinirli
MAX_VOL_SHARE = 0.002
# Portfoy Stratejisi (portfoy.py, 2017-2026 arastirmasi): %70 BTC/ETH trend cekirdegi + %30 haftalik ML sepeti, gunluk
# kapanista yeniden dengeleme. Gercek hesapta sadece PORTFOY_LIVE ve LIVE_ALLOWED birlikte acikken.
PORTFOY = True
PORTFOY_LIVE = False
PORTFOY_CORE = 0.7           # kasanin cekirdege ayrilan payi (kalani ML sepeti)
# Eski saatlik otomatik al-sat (yerel kurulumlar + Claude saatlik karari): 2 yillik testte ve GUCLU AL 4s/1g uzun
# tutma testinde piyasayi yenemedi -> kapali. Radar, grafik, elle islem ve sohbet calisir.
OTO_SAATLIK = False
# Canli haber akisi (haberler.py): kurallar gecmis haber verisiyle test edilemedi -> gercek hesap ayri izinle
HABER = True                 # haberleri oku + puanla
HABER_ISLEM = True           # sanal hesapta haberle islem yap
HABER_LIVE = False           # gercek hesapta haberle islem yap (LIVE_ALLOWED da gerekir)
CRYPTOPANIC_KEY = ""         # istege bagli ek haber kaynagi
GUNLUK_TREND = False         # Gunluk Trend Kolu (Donchian-20, top-30): 2022-26 testinde Sharpe 0.14 -> kapali (kod duruyor)
MAX_COST_R = 0.15           # komisyon+kayma (2*FEE+2*SLIPPAGE) riskin en fazla bu kadari: dar stoplu islem maliyete yenir
MIN_HISTORY_DAYS = 365      # yeni listelenen (pompa-dump agirlikli) coinler evrene girmez
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]  # evren alinamazsa yedek
BTC = "BTC/USDT"
TIMEFRAME = "1h"
HISTORY_DAYS = 730        # Egitim verisi (gun)
RETRAIN_DAYS = 7          # Model bu kadar gunde bir yeniden egitilir
DRIFT_MARGIN = 0.05       # Canli isabet beklenenden bu kadar dusukse erken yeniden egitim

PAPER_BALANCE = 1000.0

# Maliyet (tek yon). BNB ile komisyon odeniyorsa 0.00075
FEE = 0.001
SLIPPAGE = 0.0005         # Stop/market cikislar icin ek pay

# Islem sekli (triple barrier): model bunu ogrenir, bot birebir bunu uygular
TP_ATR = 2.0
SL_ATR = 1.0
HORIZON = 24              # Mum; hedef/stop olmazsa bu surede cikis

# Akilli cikis politikasi (Freqtrade/Jesse tarzi). Egitimde sabit cikisla yaristirilir, testte iyi olan kullanilir.
PARTIAL_ATR = 1.0         # +1 ATR'de pozisyonun yarisi satilir, stop basabasa cekilir
PARTIAL_FRAC = 0.5
TRAIL_ATR = 1.0           # kalan kisim zirveden 1 ATR geriden iz suren stopla korunur
FINAL_TP_ATR = 3.0        # kalan kisim icin son hedef
DECAY_BARS = 12           # 12 saatte ilerlemeyen (kismi kar almamis ve +0.25 ATR altinda) islemden cik
MAX_BARS = 36

# Risk yoneticisi (Claude'un emirleri bu sinirlarla denetlenir; Claude bunlari asamaz)
RISK_PCT = 1.0            # Islem basi en fazla risk: stop olunursa kaybedilecek kasa %
MAX_POS_PCT = 30.0        # Tek pozisyon ust siniri (kasa %)
MAX_POSITIONS = 5
DD_PAUSE_PCT = 5.0        # 24 saatte kasa bu kadar duserse 12 saat yeni islem yok
DAILY_LOSS_PCT = 3.0      # Gun icinde (UTC) kasa bu kadar duserse o gun yeni islem yok
COOLDOWN_MIN = 60         # Ayni coine cikistan sonra tekrar girmeden once bekleme
STOP_GUARD = (3, 6, 2)    # 6 saatte 3 stop olunursa 2 saat yeni islem yok
KILL_DD_PCT = 12.0        # Acil durdurma: kasa 7 gunluk zirvesinden bu kadar duserse yeni islem durur (elle devam)
ENTRY_TIMEOUT_MIN = 15    # Pasif alis emri bu surede dolmazsa iptal

# Sifir zarar kalkani: fiyat ilk riskin (giris - ilk stop) BE_TRIGGER_R katı kadar kara gecince stop komisyon dahil
# girisin BE_LOCK_PCT ustune cekilir. Sonra stop zirveden TRAIL_ATR geride ve kazanilan yolun LOCK_FRAC'i kilitli izler.
BE_TRIGGER_R = 0.4
BE_LOCK_PCT = 0.15        # komisyon + kayma ustune tampon: kucuk bir fiyat boslugunda da cikis zararsiz kalsin
LOCK_FRAC = 0.5
# Kartopu kasa: risk her zaman guncel kasanin yuzdesi (kar buyudukce pozisyon buyur); zirveden dususte ve
# anaparanin altinda risk otomatik kuculur. Ust sinir her durumda RISK_PCT.

# Tum piyasa radari: Binance'teki tum USDT pariteleri 2 dk'da bir taranir; bu hacmin altindakiler elenir (likidite)
RADAR_MIN_VOL = 3_000_000
EXTRA_SYMBOLS = ["PAXG/USDT"]   # her zaman izlenen ve islem yapilabilen ek pariteler (PAXG = 1 ons altin)

# Mikro-yapi giris filtresi (canli emir defteri)
OB_MIN_IMBALANCE = 0.30   # fiyatin cevresinde alici payi bunun altindaysa (satis duvari) girme
MAX_SPREAD_PCT = 0.30
TICK_SEC = 1              # canli fiyat/pozisyon guncelleme araligi (WebSocket verisiyle)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # kod (exe icinde gecici klasor olabilir)
try:  # surum: guncelleme_yayinla.bat yazar ve exe'ye gomulur
    with open(os.path.join(BASE_DIR, "surum.txt"), encoding="utf-8") as _fh:
        VERSION = _fh.read().strip() or "dev"
except OSError:
    VERSION = "dev"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "KriptoQuantBot")  # kalici veri
# Tasinabilir mod (flash bellek): exe'nin yaninda "veri" klasoru varsa her sey orada tutulur
_EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else BASE_DIR
if os.path.isdir(os.path.join(_EXE_DIR, "veri")):
    APP_DIR = os.path.join(_EXE_DIR, "veri")
APP_DIR = os.environ.get("KQB_APP_DIR") or APP_DIR  # gelistirici: ayri veri klasoruyle test
os.makedirs(APP_DIR, exist_ok=True)
DATA_DIR = os.path.join(APP_DIR, "data")
MODEL_FILE = os.path.join(APP_DIR, "model.pkl")
STATE_FILE = os.path.join(APP_DIR, "state.json")
LOG_FILE = os.path.join(APP_DIR, "bot.log")
PAUSE_FLAG = os.path.join(APP_DIR, "durdur_{}.flag")  # hesap basina (PAPER/LIVE): varsa yeni islem acilmaz
JOURNAL_FILE = os.path.join(APP_DIR, "tahmin_defteri.csv")  # oz-degerlendirme icin tahmin kaydi
LESSON_DB = os.path.join(APP_DIR, "ders_defteri.db")         # otopsiler, dersler, ogrenme ornekleri
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")      # arayuzden degistirilen ayarlar
PORT = int(os.environ.get("KQB_PORT") or 8765)
PUBLIC_HOST = {h.strip() for h in os.environ.get("KQB_PUBLIC_HOST", "").split(",") if h.strip()}  # sunucu: HTTPS adresleri
SUNUCU_URL_FILE = os.path.join(APP_DIR, "sunucu_adresi.txt")  # masaustu istemci: varsa yerel motor yerine bu sunucu acilir

# Arayuzden degistirilebilen ayarlar (settings.json bu dosyadaki degerleri ezer). Hepsini admin degistirir;
# normal kullanici sadece USER_EDITABLE alanlarini (kendi Binance API'si) gorur ve degistirir.
EDITABLE = ["LIVE_ALLOWED", "API_KEY", "API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "CLAUDE_API_KEY",
            "CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_DAILY_BUDGET", "DECISION_COOLDOWN_MIN", "AGENT_NOTES",
            "UNIVERSE_SIZE", "RISK_PCT", "MAX_POSITIONS", "MAX_POS_PCT", "DD_PAUSE_PCT", "DAILY_LOSS_PCT",
            "KILL_DD_PCT", "COOLDOWN_MIN", "FEE", "PAPER_BALANCE", "BE_TRIGGER_R", "TRAIL_ATR", "RADAR_MIN_VOL",
            "QUOTA_SAVER", "REVIEW_HOURS", "REVIEW_MODEL", "LOCAL_EVERY_MIN", "WHALE_ALERT_KEY", "CLAUDE_CLI_PATH",
            "MAX_COST_R", "PORTFOY", "PORTFOY_LIVE", "PORTFOY_CORE", "OTO_SAATLIK",
            "HABER", "HABER_ISLEM", "HABER_LIVE", "CRYPTOPANIC_KEY",
            "UPDATE_REPO", "UPDATE_TOKEN", "AUTO_UPDATE_CHECK", "SERVER_MODE"]
USER_EDITABLE = {"API_KEY", "API_SECRET"}
SECRETS = {"API_KEY", "API_SECRET", "TELEGRAM_TOKEN", "CLAUDE_API_KEY", "WHALE_ALERT_KEY", "UPDATE_TOKEN", "CRYPTOPANIC_KEY"}


def pause_flag(mode):
    return PAUSE_FLAG.format(mode)


def load_settings():
    import json
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as fh:  # elle (Not Defteri) duzenlenmis BOM'lu dosya da okunur
            saved = json.load(fh)
    except (OSError, ValueError):
        return
    globals().update({k: v for k, v in saved.items() if k in EDITABLE})
    if set(saved) - set(EDITABLE):  # eski surumden kalan alanlari (or. Groq anahtari) dosyadan temizle
        save_settings({})


def save_settings(new):
    import json
    cur = {k: globals()[k] for k in EDITABLE}
    for k, v in new.items():
        if k in EDITABLE and not (k in SECRETS and v == ""):  # bos gizli alan = degistirme
            cur[k] = v in (True, 1, "1", "true", "True", "on") if isinstance(cur[k], bool) else type(cur[k])(v)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)
    globals().update(cur)


load_settings()
