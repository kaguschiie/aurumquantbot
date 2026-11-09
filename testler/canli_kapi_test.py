"""Gercek emir kapisi: tutar / kasa / fiyat bandi / parite durumu, cekim izni okuma, hesap kilidi. Tamamen sahte borsa: ag yok, anahtar yok,
gercek emir yok. Kullanim: KQB_APP_DIR=<test> python -I canli-kapi_test.py <proje>"""
import os
import re
import shutil
import subprocess
import sys
import threading
import time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
import ccxt
import config as C
import bot as B
import dersler as L
import portfoy as PF


def _no_net(*a, **k):
    raise AssertionError("testte gercek baglanti yok")


ccxt.binance = _no_net  # gercek borsa nesnesi kurulamaz
ok_all = True
EV = os.path.join(C.APP_DIR, "ev")  # hesap sahibi kaydi kullanicinin gercek ev klasorune yazilmasin
shutil.rmtree(EV, ignore_errors=True)
os.makedirs(EV)
os.environ["HOME"] = os.environ["USERPROFILE"] = EV


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


class FakeEx:
    def __init__(self, last=100.0, withdraw=False):
        self.last, self.withdraw, self.status = last, withdraw, {}
        self.orders, self.cancels, self.n, self.tickers, self.key_reads = [], [], 0, 0, 0
        self.markets = {"BTC/USDT": {"info": {"status": "TRADING"}}, "ETH/USDT": {"info": {"status": "TRADING"}}}
        self.key_raises, self.ticker_raises, self.info_raises, self.order_raises, self.fo = None, None, None, {}, {"status": "open", "filled": 0, "average": None}

    def create_order(self, s, typ, side, qty, price=None, params=None):
        assert isinstance(params, dict), "ccxt params sozluk olmali"
        if (s, typ) in self.order_raises:
            raise self.order_raises[(s, typ)]
        self.n += 1
        self.orders.append({"s": s, "typ": typ, "side": side, "qty": qty, "price": price, "params": params, "id": str(self.n)})
        return {"id": str(self.n), "filled": qty, "average": price or self.last}

    def cancel_order(self, oid, s):
        self.cancels.append(oid)
        return {"status": "canceled", "filled": 0}

    def fetch_order(self, oid, s):
        return self.fo

    def fetch_balance(self):
        return {"USDT": {"free": 1000.0, "total": 1000.0}, "BTC": {"free": 1e6, "total": 1e6}, "ETH": {"free": 1e6, "total": 1e6}}

    def fetch_ticker(self, s):
        self.tickers += 1
        if self.ticker_raises:
            raise self.ticker_raises
        return {"last": self.last}

    def public_get_exchangeinfo(self, p):
        if self.info_raises:
            raise self.info_raises
        sym = p["symbol"].replace("USDT", "/USDT")
        return {"symbols": [{"status": self.status.get(sym, "TRADING")}]}

    def sapi_get_account_apirestrictions(self):
        self.key_reads += 1
        if self.key_raises:
            raise self.key_raises
        return {"enableWithdrawals": self.withdraw, "enableSpotAndMarginTrading": True, "ipRestrict": False}

    def price_to_precision(self, s, p):
        return f"{p:.2f}"

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"

    def market(self, s):
        return {"limits": {"cost": {"min": 5}}}


class Stub:
    """Motorun Account'a gosterdigi kismi."""

    def __init__(self, ex):
        self.ex, self.bid, self.ask = ex, {"BTC/USDT": 100.0, "ETH/USDT": 100.0}, {"BTC/USDT": 100.0, "ETH/USDT": 100.0}
        self.market, self.lock, self.stop, self.accounts, self.notes = {}, threading.RLock(), threading.Event(), {}, []

    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        self.notes.append((kind, msg))

    def save(self):
        pass


KEY = "sahte-test-anahtari"
C.API_KEY, C.API_SECRET, C.TELEGRAM_TOKEN = KEY, "sahte", ""  # sadece bu surecin bellegi; dosyaya yazilmaz
C.LIVE_ALLOWED = True  # sahte borsa icin; kaydedilmez
C.LIVE_MAX_USDT = 100.0
L.init()


def live(ex=None):
    ex = ex or FakeEx()
    B.D.exchange = lambda auth=False: ex
    a = B.Account(Stub(ex), "LIVE", {})
    a.b.accounts["LIVE"] = a
    a.sync()
    return a, ex


def pos(qty, entry=100.0, sl=65.0, stop_id="st1"):
    return {"qty": qty, "qty0": qty, "entry": entry, "atr": 1.0, "sl": sl, "sl0": sl, "tp": entry * 1000, "part": False, "hi": entry,
            "fee": 0.0, "realized": 0.0, "t": time.time(), "stop_id": stop_id, "stop_px": sl, "partial_at": None, "kind": PF.KIND,
            "manual": False, "max_hours": 1e6, "exit": {}, "seen": time.time()}


def plan(usdt):
    return {"type": "buy", "symbol": "BTC/USDT", "kind": PF.KIND, "entry": "taker", "usdt": usdt, "stop": 65.0, "tp2": 1e5, "tp1": 0,
            "risk_pct": 0, "max_hours": 1e6, "reason": "test", "conviction": None}


