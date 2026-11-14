import logging
import os
import re
import sys

LIVE_ALLOWED = False

API_KEY = ""
API_SECRET = ""
LIVE_MAX_USDT = 100.0
LIVE_MAX_EQUITY = 0.45
LIVE_COLLAR_PCT = 3.0

TELEGRAM_TOKEN = ""
TELEGRAM_CHAT_ID = ""

CLAUDE_API_KEY = ""
CLAUDE_MODEL = "claude-opus-5-5"
CLAUDE_EFFORT = "medium"
CLAUDE_DAILY_BUDGET = 3.0
DECISION_COOLDOWN_MIN = 10
AGENT_NOTES = ""
TV_POLL_SEC = 60
QUOTA_SAVER = True
REVIEW_HOURS = 4
REVIEW_MODEL = "claude-sonnet-5-5"
LOCAL_EVERY_MIN = 15
CLAUDE_CLI_PATH = ""

UPDATE_REPO = ""
UPDATE_TOKEN = ""
AUTO_UPDATE_CHECK = True
SERVER_MODE = False
WHALE_ALERT_KEY = ""

UNIVERSE_SIZE = 40
MIN_QUOTE_VOL = 3_000_000
MAX_VOL_SHARE = 0.002
PORTFOY = True
PORTFOY_LIVE = False
EARN_USDT = False
PORTFOY_CORE = 0.7
OTO_SAATLIK = False
HABER = True
HABER_ISLEM = True
HABER_LIVE = False
CRYPTOPANIC_KEY = ""
GUNLUK_TREND = False
MAX_COST_R = 0.15
MIN_HISTORY_DAYS = 365
SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
BTC = "BTC/USDT"
TIMEFRAME = "1h"
HISTORY_DAYS = 730
RETRAIN_DAYS = 7
DRIFT_MARGIN = 0.05

PAPER_BALANCE = 1000.0

FEE = 0.001
SLIPPAGE = 0.0005

TP_ATR = 2.0
SL_ATR = 1.0
HORIZON = 24

PARTIAL_ATR = 1.0
PARTIAL_FRAC = 0.5
TRAIL_ATR = 1.0
FINAL_TP_ATR = 3.0
DECAY_BARS = 12
MAX_BARS = 36

RISK_PCT = 1.0
MAX_POS_PCT = 30.0
MAX_POSITIONS = 5
GELISTIRME_MODEL = "claude-opus-5-5"
GELISTIRME_GUNLUK_USD = 5.0
GELISTIRME_IS_USD = 2.0
MAX_OPEN_RISK_PCT = 5.0
PX_STALE_SEC = 30
DD_PAUSE_PCT = 5.0
DAILY_LOSS_PCT = 3.0
COOLDOWN_MIN = 60
STOP_GUARD = (3, 6, 2)
KILL_DD_PCT = 12.0
ENTRY_TIMEOUT_MIN = 15

BE_TRIGGER_R = 0.4
BE_LOCK_PCT = 0.15
LOCK_FRAC = 0.5

RADAR_MIN_VOL = 3_000_000
EXTRA_SYMBOLS = ["PAXG/USDT"]

OB_MIN_IMBALANCE = 0.30
MAX_SPREAD_PCT = 0.30
TICK_SEC = 1

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(BASE_DIR, "surum.txt"), encoding="utf-8") as _fh:
        VERSION = _fh.read().strip() or "dev"
except OSError:
    VERSION = "dev"
APP_DIR = os.path.join(os.environ.get("LOCALAPPDATA", BASE_DIR), "KriptoQuantBot")
_EXE_DIR = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) else BASE_DIR
if os.path.isdir(os.path.join(_EXE_DIR, "veri")):
    APP_DIR = os.path.join(_EXE_DIR, "veri")
