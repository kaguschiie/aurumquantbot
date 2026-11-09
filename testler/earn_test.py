"""Bos USDT Esnek Earn: tamamen sahte borsa, ag yok, anahtar yok, gercek emir yok."""
import copy
import os
import re
import shutil
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


ccxt.binance = _no_net
ok_all = True
EV = os.path.join(C.APP_DIR, "ev_earn")
shutil.rmtree(EV, ignore_errors=True)
os.makedirs(EV)
os.environ["HOME"] = os.environ["USERPROFILE"] = EV
for f in os.listdir(C.APP_DIR):
    if f.startswith("state"):
        os.remove(os.path.join(C.APP_DIR, f))


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


class Ex:
    def __init__(self, free=1000.0, user=0.0):
        self.free, self.user, self.botm, self.btc = free, user, 0.0, 0.0
        self.calls, self.orders, self.n, self.gecik, self.yolda = [], [], 0, False, 0.0
        self.err_read, self.err_sub, self.err_red, self.sub_tasi_hata = None, None, None, False
        self.urun = {"productId": "USDT001", "canPurchase": True, "isSoldOut": False, "status": "PURCHASING", "minPurchaseAmount": "0.1", "latestAnnualPercentageRate": "0.03"}
        self.markets = {"BTC/USDT": {"info": {"status": "TRADING"}}}

    def sapi_get_simple_earn_flexible_position(self, p):
        self.calls.append(("pos", p))
        if self.err_read:
            raise self.err_read
        t = self.user + self.botm
        return {"rows": [{"totalAmount": str(t), "productId": "USDT001", "asset": "USDT"}] if t > 0 else [], "total": 1}

    def sapi_get_simple_earn_flexible_list(self, p):
        self.calls.append(("list", p))
        return {"rows": [self.urun], "total": 1}

    def sapi_post_simple_earn_flexible_subscribe(self, p):
        self.calls.append(("sub", p))
        if self.err_sub:
            raise self.err_sub
        a = float(p["amount"])
        assert a <= self.free
        self.free -= a
        self.botm += a
        if self.sub_tasi_hata:
            raise ccxt.NetworkError("cevap gelmedi")
        return {"purchaseId": 1, "success": True}

    def sapi_post_simple_earn_flexible_redeem(self, p):
        self.calls.append(("red", p))
        if self.err_red:
            raise self.err_red
        a = self.user + self.botm if p.get("redeemAll") == "true" else float(p["amount"])
        pay_bot = min(a, self.botm)
        self.botm -= pay_bot
        self.user -= a - pay_bot
        if self.gecik:
            self.yolda += a
        else:
            self.free += a
        return {"redeemId": 1, "success": True}

    def create_order(self, s, typ, side, qty, price=None, params=None):
        self.n += 1
        self.orders.append((s, typ, side, qty, list(c[0] for c in self.calls)))
        if side == "buy" and typ == "market":
            self.free -= qty * 100.0 * 1.001
            self.btc += qty
        return {"id": str(self.n), "filled": qty, "average": price or 100.0, "status": "closed", "fee": None, "fees": []}

    def cancel_order(self, oid, s):
        return {"status": "canceled", "filled": 0}

    def fetch_order(self, oid, s):
        return {"status": "closed", "filled": 0, "average": None}

    def fetch_balance(self):
        return {"USDT": {"free": self.free, "total": self.free}, "BTC": {"free": self.btc, "total": self.btc}}

    def fetch_ticker(self, s):
        return {"last": 100.0}

    def public_get_exchangeinfo(self, p):
        return {"symbols": [{"status": "TRADING"}]}

    def sapi_get_account_apirestrictions(self):
        return {"enableWithdrawals": False, "enableSpotAndMarginTrading": True, "ipRestrict": False}

    def price_to_precision(self, s, p):
        return f"{p:.2f}"

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"

    def market(self, s):
        return {"limits": {"cost": {"min": 5}, "amount": {"min": 0.00001}}, "precision": {"amount": 1e-6, "price": 0.01}}

    def fetch_my_trades(self, *a, **k):
        return []

    def fetch_open_orders(self, *a, **k):
        return []