# ---- yapi: tek kapi, ayar ----
src = open(os.path.join(PROJ, "bot.py"), encoding="utf-8").read()
others = [f for f in os.listdir(PROJ) if f.endswith(".py") and f != "bot.py" and "create_order(" in open(os.path.join(PROJ, f), encoding="utf-8", errors="ignore").read()]
n_co = len(re.findall(r"\.create_order\(", src))
check("create_order bot.py'de tek yerde (Account.order), baska dosyada yok", n_co == 1 and not others, (n_co, others))
cfg = open(os.path.join(PROJ, "config.py"), encoding="utf-8").read()
dflt = float(re.search(r"^LIVE_MAX_USDT = ([\d.]+)", cfg, re.M).group(1))
check("varsayilan sinir kucuk, varsayilan LIVE kapali", 0 < dflt <= 100 and re.search(r"^LIVE_ALLOWED = False", cfg, re.M) and re.search(r"^PORTFOY_LIVE = False", cfg, re.M), dflt)
check("sinir sadece admin ayari", "LIVE_MAX_USDT" in C.EDITABLE and "LIVE_MAX_USDT" not in C.USER_EDITABLE and "LIVE_MAX_USDT" not in C.SECRETS)
ui = open(os.path.join(PROJ, "ui.html"), encoding="utf-8").read()
check("arayuzde sadece admin grubu 'live' icinde", re.search(r'live: \[.*?\n.*?"LIVE_MAX_USDT"', ui) and "LIVE_MAX_USDT" not in ui.split("ubinance:")[1].split("\n")[0])
_sf, C.SETTINGS_FILE = C.SETTINGS_FILE, os.path.join(C.APP_DIR, "kapi_ayar_test.json")
_k = {k: getattr(C, k) for k in C.EDITABLE}
C.save_settings({"LIVE_MAX_USDT": "250"})
check("ayar kaydedilir, sayi olur", C.LIVE_MAX_USDT == 250.0, C.LIVE_MAX_USDT)
os.remove(C.SETTINGS_FILE)
C.SETTINGS_FILE = _sf
C.LIVE_MAX_USDT = 100.0
try:
    import app as APP
    r = APP.save_settings({"role": "user"}, {"LIVE_MAX_USDT": 1e9})
    check("normal kullanici siniri degistiremez", not r["ok"] and C.LIVE_MAX_USDT == 100.0, r)
except ImportError as e:
    check("app yuklenemedi (normal kullanici testi yapilamadi)", False, e)
import ccxt as _c
_e = _c.Exchange.__subclasses__ and __import__("ccxt.binance", fromlist=["binance"]).binance()
sapi = _e.describe()["api"]["sapi"]["get"]
check("ccxt anahtar izni ucu tanimli ve metot var", "account/apiRestrictions" in sapi and hasattr(_e, "sapi_get_account_apirestrictions") and hasattr(_e, "public_get_exchangeinfo"))

# ---- tutar, kasa, fiyat, parite ----
a, ex = live()
check("temiz anahtar: hata yok, alim acik", a.error == "" and a.blocked() is None and a.key_ok is True, a.blocked())
check("sinir ici alim gecer + borsa stopu kurulur", a.enter("BTC/USDT", plan(50)) is None and [o["side"] for o in ex.orders] == ["buy", "sell"]
      and ex.orders[1]["typ"] == "STOP_LOSS_LIMIT" and "BTC/USDT" in a.st["positions"], ex.orders)
a, ex = live()
r = a.enter("BTC/USDT", plan(150))
check("LIVE_MAX_USDT ustu alim reddedilir", r and "sınırı" in r and not ex.orders and not a.st["positions"], r)
check("kullaniciya uyari + saatte bir", sum("gönderilmedi" in m for _, m in a.b.notes) == 1 and a.enter("BTC/USDT", plan(150)) and sum("gönderilmedi" in m for _, m in a.b.notes) == 1, a.b.notes)
a, ex = live()
C.LIVE_MAX_USDT = 1e6
r = a.enter("BTC/USDT", plan(460))
check("kasanin %45'i ustu alim reddedilir", r and "%45" in r and not ex.orders, r)
check("kasanin %45'i alti gecer", a.enter("BTC/USDT", plan(440)) is None and ex.orders[0]["side"] == "buy", ex.orders[:1])
for bad in (float("nan"), float("inf")):
    C.LIVE_MAX_USDT = bad
    a, ex = live()
    r = a.enter("BTC/USDT", plan(10))
    check(f"bozuk sinir ayari ({bad}) reddeder", r and not ex.orders, r)
C.LIVE_MAX_USDT = 0.0
a, ex = live()
check("sinir 0: alim yok", a.enter("BTC/USDT", plan(10)) and not ex.orders)
C.LIVE_MAX_USDT = 100.0

a, ex = live()
ex.last = 90.0  # akisin fiyati (100) son islem fiyatindan %11 uzak
r = a.enter("BTC/USDT", plan(50))
check("piyasa alimi: fiyat bandi disi reddedilir", r and "uzak" in r and not ex.orders, r)
ex.last = 98.0
check("piyasa alimi: bant icinde gecer", a.enter("BTC/USDT", plan(50)) is None and ex.orders, ex.last)
a, ex = live()
lim = dict(plan(50), entry="maker")
a.b.bid["BTC/USDT"] = 130.0
r = a.enter("BTC/USDT", lim)
check("limit alim: bant disi fiyat reddedilir", r and "uzak" in r and not ex.orders and not a.st["pending"], r)
a.b.bid["BTC/USDT"] = 100.0
check("limit alim: bant icinde gecer (postOnly korunur)", a.enter("BTC/USDT", lim) is None and ex.orders[0]["params"].get("postOnly") is True and "BTC/USDT" in a.st["pending"], ex.orders)
a, ex = live()
ex.ticker_raises = ccxt.NetworkError("yok")
r = a.enter("BTC/USDT", plan(50))
check("son fiyat okunamazsa alim yok", r and "okunamadı" in r and not ex.orders, r)
a, ex = live()
ex.status["BTC/USDT"] = "BREAK"
r = a.enter("BTC/USDT", plan(50))
check("TRADING olmayan parite reddedilir", r and "BREAK" in r and not ex.orders, r)
a, ex = live()
ex.info_raises, ex.markets = ccxt.NetworkError("yok"), {"BTC/USDT": {"info": {"status": "HALT"}}}
r = a.enter("BTC/USDT", plan(50))
check("taze durum okunamazsa yuklu listedeki durum kullanilir", r and "HALT" in r and not ex.orders, r)
ex.markets = {}
r = a.enter("BTC/USDT", plan(50))
check("durum hic bilinmiyorsa alim yok", r and "bilinmiyor" in r and not ex.orders, r)
a, ex = live()
C.LIVE_ALLOWED = False
r = a.enter("BTC/USDT", plan(50))
check("izin yokken kapi da alimi durdurur", r and "admin" in r and not ex.orders, r)
C.LIVE_ALLOWED = True