APP_DIR = os.environ.get("KQB_APP_DIR") or APP_DIR
os.makedirs(APP_DIR, exist_ok=True)
DATA_DIR = os.path.join(APP_DIR, "data")
MODEL_FILE = os.path.join(APP_DIR, "model.pkl")
STATE_FILE = os.path.join(APP_DIR, "state.json")
LOG_FILE = os.path.join(APP_DIR, "bot.log")
PAUSE_FLAG = os.path.join(APP_DIR, "durdur_{}.flag")
JOURNAL_FILE = os.path.join(APP_DIR, "tahmin_defteri.csv")
LESSON_DB = os.path.join(APP_DIR, "ders_defteri.db")
REBAL_DIR = os.path.join(APP_DIR, "dengeleme_defteri")
SETTINGS_FILE = os.path.join(APP_DIR, "settings.json")
PORT = int(os.environ.get("KQB_PORT") or 8765)
PUBLIC_HOST = {h.strip() for h in os.environ.get("KQB_PUBLIC_HOST", "").split(",") if h.strip()}
SUNUCU_URL_FILE = os.path.join(APP_DIR, "sunucu_adresi.txt")

EDITABLE = ["LIVE_ALLOWED", "LIVE_MAX_USDT", "API_KEY", "API_SECRET", "TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID", "CLAUDE_API_KEY",
            "CLAUDE_MODEL", "CLAUDE_EFFORT", "CLAUDE_DAILY_BUDGET", "DECISION_COOLDOWN_MIN", "AGENT_NOTES",
            "UNIVERSE_SIZE", "RISK_PCT", "MAX_POSITIONS", "MAX_POS_PCT", "DD_PAUSE_PCT", "DAILY_LOSS_PCT",
            "KILL_DD_PCT", "COOLDOWN_MIN", "FEE", "PAPER_BALANCE", "BE_TRIGGER_R", "TRAIL_ATR", "RADAR_MIN_VOL",
            "QUOTA_SAVER", "REVIEW_HOURS", "REVIEW_MODEL", "LOCAL_EVERY_MIN", "WHALE_ALERT_KEY", "CLAUDE_CLI_PATH",
            "MAX_COST_R", "PORTFOY", "PORTFOY_LIVE", "EARN_USDT", "PORTFOY_CORE", "OTO_SAATLIK",
            "HABER", "HABER_ISLEM", "HABER_LIVE", "CRYPTOPANIC_KEY",
            "UPDATE_REPO", "UPDATE_TOKEN", "AUTO_UPDATE_CHECK", "SERVER_MODE"]
USER_EDITABLE = {"API_KEY", "API_SECRET"}
SECRETS = {"API_KEY", "API_SECRET", "TELEGRAM_TOKEN", "CLAUDE_API_KEY", "WHALE_ALERT_KEY", "UPDATE_TOKEN", "CRYPTOPANIC_KEY"}


def pause_flag(mode):
    return PAUSE_FLAG.format(mode)


def load_settings():
    import json
    try:
        with open(SETTINGS_FILE, encoding="utf-8-sig") as fh:
            saved = json.load(fh)
    except (OSError, ValueError):
        return
    globals().update({k: v for k, v in saved.items() if k in EDITABLE})
    if set(saved) - set(EDITABLE):
        save_settings({})


def save_settings(new):
    import json
    cur = {k: globals()[k] for k in EDITABLE}
    for k, v in new.items():
        if k in EDITABLE and not (k in SECRETS and v == ""):
            cur[k] = v in (True, 1, "1", "true", "True", "on") if isinstance(cur[k], bool) else type(cur[k])(v)
    tmp = SETTINGS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(cur, fh, indent=1, ensure_ascii=False)
    os.replace(tmp, SETTINGS_FILE)
    globals().update(cur)


load_settings()


class Maske(logging.Filter):
    RE = re.compile(r"(signature=)[0-9a-fA-F]+|(/bot)\d+:[\w-]+|(sk-ant-)[\w-]+|(X-MBX-APIKEY['\"]?:\s*['\"]?)[\w]+")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        sub = lambda t: self.RE.sub(lambda x: (x.group(1) or x.group(2) or x.group(3) or x.group(4)) + "***", t)
        m = sub(msg)
        if m != msg:
            record.msg, record.args = m, None
        if record.exc_info and not record.exc_text:
            record.exc_text = sub(logging.Formatter().formatException(record.exc_info))
        return True