class Stub:
    def __init__(self, ex):
        self.ex, self.bid, self.ask = ex, {"BTC/USDT": 100.0}, {"BTC/USDT": 100.0}
        self.market, self.lock, self.stop, self.accounts, self.notes = {}, threading.RLock(), threading.Event(), {}, []

    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        self.notes.append((kind, msg))

    def save(self):
        pass


C.API_KEY, C.API_SECRET, C.TELEGRAM_TOKEN = "sahte-test-anahtari", "sahte", ""
C.LIVE_MAX_USDT = 100.0
L.init()
B.EARN_WAIT_SEC = 2
cfg = open(os.path.join(PROJ, "config.py"), encoding="utf-8").read()
check("varsayilan kapali", re.search(r"^EARN_USDT = False", cfg, re.M))
check("ayar sadece admin", "EARN_USDT" in C.EDITABLE and "EARN_USDT" not in C.USER_EDITABLE)


def live(ex=None, st=None, on=True):
    C.LIVE_ALLOWED, C.PORTFOY_LIVE, C.EARN_USDT = True, True, on
    for f in os.listdir(C.APP_DIR):
        if f.startswith("state"):
            os.remove(os.path.join(C.APP_DIR, f))
    ex = ex or Ex()
    B.D.exchange = lambda auth=False: ex
    a = B.Account(Stub(ex), "LIVE", {})
    a.b.accounts["LIVE"] = a
    if st:
        a.st.update(copy.deepcopy(st))
    a.sync()
    return a, ex


def sayi(ex, k):
    return sum(c[0] == k for c in ex.calls)


# ---- kapaliyken hic etki yok ----
a, ex = live(on=False)
check("kapali: toplam kasa", abs(a.equity() - 1000) < 1e-6, a.equity())
check("kapali: yatirma yok", a.earn_park() is None and ex.botm == 0)
a.sync(); a.free_cash(); a.earn_tick()
check("kapali: Earn ucu hic cagrilmaz", not ex.calls, ex.calls)
for ad, k in (("PORTFOY_LIVE", "PORTFOY_LIVE"), ("LIVE_ALLOWED", "LIVE_ALLOWED")):
    a, ex = live()
    setattr(C, k, False)
    check(f"{ad} kapaliyken yatirma yok", a.earn_park() is None and ex.botm == 0)
    setattr(C, k, True)
a, ex = live()
a.key_ok = False
check("anahtar izni dogrulanmadan yatirma yok", a.earn_park() is None and ex.botm == 0)
a, ex = live()
ex.urun["status"] = "END"
check("urun kapaliysa yatirma yok", a.earn_park() is None and ex.botm == 0)

# ---- yatirma ----
a, ex = live()
t = a.earn_park()
sub = [c[1] for c in ex.calls if c[0] == "sub"]
check("yatirma: kasanin %5'i spotta kalir", t == 950.0 and abs(ex.free - 50) < 1e-6 and ex.botm == 950, (t, ex.free))
check("yatirma: otomatik abonelik kapali, spot kaynak", sub and sub[0]["autoSubscribe"] == "false" and sub[0]["sourceAccount"] == "SPOT", sub)
check("yatirma: kasa degismez", abs(a.equity() - 1000) < 1e-6, a.equity())
check("yatirma: bos para Earn dahil", abs(a.free_cash() - 1000) < 1e-6, a.free_cash())
check("yatirma: onay gelince dondurma biter", a.earn_hold == 0)
n = sayi(ex, "sub")
check("kucuk artik icin tekrar yatirma yok", a.earn_park() is None and sayi(ex, "sub") == n)