# ---- cikis ve stop kapida kalmaz ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = pos(50.0)  # 5000 USDT: sinirin ve kasanin %40'inin cok ustunde
a.exit("BTC/USDT", a.st["positions"]["BTC/USDT"], "PORTFOY_CIKIS")
check("buyuk pozisyondan cikis gider (sinira takilmaz)", ex.orders and ex.orders[0]["side"] == "sell" and ex.orders[0]["qty"] == 50.0 and "BTC/USDT" not in a.st["positions"], ex.orders)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(50.0, stop_id=None)
p["stop_id"] = a.place_stop("BTC/USDT", p)
check("buyuk pozisyonun %35 felaket stopu kurulur", p["stop_id"] and ex.orders[-1]["typ"] == "STOP_LOSS_LIMIT" and ex.orders[-1]["params"]["stopPrice"] == 65.0, ex.orders)
a, ex = live()
a.b.bid["BTC/USDT"] = 50.0
ex.last = 50.0
a.st["positions"]["BTC/USDT"] = p = pos(50.0)
a.exit("BTC/USDT", p, "STOP", stop=True)
check("sert dususte (fiyat bandi disinda) piyasa satisi engellenmez", ex.orders and ex.orders[0]["typ"] == "market" and "BTC/USDT" not in a.st["positions"], ex.orders)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
try:
    a.order("BTC/USDT", "market", "sell", 50.0)
    check("pozisyondan fazla satis (sinir ustu) reddedilir", False)
except B.FenceDeny as e:
    check("pozisyondan fazla satis (sinir ustu) reddedilir", not ex.orders, e)
try:
    a.order("ETH/USDT", "market", "sell", 50.0)
    check("pozisyonu olmayan coinde sinir ustu satis reddedilir", False)
except B.FenceDeny as e:
    check("pozisyonu olmayan coinde sinir ustu satis reddedilir", not ex.orders, e)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
p["sl"] = 150.0
a.b.notes.clear()
check("anlik fiyatin cok ustunde stop kurulmaz", a.place_stop("BTC/USDT", p, quiet=True) is None and not ex.orders)
try:
    a._replace_stop("BTC/USDT", p)
    check("reddedilecek yeni stop icin eski stop iptal edilmez", False)
except B.FenceDeny:
    check("reddedilecek yeni stop icin eski stop iptal edilmez", not ex.cancels and p["stop_id"] == "st1", ex.cancels)
a, ex = live()
ex.status["BTC/USDT"] = "BREAK"
a._status("BTC/USDT")  # BREAK onbellekte, parite acildi: satis bayat duruma takilmaz
ex.status["BTC/USDT"] = "TRADING"
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
a.exit("BTC/USDT", p, "STOP", stop=True)
check("bayat BREAK durumu pozisyon cikisini engellemez", ex.orders and ex.orders[0]["side"] == "sell" and "BTC/USDT" not in a.st["positions"], ex.orders)
a, ex = live()
ex.status["BTC/USDT"] = "BREAK"
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
p["stop_id"] = a.place_stop("BTC/USDT", p, quiet=True)
check("BREAK durumunda stop kapida kalmaz", p["stop_id"] and ex.orders[-1]["typ"] == "STOP_LOSS_LIMIT", ex.orders)
a, ex = live()
ex.status["BTC/USDT"] = "BREAK"
a.st["positions"]["BTC/USDT"] = p = pos(0.01, stop_id=None)  # 1 USDT: satilamayacak kadar az, kapali paritede
a.exit("BTC/USDT", p, "HEDEF")
check("kapali paritedeki toz defterden duser", "BTC/USDT" not in a.st["positions"] and not ex.orders, ex.orders)
a, ex = live()
ex.status["BTC/USDT"] = "BREAK"
try:
    a.order("BTC/USDT", "market", "sell", 1.0)
    check("pozisyonu olmayan satis TRADING disinda hala reddedilir", False)
except B.FenceDeny as e:
    check("pozisyonu olmayan satis TRADING disinda hala reddedilir", "BREAK" in str(e) and not ex.orders, e)
a, ex = live()
a.st["positions"]["BTC/USDT"] = pos(1.0)
for bad in (0.0, -1.0, float("nan")):
    try:
        a.order("BTC/USDT", "market", "sell", bad)
        check(f"gecersiz miktar reddedilir ({bad})", False)
    except B.FenceDeny as e:
        check(f"gecersiz miktar reddedilir ({bad})", "geçersiz" in str(e) and not ex.orders, e)
try:
    a.order("BTC/USDT", "limit", "sell", 1.0, 150.0)
    check("duz limit satis bant disinda reddedilir", False)
except B.FenceDeny as e:
    check("duz limit satis bant disinda reddedilir", "uzak" in str(e) and not ex.orders, e)