# ---- alim oncesi cekme ----
a, ex = live()
a.earn_park()
o = a.order("BTC/USDT", "market", "buy", 0.8, act={"do": "x"}) if False else None
a.st["orders"].clear()
ex.calls.clear()
a.key_ok = True
try:
    a.order("BTC/USDT", "market", "buy", 0.8)
    r = None
except Exception as e:
    r = e
check("alim: spot yetmezse once cekilir, sonra emir", r is None and ex.orders and "red" in ex.orders[0][4] and ex.free > 800, (r, ex.orders, ex.free))
check("alim: tum bot parasi spota doner", ex.botm == 0 and a.st["earn_son"] == 0 and sayi(ex, "red") == 1)
check("alim: redeemAll (kullanici parasi yok)", [c[1] for c in ex.calls if c[0] == "red"][0].get("redeemAll") == "true")
a, ex = live()
a.earn_park()
ex.calls.clear()
a.order("BTC/USDT", "market", "buy", 0.1)
check("alim: spot yetiyorsa cekme yok", sayi(ex, "red") == 0 and ex.orders)

# ---- cekme gecikir: emir gitmez, kasa dusmus gorunmez ----
a, ex = live()
a.earn_park()
ex.gecik = True
ex.calls.clear()
hata = None
try:
    a.order("BTC/USDT", "market", "buy", 0.8)
except ccxt.BaseError as e:
    hata = e
check("gecikme: emir gitmez, hata tekrar denenir turden", isinstance(hata, ccxt.ExchangeNotAvailable) and not ex.orders, hata)
check("gecikme: uyari var", any("Earn'den çekilemedi" in m for _, m in a.b.notes))
a.sync()
check("gecikme: kasa dusmus gorunmez", abs(a.equity() - 1000) < 1e-6 and a.earn_hold, a.equity())
ex.free += ex.yolda; ex.yolda = 0
a.earn_hold = time.time() - 1
a.earn_t = 0
a.sync()
a.sync()
check("gecikme bitince kasa dogru", abs(a.equity() - 1000) < 1e-6 and a.earn_hold == 0, (a.equity(), a.earn_hold))

# ---- kullanicinin kendi Earn parasi ----
a, ex = live(Ex(user=200.0))
a.earn_park()
a.sync()
check("kullanici parasi kasaya sayilmaz", abs(a.equity() - 1000) < 1e-6 and a.st["earn_dis"] == 200 and a.st["earn_son"] == 950, (a.equity(), a.st["earn_dis"]))
ex.calls.clear()
a.order("BTC/USDT", "market", "buy", 0.8)
red = [c[1] for c in ex.calls if c[0] == "red"]
check("kullanici parasi cekilmez", ex.user == 200 and ex.botm == 0 and "amount" in red[0] and "redeemAll" not in red[0], red)
a, ex = live(Ex(user=200.0))
a.earn_park()
ex.user = 50.0
a.earn_t = 0
a.sync()
check("kullanici kendi parasini cekerse bot payi bozulmaz", a.st["earn_dis"] == 50 and abs(a.st["earn_son"] - 950) < 1e-6 and abs(a.equity() - 1000) < 1e-6, (a.st["earn_dis"], a.st["earn_son"]))
a, ex = live()
a.earn_park()
ex.botm += 1.5  # faiz
a.earn_t = 0
a.sync()
check("faiz kasaya eklenir", abs(a.equity() - 1001.5) < 1e-6, a.equity())

# ---- okuma hatalari ----
a, ex = live()
a.earn_park()
ex.err_read = ccxt.NetworkError("yok")
a.earn_t = 0
a.sync()
check("konum okunamazsa kasa son degerle kalir", abs(a.equity() - 1000) < 1e-6 and a.synced, a.equity())
ol = copy.deepcopy({k: a.st[k] for k in ("earn_var", "earn_dis", "earn_son")})
b2, ex2 = live(ex, ol)
check("yeniden baslama + okuma hatasi: kasa korunur", abs(b2.equity() - 1000) < 1e-6, b2.equity())
ex.err_read = None