a, ex = live()
a.b.bid.update({"BTC/USDT": 60.0, "ETH/USDT": 60.0})  # ikisi de stop altinda, borsa stopu yok
a.st["positions"]["BTC/USDT"] = pos(1.0, stop_id=None)
a.st["positions"]["ETH/USDT"] = pos(1.0, stop_id=None)
ex.order_raises[("BTC/USDT", "market")] = ccxt.ExchangeError("red")
a.manage_positions()
check("manage_positions: bir coinin hatasi digerinin cikisini durdurmaz", "BTC/USDT" in a.st["positions"] and "ETH/USDT" not in a.st["positions"], ex.orders)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0)
p["partial_at"] = 100.0
a.partial("BTC/USDT", p, 100.0)
check("kademeli satis: sat + stopu yenile", ex.cancels == ["st1"] and ex.orders[0]["typ"] == "market" and ex.orders[0]["qty"] == 10.0 * C.PARTIAL_FRAC and ex.orders[-1]["typ"] == "STOP_LOSS_LIMIT" and p["part"], ex.orders)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0)
ex.cancel_order = lambda oid, s: {"status": "canceled", "filled": 9.0, "average": 100.0}  # stop iptal aninda %90 dolmus
a.partial("BTC/USDT", p, 100.0)
check("kademeli satis: stop kismen dolduysa kalan kadar satilir, kapida takilmaz", ex.orders and ex.orders[0]["side"] == "sell" and ex.orders[0]["typ"] == "market" and ex.orders[0]["qty"] <= 1.0, ex.orders[:1])
a, ex = live()
del ex.markets
ex.info_raises = ccxt.NetworkError("yok")
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
a.exit("BTC/USDT", p, "HEDEF")
check("durum okunamasa da pozisyon cikisi gider", ex.orders and ex.orders[0]["side"] == "sell", ex.orders)

a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
p["qty"] = 0.0  # stop parca parca tamamen dolmus
a.exit("BTC/USDT", p, "HEDEF")
check("miktari sifira inmis pozisyon defterden duser (kapida takilmaz)", "BTC/USDT" not in a.st["positions"] and not ex.orders)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0)
ex.cancel_order = lambda oid, s: {"status": "canceled", "filled": 9.0, "average": 100.0}  # stop iptal aninda %90 dolmus
r = a.pf_sell("BTC/USDT", 500.0, "test")
check("portfoy azaltma: stop kismen dolduysa kalan kadar satilir, kapida takilmaz", r is None and ex.orders and ex.orders[0]["side"] == "sell" and ex.orders[0]["qty"] <= 1.0, (r, ex.orders[:1]))

# ---- portfoy akisi ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(0.5)
r = a.pf_buy("BTC/USDT", 300, "test")
check("portfoy artirma reddi: felaket stopu iptal edilmez", r and "sınırı" in r and not ex.cancels and p["stop_id"] == "st1" and not ex.orders, r)
r = a.pf_buy("BTC/USDT", 50, "test")
check("portfoy artirma: gecer, stop yenilenir", r is None and ex.cancels == ["st1"] and [o["side"] for o in ex.orders] == ["buy", "sell"], r)
a, ex = live()
ex.order_raises[("BTC/USDT", "market")] = ccxt.InvalidOrder("Market is closed")
a.st["positions"]["BTC/USDT"] = pos(1.0)
a.st["positions"]["ETH/USDT"] = pos(1.0, stop_id="st2")
out = a.rebalance({}, False)
check("dengeleme: borsanin reddettigi coin digerini durdurmaz", "BTC/USDT" in a.st["positions"] and "ETH/USDT" not in a.st["positions"] and [o["s"] for o in ex.orders if o["typ"] == "market"] == ["ETH/USDT"], out)
a, ex = live()
for s_ in ("BTC/USDT", "ETH/USDT"):
    a.st["positions"][s_] = pos(0.5, stop_id="st_" + s_[:3])
_t = ex.fetch_ticker
_n = [0]
def _tk(s_):  # ilk kapi gecer, ikinci kapida fiyat okunamaz (stop iptal edildikten sonra)
    _n[0] += 1
    if _n[0] == 2:
        raise ccxt.RequestTimeout("zaman asimi")
    return _t(s_)
ex.fetch_ticker = _tk
out = a.rebalance({"BTC/USDT": 0.12, "ETH/USDT": 0.12}, False)
pb, pe = a.st["positions"]["BTC/USDT"], a.st["positions"]["ETH/USDT"]
buys = [o for o in ex.orders if o["side"] == "buy"]
stops = [o for o in ex.orders if o["typ"] == "STOP_LOSS_LIMIT" and o["s"] == "BTC/USDT"]
check("pf_buy: ikinci kapi reddederse iptal edilen stop hemen geri kurulur", ex.cancels[0] == "st_BTC" and pb["stop_id"] and stops and not [o for o in buys if o["s"] == "BTC/USDT"], (out, ex.cancels))
check("dengeleme: bir coinin alim reddi digerinin alimini durdurmaz", [o["s"] for o in buys] == ["ETH/USDT"] and pe["qty"] > 0.5, out)
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(0.5)
ex.order_raises[("BTC/USDT", "market")] = ccxt.InsufficientFunds("bakiye")
r = a.pf_buy("BTC/USDT", 50, "test")
check("pf_buy: borsa alimi reddederse de stop geri kurulur", r and p["stop_id"] and p["stop_id"] != "st1" and ex.orders[-1]["typ"] == "STOP_LOSS_LIMIT" and p["qty"] == 0.5, r)
a, ex = live()
a.st["positions"]["BTC/USDT"] = pos(0.5)
a.st["positions"]["ETH/USDT"] = pos(0.5, stop_id="st2")
_c = ex.cancel_order
def _ci(oid, s_):
    if s_ == "BTC/USDT":
        raise ccxt.ExchangeError("iptal olmadi")
    return _c(oid, s_)
ex.cancel_order = _ci
out = a.rebalance({"BTC/USDT": 0.12, "ETH/USDT": 0.12}, False)
check("dengeleme: stop iptal hatasi alim dongusunu kirmaz", [o["s"] for o in ex.orders if o["side"] == "buy"] == ["ETH/USDT"] and a.st["positions"]["BTC/USDT"]["stop_id"] == "st1", out)

# ---- anahtar izinleri ----
a, ex = live(FakeEx(withdraw=True))
check("cekim izni acik: yeni alim yok, sebep gorunur", a.key_ok is False and "çekme izni" in (a.blocked() or "") and "çekme izni" in a.error, a.blocked())
check("cekim izni: uyari gonderildi", any("çekme izni" in m for k, m in a.b.notes if k == "uyari"))
C.LIVE_MAX_USDT = 1e6
r = a.enter("BTC/USDT", plan(50))
check("cekim izni acik: kapi da alimi reddeder", r and "çekme izni" in r and not ex.orders, r)
C.LIVE_MAX_USDT = 100.0
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
a.exit("BTC/USDT", p, "HEDEF")
check("cekim izni acik: mevcut pozisyon korunur / cikar", ex.orders and ex.orders[0]["side"] == "sell", ex.orders)
ex.orders.clear()
ex.withdraw, a.key_t = False, 0
a.sync()
check("izin kapatilinca alim acilir", a.key_ok and a.error == "" and a.enter("BTC/USDT", plan(50)) is None and ex.orders[0]["side"] == "buy", a.error)
n = ex.key_reads
a.sync()
check("izin her bakiye okumasinda degil, seyrek okunur", ex.key_reads == n, ex.key_reads)
a.key_t, ex.withdraw = 0, True
a.sync()
check("izin sonradan acilirsa saatlik okumada yakalanir", a.key_ok is False and a.blocked(), a.blocked())
ex.withdraw, ex.key_raises, a.key_t = False, ccxt.NetworkError("yok"), 0
a.sync()
check("gecici ag hatasi kotu sonucu temizlemez", a.key_ok is False and a.blocked(), a.key_ok)
ex.key_raises, a.key_t = ccxt.ExchangeError("x"), 0
a.sync()
check("uc hatasi kotu sonucu temizlemez", a.key_ok is False and a.blocked(), a.key_ok)

ex = FakeEx()
ex.key_raises = ccxt.NetworkError("yok")
a, ex = live(ex)
check("ilk okuma ag hatasi: bilinmeyen izinle alim yok", a.key_ok is None and "okunamadı" in (a.blocked() or "") and a.enter("BTC/USDT", plan(50)) and not ex.orders, a.blocked())
ex.key_raises, a.key_t = None, 0
a.sync()
check("ag duzelince acilir", a.key_ok is True and a.blocked() is None, a.blocked())
ex = FakeEx()
ex.key_raises = ccxt.AuthenticationError("-2015")
a, ex = live(ex)
check("anahtar reddi: alim yok", a.key_ok is False and a.blocked(), a.blocked())
ex = FakeEx()
ex.sapi_get_account_apirestrictions = None
a, ex = live(ex)
check("uc yok: uyari + devam", a.key_ok is True and a.blocked() is None and any("okunamıyor" in m for _, m in a.b.notes), a.b.notes)
ex = FakeEx()
ex.key_raises = ccxt.BadRequest("yok")
a, ex = live(ex)
check("uc hata veriyor: uyari + devam", a.key_ok is True and a.key_na and any("okunamıyor" in m for _, m in a.b.notes))
ex.key_raises, ex.withdraw, a.key_t = None, True, time.time() - 61
a.sync()
check("uc okunamiyorken dakikada bir denenir: cekim izni acik yakalanir", a.key_ok is False and a.key_na is False and a.blocked(), a.key_ok)
for err in (ccxt.BadResponse("bozuk"), ccxt.NullResponse("bos"), ccxt.ExchangeError("-1000"), KeyError("enableWithdrawals")):
    ex = FakeEx(withdraw=True)
    ex.key_raises = err
    a, ex = live(ex)
    r = a.enter("BTC/USDT", plan(50))
    check(f"ilk okuma gecici hata ({type(err).__name__}): izin bilinmiyor, alim yok", a.key_ok is None and "okunamadı" in (a.blocked() or "") and r and not ex.orders, a.blocked())
    ex.key_raises, a.key_t = None, time.time() - 61
    a.sync()
    check(f"gecici hata gecince gercek izin okunur ({type(err).__name__})", a.key_ok is False and "çekme izni" in a.error, a.error)
ex = FakeEx()
ex.key_raises = ccxt.NetworkError("yok")
a, ex = live(ex)
n, a.key_t = ex.key_reads, time.time() - 30
a.sync()
check("izin bilinmezken 30 sn'de yeniden okunmaz", ex.key_reads == n, ex.key_reads)
a.key_t = time.time() - 61
a.sync()
check("izin bilinmezken dakikada bir yeniden okunur", ex.key_reads == n + 1, ex.key_reads)
ex.key_raises, a.key_t = None, time.time() - 61
a.sync()
n, a.key_t = ex.key_reads, time.time() - 61
a.sync()
check("izin temizken dakikada bir okunmaz", a.key_ok is True and ex.key_reads == n, ex.key_reads)
a.key_t = time.time() - 3601
a.sync()
check("izin temizken saatte bir okunur", ex.key_reads == n + 1, ex.key_reads)
ex = FakeEx(withdraw="evet")
a, ex = live(ex)
check("anlasilmayan izin degeri acik sayilir", a.key_ok is False, a.key_err)

# ---- hesap kilidi: gercek ikinci surec ----
HOLD = """import ast, errno, hashlib, logging, os, sys, tempfile, time
sys.path.insert(0, sys.argv[1])
import config as C
log, _LOCKS = logging.getLogger("x"), {}
tree = ast.parse(open(os.path.join(sys.argv[1], "bot.py"), encoding="utf-8").read())  # gercek kod, sadece kilit fonksiyonlari (bot.py'nin tamami 16 sn'de yuklenir)
exec(compile(ast.Module([n for n in tree.body if getattr(n, "name", "") in ("_flock", "hold_lock")], []), "bot.py", "exec"))
print("YOK" if hold_lock(sys.argv[2]) else "KILIT", flush=True)
time.sleep(120)
"""