# ---- yatirma reddi / belirsiz ----
a, ex = live()
ex.err_sub = ccxt.PermissionDenied("izin yok")
check("red: istisna atmaz, uyarir", a.earn_park() is None and any("Earn'e konamadı" in m for _, m in a.b.notes))
a.sync()
check("red: kasa dogru", abs(a.equity() - 1000) < 1e-6 and ex.free == 1000)
a.earn_hold = time.time() - 1; a.earn_t = 0; a.sync(); a.sync()
check("red: dondurma biter", a.earn_hold == 0 and abs(a.equity() - 1000) < 1e-6, a.earn_hold)
a, ex = live()
ex.sub_tasi_hata = True
a.earn_park()
a.sync()
check("belirsiz yatirma: para gitti, kasa dusmus gorunmez", ex.botm == 950 and abs(a.equity() - 1000) < 1e-6 and a.earn_hold, a.equity())
a.earn_hold = time.time() - 1; a.earn_t = 0; a.sync(); a.sync()
check("belirsiz yatirma: sonra dogru okunur", a.st["earn_son"] == 950 and abs(a.equity() - 1000) < 1e-6 and not a.earn_hold, a.st["earn_son"])

# ---- kapatinca doner ----
a, ex = live()
a.earn_park()
C.EARN_USDT = False
a.earn_tick()
check("ayar kapaninca para spota doner", ex.botm == 0 and abs(ex.free - 1000) < 1e-6 and a.st["earn_son"] == 0)
n = sayi(ex, "red")
a.earn_tick()
check("sonra bir daha cekilmez", sayi(ex, "red") == n)
C.EARN_USDT = True

# ---- Earn bozukken satis ve zarar kilidi ----
a, ex = live()
a.earn_park()
ex.err_read = ex.err_red = ccxt.ExchangeError("bakim")
a.st["positions"]["BTC/USDT"] = {"qty": 1.0, "qty0": 1.0, "entry": 100.0, "atr": 1.0, "sl": 65.0, "sl0": 65.0, "tp": 1e5, "part": False, "hi": 100.0, "fee": 0.0, "realized": 0.0,
                                 "t": time.time(), "stop_id": None, "stop_px": 65.0, "partial_at": None, "kind": PF.KIND, "manual": False, "max_hours": 1e6, "exit": {}, "seen": time.time()}
n = ex.n
a.order("BTC/USDT", "market", "sell", 1.0)
check("Earn bakimdayken satis gider", ex.n == n + 1 and ex.orders[-1][2] == "sell")
del a.st["positions"]["BTC/USDT"]

# ---- dengeleme ----
a, ex = live()
a.earn_park()
ex.calls.clear()
C.LIVE_MAX_USDT = 500.0
r = a.rebalance({"BTC/USDT": 0.3}, False)
pos = a.st["positions"].get("BTC/USDT") or {}
check("dengeleme: tutar Earn dahil kasadan hesaplanir, alim gider", 2.5 < (pos.get("qty") or 0) < 3.1 and sayi(ex, "red") == 1, (r, pos.get("qty"), ex.free))
check("dengeleme: hata yok", a.pf_err == 0, a.pf_err)
a, ex = live()
a.earn_park()
ex.gecik = True
ex.calls.clear()
r = a.rebalance({"BTC/USDT": 0.3}, False)
check("dengeleme: para gelmezse alim yok, tamamlanmadi sayilir", a.pf_err >= 1 and "BTC/USDT" not in a.st["positions"], (r, a.pf_err))
C.LIVE_MAX_USDT = 100.0

# ---- arayuz ----
ui = open(os.path.join(PROJ, "ui.html"), encoding="utf-8").read()
check("arayuzde ayar ve kasa bilgisi var", '"EARN_USDT"' in ui and "s.earn" in ui)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