def child(key, appdir=None):
    env = dict(os.environ)
    if appdir:
        env["KQB_APP_DIR"] = appdir
    p = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-c", HOLD, PROJ, key], stdout=subprocess.PIPE, text=True, env=env)
    return p, p.stdout.readline().strip()


other = os.path.join(C.APP_DIR, "ikinci_kopya")
os.makedirs(other, exist_ok=True)
check("ayni surec ayni kilidi yeniden alir (motor yeniden baslatma)", B.hold_lock(KEY) is None and B.hold_lock(KEY) is None)
p, got = child(KEY)
check("kilit bizdeyken ikinci surec (ayni veri klasoru) alamaz", got == "YOK", got)
p.kill(); p.wait()
p, got = child(KEY, other)
check("kilit bizdeyken ikinci surec (ayri veri klasoru) alamaz", got == "YOK", got)
p.kill(); p.wait()
p, got = child(KEY + "-baska")
check("baska hesabin anahtari etkilenmez", got == "KILIT", got)
p.kill(); p.wait()

K2 = KEY + "-2"
p, got = child(K2)
C.API_KEY = K2
a, ex = live()
check("kilit baskasindaysa hesap pasif: alim yok, sebep gorunur", got == "KILIT" and a.own is False and "başka bir motor" in (a.blocked() or "") and any("başka bir motor" in m for k, m in a.b.notes if k == "uyari"), a.blocked())
C.LIVE_MAX_USDT = 1e6
r = a.enter("BTC/USDT", plan(50))
check("kilit yok: alim reddedilir", r and "başka bir motor" in r and not ex.orders, r)
C.LIVE_MAX_USDT = 100.0
a.st["positions"]["BTC/USDT"] = p2 = pos(1.0)
a.b.bid["BTC/USDT"] = 60.0  # stop altinda
a.manage_positions()
check("kilit yok: saniyelik yonetim stopa ve pozisyona dokunmaz", not ex.orders and not ex.cancels and "BTC/USDT" in a.st["positions"])
a.st["pending"]["BTC/USDT"] = {"id": "e1", "t": 0, "qty": 1, "price": 100}
a.manage_pending()
check("kilit yok: bekleyen emre dokunmaz", not ex.cancels and a.st["pending"])
for name, fn in (("satis", lambda: a.exit("BTC/USDT", p2, "HEDEF")), ("stop iptali", lambda: a._cancel_stop("BTC/USDT", p2)),
                 ("emir iptali", lambda: a.finish_pending("BTC/USDT", a.st["pending"]["BTC/USDT"], True)),
                 ("elle kapat", lambda: a.close("BTC/USDT", "ELLE")), ("dogrudan emir", lambda: a.order("BTC/USDT", "market", "sell", 1.0))):
    try:
        fn()
        check(f"kilit yok: {name} reddedilir", False)
    except ccxt.BaseError as e:
        check(f"kilit yok: {name} reddedilir", not ex.orders and not ex.cancels, e)
check("kilit yok: stop kurulmaz", a.place_stop("BTC/USDT", p2, quiet=True) is None and not ex.orders)
out = a.rebalance({}, False)
check("kilit yok: dengeleme satis yapmaz", not ex.orders and not ex.cancels, out)
a.st["pending"].clear()
_fp, a.finish_pending = a.finish_pending, lambda *x, **k: check("kilit yok: manage_pending emre hic bakmaz", False)
a.st["pending"]["BTC/USDT"] = {"id": "e1", "t": 0, "qty": 1, "price": 100}
a.manage_pending()
a.finish_pending = _fp
a.st["pending"].clear()
ex.fo = {"status": "closed", "filled": 1.0, "average": 65.0}  # borsada stop dolmus gorunuyor (hesap baska motorun)
a.b.bid["BTC/USDT"] = 100.0
p2["stop_chk"] = 0  # 15 sn kontrol araligi engellemesin
a.manage_positions()
check("kilit yok: baska motorun dolumunu defterine yazmaz", "BTC/USDT" in a.st["positions"] and not a.st["trades"], a.st["trades"][-1:])
try:
    a.partial("BTC/USDT", p2, 100.0)
    check("kilit yok: kademeli satis reddedilir", False)
except ccxt.BaseError as e:
    check("kilit yok: kademeli satis reddedilir", not ex.orders and not ex.cancels and p2["stop_id"] == "st1", e)
try:
    a.pf_sell("BTC/USDT", 30.0, "test")
    check("kilit yok: portfoy azaltma reddedilir", False)
except ccxt.BaseError as e:
    check("kilit yok: portfoy azaltma reddedilir", not ex.orders and not ex.cancels and p2["stop_id"] == "st1", e)
ex.fo = {"status": "open", "filled": 0, "average": None}
p.kill(); p.wait()
a.sync()
check("eski surec olse de pasif kopya kendiliginden devralmaz", a.own is False and "başka bir motor" in a.error and B._LOCKS.get(B.hashlib.sha256(K2.encode()).hexdigest()[:16]) is None, a.error)
a.b.bid["BTC/USDT"] = 100.0
r = a.enter("ETH/USDT", dict(plan(50), symbol="ETH/USDT"))
check("devralma yok: pasif kopya eski defterle alim yapmaz", r and not ex.orders, r)
a, ex = live()
check("yeniden baslayinca (defter dosyadan) kilit alinir, alim calisir", a.own is True and a.error == "" and a.enter("ETH/USDT", dict(plan(50), symbol="ETH/USDT")) is None and ex.orders, a.error)
p, got = child(KEY)
check("anahtar degisince eski hesabin kilidi birakilir", got == "KILIT", got)
p.kill(); p.wait()

# ---- ayni anahtarli baska veri klasoru: once baslasa da eski defterle islem yapamaz ----
K3 = KEY + "-3"
open(os.path.join(other, os.path.basename(C.STATE_FILE)), "w").close()
_app = C.APP_DIR
p, got = child(K3, other)  # A: hesabi yoneten kopya (ayri veri klasoru)
p.kill(); p.wait()  # A guncellemede kapandi, kilit bos
C.API_KEY = K3
a, ex = live()  # B once basladi
r = a.enter("BTC/USDT", plan(50))
check("kilit bos ama hesabi baska klasor yonetti: B pasif, alim yok", got == "KILIT" and a.own is False and "veri klasörü" in a.error and r and not ex.orders, a.error)
a.st["positions"]["BTC/USDT"] = p3 = pos(1.0)
a.b.bid["BTC/USDT"] = 60.0
a.manage_positions()
check("B: A'nin hesabinda stopa ve satisa dokunmaz", not ex.orders and not ex.cancels and "BTC/USDT" in a.st["positions"], ex.orders)
a.b.bid["BTC/USDT"] = 100.0
p, got = child(K3, other)
check("B kilidi tutmaz: A geri donunce hesabi alir", got == "KILIT", got)
p.kill(); p.wait()
sahip = os.path.join(EV, f".kqb_canli_{B.hashlib.sha256(K3.encode()).hexdigest()[:16]}.sahip")
os.remove(sahip)
a, ex = live()
check("kullanici kaydi silince B yeni sahip olur", a.own is True and a.error == "" and open(sahip, encoding="utf-8").read() == C.APP_DIR, a.error)
p, got = child(K3, other)
check("artik A eski sayilir: kilit bizde", got == "YOK", got)
p.kill(); p.wait()
K4 = KEY + "-4"
h4 = B.hashlib.sha256(K4.encode()).hexdigest()[:16]
gone = os.path.join(C.APP_DIR, "tasinmis_klasor")
open(os.path.join(EV, f".kqb_canli_{h4}.sahip"), "w", encoding="utf-8").write(gone)
C.API_KEY = K4
a, ex = live()
check("eski klasor artik yoksa (tasindi) engel yok", a.own is True and a.error == "", a.error)

# ---- kilit dosyasi hatasi 'baska motor' degildir ----
K5 = KEY + "-5"
C.API_KEY = K5
_open = B.os.open
def _no_file(path, *x, **k):
    if "kqb_canli" in str(path):
        raise PermissionError(13, "izin yok", path)
    return _open(path, *x, **k)
B.os.open = _no_file
a, ex = live()
a.st["positions"]["BTC/USDT"] = p5 = pos(1.0, stop_id=None)
a.b.bid["BTC/USDT"] = 60.0
check("dosya hatasi: yanlis 'baska motor' yok, sebep gorunur, koruma acik", a.own is True and "Hesap kilidi alınamadı" in a.error and "başka bir motor" not in a.error and any("Hesap kilidi" in m for k, m in a.b.notes if k == "uyari"), a.error)
a.manage_positions()
check("dosya hatasi: stop altindaki pozisyon satilir", [o for o in ex.orders if o["typ"] == "market" and o["side"] == "sell"] and "BTC/USDT" not in a.st["positions"], ex.orders)
a.b.bid["BTC/USDT"] = 100.0
ex.orders.clear()
r = a.enter("ETH/USDT", dict(plan(50), symbol="ETH/USDT"))
check("dosya hatasi: yeni alim yok (engel ve kapi)", a.blocked() and r and "kilidi" in r and not ex.orders, r)
B.os.open = _open
a.sync()
check("dosya hatasi gecince kilit alinir, alim acilir", a.lock_err == "" and a.error == "" and a.enter("ETH/USDT", dict(plan(50), symbol="ETH/USDT")) is None and ex.orders, a.error)
p, got = child(K5)
check("dosya hatasi gecince kilit gercekten bizde", got == "YOK", got)
p.kill(); p.wait()

K6 = KEY + "-6"
C.API_KEY = K6
_fl = B._flock
def _no_space(fd):
    raise OSError(B.errno.ENOSPC, "yer yok")
B._flock = _no_space
a, ex = live()
check("kilitleme cagrisinda baska hata (yer yok): pasif degil, alim yok", a.own is True and "kilidi" in a.error and "başka bir motor" not in a.error, a.error)
def _busy(fd):
    raise OSError(B.errno.EAGAIN, "dolu")
B._flock = _busy
a.sync()
check("kilit dosyasi duzelince gercek cakisma cikarsa pasif olunur", a.own is False and "başka bir motor" in a.error, a.error)
B._flock = _fl

K7 = KEY + "-7"
C.API_KEY = K7
import tempfile as _tf
_tmp = _tf.gettempdir()
def _no_tmp(path, *x, **k):
    if "kqb_canli" in str(path) and os.path.dirname(str(path)) == _tmp:
        raise PermissionError(13, "izin yok", path)
    return _open(path, *x, **k)
B.os.open = _no_tmp
a, ex = live()
p, got = child(K7)
check("gecici klasor hatasinda veri klasoru kilidi tutulur: ayni klasordeki kopya alamaz", a.own is True and a.lock_err and got == "YOK", (got, a.error))
p.kill(); p.wait()
B.os.open = _open
a.sync()
p, got = child(K7, other)
check("gecici klasor duzelince kilit tamamlanir: ayri klasordeki kopya da alamaz", a.lock_err == "" and got == "YOK", (got, a.error))
p.kill(); p.wait()

K10 = KEY + "-10"
C.API_KEY, C.LIVE_ALLOWED = K10, False
a, ex = live()
C.LIVE_ALLOWED = True
def _slow(fd):
    time.sleep(0.05)
    _fl(fd)
B._flock = _slow
ts = [threading.Thread(target=a.take_lock) for _ in range(4)]
[x.start() for x in ts]
[x.join() for x in ts]
B._flock = _fl
check("ayni anda kilit denemesi surecin kendi kilidine carpmaz", a.own is True and a.lock_why == "", a.lock_why)

# ---- islem izni kapali kopya kilit almaz ----
K8 = KEY + "-8"
C.API_KEY, C.LIVE_ALLOWED = K8, False
a, ex = live()
p, got = child(K8)
check("izin kapali, defter bos: kilit alinmaz, gercek motor pasif kalmaz", got == "KILIT" and a.own is False and a.lock_why == "" and "başka bir motor" not in (a.blocked() or "") and "admin" in (a.blocked() or ""), (got, a.blocked()))
p.kill(); p.wait()
r = a.enter("BTC/USDT", plan(50))
check("izin kapali kopya alim yapamaz", r and "admin" in r and not ex.orders, r)
C.LIVE_ALLOWED = True
a.sync()
p, got = child(K8)
check("izin sonradan acilinca kilit alinir", a.own is True and a.error == "" and got == "YOK", (got, a.error))
p.kill(); p.wait()
K9 = KEY + "-9"
C.API_KEY, C.LIVE_ALLOWED = K9, False
ex = FakeEx()
B.D.exchange = lambda auth=False: ex
stub = Stub(ex)
a = B.Account(stub, "LIVE", {"positions": {"BTC/USDT": pos(1.0)}})
stub.accounts["LIVE"] = a
a.sync()
p, got = child(K9)
stub.bid["BTC/USDT"] = 60.0
a.st["positions"]["BTC/USDT"]["stop_px"] = 0
a.manage_positions()
check("izin kapali ama defterde pozisyon var: kilit alinir, pozisyon korunur", got == "YOK" and a.own is True and ex.orders and "BTC/USDT" not in a.st["positions"], (got, ex.orders))
p.kill(); p.wait()
C.LIVE_ALLOWED = True

# ---- 2026-11-08: STOP_LOSS yedegi + fail-closed (stop kurulamazsa yeni alim yok) ----
C.LIVE_ALLOWED = True
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.order_raises[("BTC/USDT", "STOP_LOSS_LIMIT")] = ccxt.InvalidOrder('binance {"code":-1013,"msg":"Filter failure: PERCENT_PRICE_BY_SIDE"}')
p["stop_id"] = a.place_stop("BTC/USDT", p, quiet=True)
check("STOP_LOSS_LIMIT reddedilince STOP_LOSS (tetikte piyasa) kurulur", p["stop_id"] and ex.orders[-1]["typ"] == "STOP_LOSS" and ex.orders[-1]["price"] is None
      and ex.orders[-1]["params"]["stopPrice"] == 65.0 and "stop_err" not in p, ex.orders[-1:])
a, ex = live()
base_block = a.blocked()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.order_raises[("BTC/USDT", "STOP_LOSS_LIMIT")] = ccxt.InvalidOrder("red")
ex.order_raises[("BTC/USDT", "STOP_LOSS")] = ccxt.InvalidOrder("red")
p["stop_id"] = a.place_stop("BTC/USDT", p, quiet=True)
check("iki stop turu de reddedilirse stop yok, hata zamani yazilir", p["stop_id"] is None and p.get("stop_err"), p.get("stop_err"))
check("ilk 2 dk yeni alim acik (gecici hata)", a.blocked() == base_block, a.blocked())
p["stop_err"] = time.time() - 200
check("2 dk'dan uzun stopsuz pozisyon: yeni alim durur (fail-closed)", "stop emri kurulamadı" in (a.blocked() or ""), a.blocked())
r = a.enter("ETH/USDT", dict(plan(50), symbol="ETH/USDT"))
check("stopsuz pozisyon varken yeni alis emri gitmez", r and not [o for o in ex.orders if o["side"] == "buy"], r)
ex.order_raises.clear()
p["stop_id"] = a.place_stop("BTC/USDT", p, quiet=True)
check("stop kurulunca alim yeniden acilir", p["stop_id"] and "stop_err" not in p and a.blocked() == base_block, a.blocked())
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.order_raises[("BTC/USDT", "STOP_LOSS_LIMIT")] = B.FenceDeny("kapi")
check("kapi (FenceDeny) reddi STOP_LOSS'a gecmez", a.place_stop("BTC/USDT", p, quiet=True) is None and not [o for o in ex.orders if o["typ"] == "STOP_LOSS"])
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
p["stop_err"] = time.time() - 999
a.live = False
check("sanal hesapta stop kontrolu alimi durdurmaz", "stop emri" not in (a.blocked() or ""))
a.live = True

# ---- sanal hesap dokunulmadi ----
C.API_KEY, C.LIVE_MAX_USDT, C.LIVE_ALLOWED = "", 100.0, False
ex = FakeEx(withdraw=True)
stub = Stub(ex)
pa = B.Account(stub, "PAPER", {})
stub.accounts["PAPER"] = pa
locks0 = dict(B._LOCKS)
_f, B.Account.fence = B.Account.fence, lambda *a, **k: (_ for _ in ()).throw(AssertionError("sanal hesap kapidan gecmez"))
pa.st["cash"] = 1_000_000.0
r = pa.enter("BTC/USDT", plan(900_000))  # sinirin ve %40'in cok ustu
ok = r is None and "BTC/USDT" in pa.st["positions"]
pa.exit("BTC/USDT", pa.st["positions"]["BTC/USDT"], "HEDEF")
B.Account.fence = _f
check("PAPER: kapidan gecmez, sinir yok, borsaya hic gidilmez", ok and not ex.orders and not ex.tickers and not ex.key_reads and not ex.cancels and pa.own and B._LOCKS == locks0, r)
check("LIVE ayarlari bellekte kapali", not C.LIVE_ALLOWED and not C.PORTFOY_LIVE and not C.HABER_LIVE)

print("\nSONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
