"""Gercek emrin tekillik yolu: kimlik once diske, hata ayrimi (gitmedi / red / belirsiz), kimlikle sorgu, dolumda kayit, gercek komisyon.
Tamamen sahte borsa: ag yok, gercek anahtar yok, gercek emir yok. Kullanim: KQB_APP_DIR=<test> python -I emir-kimligi_test.py <proje>"""
import importlib.util
import json
import os
import re
import shutil
import socket
import sys
import threading
import time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
import ccxt
import requests
import config as C
import bot as B
import dersler as L
import portfoy as PF


def _no_net(*a, **k):
    raise AssertionError("testte gercek baglanti yok")


socket.socket.connect = _no_net  # hicbir yol internete cikamaz (gercek ccxt nesnesi de sahte oturumla)
ok_all = True
EV = os.path.join(C.APP_DIR, "ev_emir")
shutil.rmtree(EV, ignore_errors=True)
os.makedirs(EV)
os.environ["HOME"] = os.environ["USERPROFILE"] = EV
C.STATE_FILE = os.path.join(C.APP_DIR, "emir_state.json")
C.API_KEY, C.API_SECRET, C.TELEGRAM_TOKEN = "sahte-test-anahtari", "sahte", ""  # sadece bu surecin bellegi; dosyaya yazilmaz
C.LIVE_ALLOWED = True  # sahte borsa icin; kaydedilmez
C.LIVE_MAX_USDT = 500.0
L.init()
B.ORDER_RETRY_SEC = 0.01


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


class FakeEx:
    """Sahte borsa. plan: siradaki create_order'in davranisi. venue: borsadaki emirler {kimlik: emir}."""

    def __init__(self, last=100.0):
        self.last, self.sent, self.cancels, self.venue, self.hidden, self.trades, self.plan = last, [], [], {}, {}, {}, []
        self.n, self.on_send, self.query_raises, self.queries, self.trades_raises = 0, None, None, [], None
        self.fee = {"currency": "BTC", "rate": 0.001}  # None: cevapta komisyon yok
        self.fill_frac, self.free = 1.0, 1e6
        self.markets = {}

    def _mk(self, cid, s, typ, side, qty, price, inline=True):
        self.n += 1
        oid = f"v{self.n}"
        market = typ == "market"
        filled = qty * self.fill_frac if market else 0.0
        avg = (price or self.last) if market else None
        fee = None
        if market and self.fee:
            cur = self.fee["currency"]
            cost = self.fee["cost"] if "cost" in self.fee else filled * self.fee["rate"] * (avg if cur == "USDT" else 1)
            fee = {"currency": cur, "cost": cost}
            self.trades[oid] = [{"fee": fee}]
        o = {"id": oid, "clientOrderId": cid, "symbol": s, "side": side, "type": typ, "status": "closed" if market else "open",
             "filled": filled, "average": avg, "fee": fee, "fees": [fee] if fee else []}
        self.venue[cid] = dict(o, fee=None, fees=[])  # GET /order cevabinda komisyon yoktur (gercek Binance gibi)
        return o

    def create_order(self, s, typ, side, qty, price=None, params=None):
        cid = params["newClientOrderId"]
        self.sent.append({"s": s, "typ": typ, "side": side, "qty": qty, "price": price, "cid": cid, "params": params})
        if self.on_send:
            self.on_send(cid)
        act = self.plan.pop(0) if self.plan else "ok"
        if isinstance(act, Exception):
            raise act
        if act == "landed":  # borsa isledi, cevap gelmedi
            self._mk(cid, s, typ, side, qty, price)
            raise ccxt.RequestTimeout("zaman asimi")
        if act == "late":  # borsa isleyecek ama sorguda henuz gorunmuyor
            self.hidden[cid] = (s, typ, side, qty, price)
            raise ccxt.RequestTimeout("zaman asimi")
        if act == "lost":  # hic ulasmadi ama biz bilmiyoruz
            raise ccxt.RequestTimeout("zaman asimi")
        if act == "ack":  # sadece kabul cevabi (dolum bilgisi yok)
            o = self._mk(cid, s, typ, side, qty, price)
            return {"id": o["id"], "clientOrderId": cid, "filled": None, "average": None, "status": None}
        if act == "expired":
            self.n += 1
            self.venue[cid] = {"id": f"v{self.n}", "clientOrderId": cid, "status": "expired", "filled": 0.0, "average": None, "fee": None, "fees": []}
            return dict(self.venue[cid])
        return self._mk(cid, s, typ, side, qty, price)

    def land(self):
        for cid, a in self.hidden.items():
            self._mk(cid, *a)
        self.hidden = {}

    def fetch_order(self, oid, s, params=None):
        self.queries.append((oid, s, params))
        if self.query_raises:
            raise self.query_raises
        cid = (params or {}).get("origClientOrderId")
        if cid:
            if cid not in self.venue:
                raise ccxt.OrderNotFound("binance -2013 Order does not exist")
            return dict(self.venue[cid])
        for o in self.venue.values():
            if o["id"] == oid:
                return dict(o)
        raise ccxt.OrderNotFound("yok")

    def cancel_order(self, oid, s):
        self.cancels.append(oid)
        for o in self.venue.values():
            if o["id"] == oid:
                o["status"] = "canceled"
                return dict(o)
        return {"status": "canceled", "filled": 0}

    def fetch_my_trades(self, s, since=None, limit=None, params=None):
        if self.trades_raises:
            raise self.trades_raises
        return self.trades.get(params["orderId"], [])

    def fetch_balance(self):
        return {"USDT": {"free": 1000.0, "total": 1000.0}, "BTC": {"free": self.free, "total": self.free}, "ETH": {"free": self.free, "total": self.free}}

    def fetch_ticker(self, s):
        return {"last": self.last}

    def public_get_exchangeinfo(self, p):
        return {"symbols": [{"status": "TRADING"}]}

    def sapi_get_account_apirestrictions(self):
        return {"enableWithdrawals": False}

    def price_to_precision(self, s, p):
        return f"{p:.2f}"

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"

    def market(self, s):
        return {"limits": {"cost": {"min": 5}}}


class Stub:
    """Motorun Account'a gosterdigi kismi; kayit gercek Bot.save."""

    def __init__(self, ex):
        self.ex, self.bid, self.ask = ex, {"BTC/USDT": 100.0, "ETH/USDT": 100.0, "BNB/USDT": 500.0}, {"BTC/USDT": 100.0, "ETH/USDT": 100.0}
        self.market, self.lock, self.stop, self.accounts, self.notes, self.file, self.ag = {}, threading.RLock(), threading.Event(), {}, [], {}, {}

    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        self.notes.append((kind, msg))

    def save(self):
        B.Bot.save(self)


def live(ex=None, saved=None):
    ex = ex or FakeEx()
    B.D.exchange = lambda auth=False: ex
    a = B.Account(Stub(ex), "LIVE", saved or {})
    a.b.accounts["LIVE"] = a
    a.sync()
    return a, ex


def pos(qty, entry=100.0, sl=65.0, stop_id=None):
    return {"qty": qty, "qty0": qty, "entry": entry, "atr": 1.0, "sl": sl, "sl0": sl, "tp": entry * 1000, "part": False, "hi": entry,
            "fee": 0.0, "realized": 0.0, "t": time.time(), "stop_id": stop_id, "stop_px": sl if stop_id else None, "partial_at": None,
            "kind": PF.KIND, "manual": False, "max_hours": 1e6, "exit": {}, "seen": time.time()}


def plan(usdt=100.0, entry="taker"):
    return {"type": "buy", "symbol": "BTC/USDT", "kind": PF.KIND, "entry": entry, "usdt": usdt, "stop": 65.0, "tp2": 1e5, "tp1": 0,
            "risk_pct": 0, "max_hours": 1e6, "reason": "test", "conviction": None}


def disk():
    with open(C.STATE_FILE, encoding="utf-8") as fh:
        return json.load(fh)["LIVE"]


def age(a, sec=None):
    """Kayitlari yaslandir: sorgu kisitlamasi kalksin; sec verilirse emir o kadar eski."""
    for r in a.st["orders"].values():
        r["chk"] = 0
        if sec:
            r["t"] = time.time() - sec


# ---- kimlik ----
c1 = B.order_cid("LIVE", 7, "BTC/USDT", "market", "buy", 1.0, None, 123.5)
check("kimlik belirlenimci: ayni girdi ayni kimlik", c1 == B.order_cid("LIVE", 7, "BTC/USDT", "market", "buy", 1.0, None, 123.5), c1)
check("kimlik farkli emirde farkli", len({c1, B.order_cid("LIVE", 8, "BTC/USDT", "market", "buy", 1.0, None, 123.5), B.order_cid("LIVE", 7, "ETH/USDT", "market", "buy", 1.0, None, 123.5),
                                         B.order_cid("LIVE", 7, "BTC/USDT", "market", "sell", 1.0, None, 123.5), B.order_cid("LIVE", 7, "BTC/USDT", "market", "buy", 1.0, None, 124.5)}) == 5)
check("kimlik Binance bicimine uyar (36 karakter, cok buyuk sira no da)", re.fullmatch(r"[a-zA-Z0-9_-]{1,36}", B.order_cid("LIVE", 10**10, "BTC/USDT", "STOP_LOSS_LIMIT", "sell", 1.0, 9.0, 1.0)))

# ---- kimlik gonderimden once diskte ----
a, ex = live()
seen = {}
def _on_send(cid):
    if seen:
        return
    d = disk()
    seen.update(cid=cid, disk=cid in d["orders"], r=d["orders"].get(cid), seq=d["oseq"])
ex.on_send = _on_send
check("alis acildi", a.enter("BTC/USDT", plan()) is None and "BTC/USDT" in a.st["positions"], a.b.notes[-1:])
check("gonderim aninda kimlik diskte", seen["disk"] and seen["r"]["side"] == "buy" and seen["r"]["act"]["do"] == "enter" and seen["seq"] >= 1, seen)
s0 = ex.sent[0]
check("borsaya newClientOrderId gitti, kayitla ayni, kayitli alanlardan yeniden uretilebilir", s0["params"]["newClientOrderId"] == seen["cid"]
      and seen["cid"] == B.order_cid("LIVE", seen["seq"], "BTC/USDT", "market", "buy", seen["r"]["qty"], None, seen["r"]["t"]), s0)
check("stop emrinin de kendi kimligi var", ex.sent[1]["typ"] == "STOP_LOSS_LIMIT" and ex.sent[1]["cid"] != s0["cid"] and ex.sent[1]["cid"] in ex.venue, ex.sent[1:])
d = disk()
check("dolumdan hemen sonra diskte: pozisyon var, acik kayit yok", "BTC/USDT" in d["positions"] and d["positions"]["BTC/USDT"]["stop_id"] and not d["orders"] and not a.st["orders"], d["orders"])
ex.on_send = None

# ---- gercek komisyon: alis ----
p = a.st["positions"]["BTC/USDT"]
check("komisyon coinden: miktar = dolan - komisyon (sabit oran varsayimi degil)", abs(p["qty"] - 0.999) < 1e-9 and abs(p["fee"] - 0.1) < 1e-9, (p["qty"], p["fee"]))
a, ex = live()
ex.fee = {"currency": "BTC", "rate": 0.00075}
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("indirimli oran gercekten okunur (%0,075)", abs(p["qty"] - 0.99925) < 1e-9 and abs(p["fee"] - 0.075) < 1e-9, (p["qty"], p["fee"]))
a, ex = live()
ex.fee = {"currency": "BNB", "cost": 0.0002}
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("komisyon BNB: coinden kesinti yok, USDT karsiligi BNB kuruyla", p["qty"] == 1.0 and abs(p["fee"] - 0.0002 * 500) < 1e-9, (p["qty"], p["fee"]))
a, ex = live()
a.b.bid.pop("BNB/USDT")
ex.fee = {"currency": "BNB", "cost": 0.0002}
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("BNB fiyati akista yoksa borsadan okunur", p["qty"] == 1.0 and abs(p["fee"] - 0.0002 * ex.last) < 1e-9, p["fee"])
a, ex = live()
ex.fee = {"currency": "BTC", "cost": 0.0}
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("sifir komisyon sifirdir, tahmin yapilmaz", p["qty"] == 1.0 and p["fee"] == 0.0, (p["qty"], p["fee"]))
a, ex = live()
ex.free = 0.5
a.enter("BTC/USDT", plan())
check("bakiye sinirlamasi korunur", a.st["positions"]["BTC/USDT"]["qty"] == 0.5)
a, ex = live()
_mk = ex._mk
ex._mk = lambda *x, **k: {**_mk(*x, **k), "fee": None, "fees": []}  # cevapta komisyon yok, islem listesinde var
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("cevapta yoksa islem listesinden", abs(p["qty"] - 0.999) < 1e-9 and abs(p["fee"] - 0.1) < 1e-9, (p["qty"], p["fee"]))
a, ex = live()
ex.fee, ex.trades_raises = None, ccxt.NetworkError("yok")
a.enter("BTC/USDT", plan())
p = a.st["positions"]["BTC/USDT"]
check("hicbir yerden okunamazsa eski tahmin (miktar x FEE)", abs(p["qty"] - (1 - C.FEE)) < 1e-9 and abs(p["fee"] - 100 * C.FEE) < 1e-9, (p["qty"], p["fee"]))

# ---- gercek komisyon: satis ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.fee = {"currency": "USDT", "rate": 0.0005}
a.b.bid["BTC/USDT"] = 110.0
ex.last = 110.0
a.exit("BTC/USDT", p, "HEDEF")
t = a.st["trades"][-1]
check("satis komisyonu USDT: K/Z gercek komisyonla", abs(t["pnl"] - (10.0 - 110.0 * 0.0005)) < 1e-9 and "BTC/USDT" not in a.st["positions"], t["pnl"])
check("satistan hemen sonra diskte: pozisyon yok, islem kayitli", "BTC/USDT" not in disk()["positions"] and len(disk()["trades"]) == 1 and not disk()["orders"])
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.fee = {"currency": "BNB", "cost": 0.0001}
a.exit("BTC/USDT", p, "HEDEF")
check("satis komisyonu BNB: USDT karsiligi", abs(a.st["trades"][-1]["pnl"] - (0.0 - 0.0001 * 500)) < 1e-9, a.st["trades"][-1]["pnl"])
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
ex.venue["st"] = {"id": "st9", "clientOrderId": "st", "status": "closed", "filled": 1.0, "average": 90.0, "fee": None, "fees": []}
ex.trades["st9"] = [{"fee": {"currency": "USDT", "cost": 0.07}}]
p.update(stop_id="st9", stop_px=90.0, sl=90.0)
a.manage_positions()
t = a.st["trades"][-1]
check("borsa stopu doldu: gercek komisyon islem listesinden, hemen diskte", abs(t["pnl"] - (-10.0 - 0.07)) < 1e-9 and not disk()["positions"], t["pnl"])
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id="st8")
ex.trades["st8"] = [{"fee": {"currency": "USDT", "cost": 0.05}}]
ex.venue["st"] = {"id": "st8", "clientOrderId": "st", "status": "open", "filled": 3.0, "average": 100.0, "fee": None, "fees": []}
a.partial("BTC/USDT", p, 100.0)
sold = min(10.0 * C.PARTIAL_FRAC, 7.0)
check("stop iptalde kismi dolum: gercek komisyon, miktar duser, kalandan satilir", abs(p["qty"] - (7.0 - sold)) < 1e-6 and abs(p["realized"] - (-0.05 - sold * 0.001 * 100)) < 1e-9, (p["qty"], p["realized"]))

# ---- limit alis: dolunca gercek komisyon ----
a, ex = live()
check("limit alis: bekleyen emir kimlik + borsa numarasiyla", a.enter("BTC/USDT", plan(entry="maker")) is None and a.st["pending"]["BTC/USDT"]["id"] == ex.venue[ex.sent[0]["cid"]]["id"] and ex.sent[0]["params"]["postOnly"] and not a.st["orders"])
v = ex.venue[ex.sent[0]["cid"]]
v.update(status="closed", filled=1.0, average=100.0)
ex.trades[v["id"]] = [{"fee": {"currency": "BTC", "cost": 0.00075}}]
a.manage_pending()
p = a.st["positions"].get("BTC/USDT")
check("limit doldu: gercek komisyon, hemen diskte", p and abs(p["qty"] - 0.99925) < 1e-9 and "BTC/USDT" in disk()["positions"] and not disk()["pending"], p and p["qty"])

# ---- belirsiz: emir borsada dolmus ----
a, ex = live()
ex.plan = ["landed"]
r = a.enter("BTC/USDT", plan())
p = a.st["positions"].get("BTC/USDT")
check("zaman asimi + kimlikle sorgu buldu: yeniden gonderim yok, pozisyon acildi", r is None and p and len([x for x in ex.sent if x["typ"] == "market"]) == 1, (r, ex.sent[:2]))
check("sorgu origClientOrderId ile", ex.queries[0][2] == {"origClientOrderId": ex.sent[0]["cid"]}, ex.queries[:1])
check("sorgudan gelen dolumda komisyon islem listesinden", abs(p["qty"] - 0.999) < 1e-9 and abs(p["fee"] - 0.1) < 1e-9, (p["qty"], p["fee"]))
check("kayit silindi, diskte pozisyon", not a.st["orders"] and "BTC/USDT" in disk()["positions"] and not disk()["orders"])
check("stop da kuruldu", p["stop_id"] and ex.sent[-1]["typ"] == "STOP_LOSS_LIMIT")

# ---- belirsiz: emir sorguda henuz yok, sonra gorunuyor ----
a, ex = live()
ex.plan = ["late"]
r = a.enter("BTC/USDT", plan())
n0 = len(ex.sent)
check("zaman asimi + yok: reddedildi sayilmaz, kullaniciya bilinmiyor denir", "BTC/USDT" not in a.st["positions"] and a.st["orders"] and "bilinmiyor" in r and "reddetti" not in r, r)
check("uyari kaydi", any("sonucu bilinmiyor" in m and k == "uyari" for k, m in a.b.notes), a.b.notes[-2:])
check("kayit diskte 'yolda' durumunda", len(disk()["orders"]) == 1, disk()["orders"])
r2 = a.enter("BTC/USDT", plan())
check("ikinci alis denemesi borsaya gitmez", len(ex.sent) == n0 and "bilinmiyor" in r2, r2)
a.st["positions"]["ETH/USDT"] = pe = pos(1.0, stop_id=None)
a.exit("ETH/USDT", pe, "HEDEF")
check("baska coin etkilenmez", "ETH/USDT" not in a.st["positions"])
n1 = len(ex.sent)
age(a)
a.manage_pending()
check("sorgu hala yok, sure dolmadi: kayit kalir, gonderim yok", a.st["orders"] and len(ex.sent) == n1 and "BTC/USDT" not in a.st["positions"])
q = len(ex.queries)
a.manage_pending()
a.manage_pending()
check("sorgu kisitli: her saniye borsaya sorulmaz", len(ex.queries) == q, len(ex.queries) - q)
ex.land()
age(a)
a.manage_pending()
p = a.st["positions"].get("BTC/USDT")
check("borsada gorununce pozisyon acilir (tam bir kez)", p and abs(p["qty"] - 0.999) < 1e-9 and not a.st["orders"], p and p["qty"])
a.manage_pending()
check("tekrar islenmez", len([x for x in ex.sent if x["typ"] == "market" and x["s"] == "BTC/USDT"]) == 1 and abs(a.st["positions"]["BTC/USDT"]["qty"] - 0.999) < 1e-9)
check("bulunan emir bildirilir", any("bulundu" in m for _, m in a.b.notes), a.b.notes[-3:])

# ---- belirsiz: emir hic ulasmamis ----
a, ex = live()
ex.plan = ["lost"]
a.enter("BTC/USDT", plan())
cid0 = ex.sent[0]["cid"]
age(a, 60)
a.manage_pending()
check("60 sn yok: hala kilitli", a.st["orders"] and a.enter("BTC/USDT", plan()) and len(ex.sent) == 1)
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
check("sure doldu + hala yok: kayit silinir, uyari, diskte yok", not a.st["orders"] and not disk()["orders"] and any("bulunamadı" in m for _, m in a.b.notes))
check("sonraki deneme yeni kimlikle gider", a.enter("BTC/USDT", plan()) is None and ex.sent[1]["cid"] != cid0 and "BTC/USDT" in a.st["positions"], ex.sent[1]["cid"])

# ---- sorgu da calismiyorsa ----
a, ex = live()
ex.plan = ["landed"]
ex.query_raises = ccxt.NetworkError("ag yok")
a.enter("BTC/USDT", plan())
age(a, 3600)
a.manage_pending()
check("sorgu calismiyor: kayit silinmez (saat gecse de), kilit kalir, uyari", a.st["orders"] and "BTC/USDT" not in a.st["positions"] and any("okunamıyor" in m for _, m in a.b.notes), a.b.notes[-1:])
ex.query_raises = None
age(a)
a.manage_pending()
check("sorgu duzelince dolum bulunur", "BTC/USDT" in a.st["positions"] and not a.st["orders"])

# ---- red: tekrar yok ----
for name, err in (("bakiye yetersiz", ccxt.InsufficientFunds("-2010 Account has insufficient balance")), ("gecersiz emir", ccxt.InvalidOrder("LOT_SIZE")),
                  ("hatali istek", ccxt.BadRequest("-1013")), ("anahtar", ccxt.AuthenticationError("-2015")), ("izin", ccxt.PermissionDenied("x")), ("post only", ccxt.OrderImmediatelyFillable("-2010 would match"))):
    a, ex = live()
    ex.plan = [err]
    r = a.enter("BTC/USDT", plan())
    check(f"red ({name}): bir kez gonderilir, sorgu yok, kayit silinir, coin kilitlenmez", len(ex.sent) == 1 and not ex.queries and not a.st["orders"] and not disk()["orders"] and r and "reddetti" in r and a.enter("BTC/USDT", plan()) is None, r)

# ---- gitmedi: ayni kimlikle yeniden ----
for name, err in (("hiz siniri", ccxt.RateLimitExceeded("-1003")), ("429", ccxt.DDoSProtection("429")), ("zaman damgasi", ccxt.InvalidNonce("-1021"))):
    a, ex = live()
    ex.plan = [err]
    r = a.enter("BTC/USDT", plan())
    mk = [x for x in ex.sent if x["typ"] == "market"]
    check(f"gitmedi ({name}): ayni kimlikle bir kez daha, sorgu yok, tek pozisyon", r is None and len(mk) == 2 and mk[0]["cid"] == mk[1]["cid"] and not ex.queries and "BTC/USDT" in a.st["positions"], r)
a, ex = live()
ex.plan = [ccxt.RateLimitExceeded("-1003"), ccxt.RateLimitExceeded("-1003")]
r = a.enter("BTC/USDT", plan())
check("gitmedi iki kez: kayit silinir, kilit yok, sonraki dongu dener", len(ex.sent) == 2 and not a.st["orders"] and r and not ex.queries and a.enter("BTC/USDT", plan()) is None, r)
a, ex = live()
ex.plan = [ccxt.RateLimitExceeded("-1003"), ccxt.InvalidOrder("binance {\"code\":-2010,\"msg\":\"Duplicate order sent.\"}")]
ex.on_send = lambda cid: ex.plan and len(ex.sent) == 2 and ex._mk(cid, "BTC/USDT", "market", "buy", 1.0, None)
r = a.enter("BTC/USDT", plan())
ex.on_send = None
check("yeniden denemede 'Duplicate': red sayilmaz, sorguyla bulunur", r is None and "BTC/USDT" in a.st["positions"] and ex.queries, r)
a, ex = live()
ex.plan = [ccxt.ExchangeError("taninmayan"), ]
a.enter("BTC/USDT", plan())
check("taninmayan borsa hatasi reddedildi sayilmaz: sorgulanir", a.st["orders"] and ex.queries and len(ex.sent) == 1)
a, ex = live()
ex.plan = [ValueError("bozuk cevap")]
r = a.enter("BTC/USDT", plan())
check("ccxt disi hata (cevap cozulemedi): belirsiz", a.st["orders"] and ex.queries and "bilinmiyor" in r, r)

# ---- gercek ccxt siniflandirmasi: istek sahte oturumdan gecer ----
MK = {"id": "BTCUSDT", "symbol": "BTC/USDT", "base": "BTC", "quote": "USDT", "baseId": "BTC", "quoteId": "USDT", "spot": True, "margin": False, "swap": False, "future": False, "option": False,
      "contract": False, "linear": None, "inverse": None, "type": "spot", "active": True, "precision": {"amount": 0.00001, "price": 0.01}, "taker": 0.001, "maker": 0.001,
      "limits": {"amount": {"min": 0.00001, "max": None}, "price": {"min": 0.01, "max": None}, "cost": {"min": 5, "max": None}, "market": {"min": None, "max": None}},
      "info": {"status": "TRADING", "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT"]}}


class Sess(requests.Session):
    """ccxt'nin gordugu HTTP oturumu: script'teki cevabi dondurur, baska hicbir yere gitmez."""

    def __init__(self):
        super().__init__()
        self.script, self.calls = [], []

    def request(self, method, url, data=None, headers=None, **k):
        self.calls.append((method, url, data.decode() if isinstance(data, bytes) else data))
        x = self.script.pop(0)
        if isinstance(x, Exception):
            raise x
        r = requests.Response()
        r.status_code, r._content, r.url, r.reason = x[0], json.dumps(x[1]).encode(), url, "x"
        r.headers["Content-Type"] = "application/json"
        return r


def real():
    e = ccxt.binance({"apiKey": "sahte", "secret": "sahte", "enableRateLimit": False})
    e.set_markets([MK], {c: {"id": c, "code": c, "precision": 1e-8} for c in ("BTC", "USDT", "BNB")})
    e.session = Sess()
    return e


rx = real()
for name, want, x in (
        ("okuma zaman asimi", "belirsiz", requests.exceptions.ReadTimeout("Read timed out")),
        ("baglanti koptu", "belirsiz", requests.exceptions.ConnectionError("Connection aborted.")),
        ("baglanti zaman asimi", "gitmedi", requests.exceptions.ConnectTimeout("connect timed out")),
        ("DNS / baglanti reddi", "gitmedi", requests.exceptions.ConnectionError("Max retries exceeded (Caused by NewConnectionError('x'))")),
        ("HTTP 503", "belirsiz", (503, {"code": -1, "msg": "Service unavailable"})),
        ("-1007 zaman asimi", "belirsiz", (400, {"code": -1007, "msg": "Timeout waiting for response from backend server."})),
        ("-1006 beklenmeyen cevap", "belirsiz", (400, {"code": -1006, "msg": "An unexpected response was received from the message bus."})),
        ("-1000 bilinmeyen", "belirsiz", (400, {"code": -1000, "msg": "An unknown error occurred"})),
        ("-2010 Duplicate", "belirsiz", (400, {"code": -2010, "msg": "Duplicate order sent."})),
        ("-2010 bakiye", "red", (400, {"code": -2010, "msg": "Account has insufficient balance for requested action."})),
        ("-1013 filtre", "red", (400, {"code": -1013, "msg": "Filter failure: NOTIONAL"})),
        ("-2015 anahtar", "red", (401, {"code": -2015, "msg": "Invalid API-key, IP, or permissions for action."})),
        ("-1021 zaman damgasi", "gitmedi", (400, {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."})),
        ("429 hiz siniri", "gitmedi", (429, {"code": -1003, "msg": "Too many requests."})),
        ("-1015 cok emir", "gitmedi", (400, {"code": -1015, "msg": "Too many new orders."})),
        ("bozuk JSON degil ama anlamsiz kod", "belirsiz", (400, {"code": -9999, "msg": "?"}))):
    rx.session.script = [x]
    try:
        rx.create_order("BTC/USDT", "market", "buy", 1.0, None, {"newClientOrderId": "kqbL1-x"})
        check(f"gercek ccxt {name}: hata bekleniyordu", False)
    except Exception as e:
        check(f"gercek ccxt {name} -> {want}", B.order_fail(e) == want, (type(e).__name__, B.order_fail(e)))


class Hybrid(FakeEx):
    """Emir ve sorgu gercek ccxt kodundan (sahte oturum), gerisi sahte."""

    def __init__(self):
        super().__init__()
        self.rx = real()

    def create_order(self, *x, **k):
        return self.rx.create_order(*x, **k)

    def fetch_order(self, *x, **k):
        return self.rx.fetch_order(*x, **k)

    def fetch_my_trades(self, *x, **k):
        return self.rx.fetch_my_trades(*x, **k)


FULL = {"symbol": "BTCUSDT", "orderId": 7, "orderListId": -1, "clientOrderId": "x", "transactTime": 1700000000000, "price": "0.00000000", "origQty": "1.00000", "executedQty": "1.00000",
        "cummulativeQuoteQty": "100.0", "status": "FILLED", "timeInForce": "GTC", "type": "MARKET", "side": "BUY",
        "fills": [{"price": "100.0", "qty": "0.5", "commission": "0.0004", "commissionAsset": "BTC", "tradeId": 1}, {"price": "100.0", "qty": "0.5", "commission": "0.0004", "commissionAsset": "BTC", "tradeId": 2}]}
STOPOK = {"symbol": "BTCUSDT", "orderId": 8, "clientOrderId": "y", "transactTime": 1700000000000, "price": "65", "origQty": "1", "executedQty": "0", "cummulativeQuoteQty": "0", "status": "NEW", "type": "STOP_LOSS_LIMIT", "side": "SELL", "fills": []}
GOT = {"symbol": "BTCUSDT", "orderId": 7, "clientOrderId": "x", "price": "0", "origQty": "1", "executedQty": "1", "cummulativeQuoteQty": "100", "status": "FILLED", "type": "MARKET", "side": "BUY", "time": 1700000000000, "updateTime": 1700000000000, "isWorking": True, "stopPrice": "0"}
TRADE = {"symbol": "BTCUSDT", "id": 1, "orderId": 7, "price": "100.0", "qty": "1", "quoteQty": "100", "commission": "0.0002", "commissionAsset": "BNB", "time": 1700000000000, "isBuyer": True, "isMaker": False, "isBestMatch": True}

hx = Hybrid()
a, ex = live(hx)
hx.rx.session.script = [(200, FULL), (200, STOPOK)]
r = a.enter("BTC/USDT", plan())
p = a.st["positions"].get("BTC/USDT")
calls = hx.rx.session.calls
check("gercek ccxt: dolum, FULL cevaptaki parcali komisyon toplanir", r is None and p and abs(p["qty"] - 0.9992) < 1e-9 and abs(p["fee"] - 0.08) < 1e-9, (r, p and p["qty"], p and p["fee"]))
check("gercek ccxt: POST govdesinde kimlik, stopta da", "newClientOrderId=kqbL" in calls[0][2] and "newClientOrderId=kqbL" in calls[1][2] and "STOP_LOSS_LIMIT" in calls[1][2] and p["stop_id"] == "8", calls[1][2][:120])

hx = Hybrid()
a, ex = live(hx)
hx.rx.session.script = [requests.exceptions.ReadTimeout("Read timed out"), (200, GOT), (200, [TRADE]), (200, STOPOK)]
r = a.enter("BTC/USDT", plan())
p = a.st["positions"].get("BTC/USDT")
calls = hx.rx.session.calls
cid = re.search(r"newClientOrderId=([^&]+)", calls[0][2]).group(1)
check("gercek ccxt: zaman asimi -> GET origClientOrderId -> myTrades orderId; ikinci POST yalniz stop", r is None and calls[1][0] == "GET" and f"origClientOrderId={cid}" in calls[1][1] and "myTrades" in calls[2][1] and "orderId=7" in calls[2][1]
      and [c[0] for c in calls].count("POST") == 2 and "STOP_LOSS_LIMIT" in calls[3][2], [c[1][:60] for c in calls])
check("gercek ccxt: sorgudan sonra BNB komisyonu: miktar tam, ucret kurla", p and p["qty"] == 1.0 and abs(p["fee"] - 0.0002 * 500) < 1e-9, p and (p["qty"], p["fee"]))
hx = Hybrid()
a, ex = live(hx)
hx.rx.session.script = [requests.exceptions.ReadTimeout("Read timed out"), (400, {"code": -2013, "msg": "Order does not exist."})]
r = a.enter("BTC/USDT", plan())
check("gercek ccxt: -2013 'yok' -> kayit kalir, ayni anda yeniden gonderim yok", a.st["orders"] and "BTC/USDT" not in a.st["positions"] and [c[0] for c in hx.rx.session.calls] == ["POST", "GET"], r)

# ---- kilitlenme: gonderim sirasinda surec oldu ----
a, ex = live()
ex.plan = ["landed"]
def boom(cid):  # disk kaydindan sonra, cevaptan once surec olur
    raise SystemExit
ex.on_send = boom
try:
    a.enter("BTC/USDT", plan())
except SystemExit:
    pass
saved = disk()
check("kilitlenme: diskte 'yolda' kaydi, pozisyon yok", saved["orders"] and not saved["positions"], saved["orders"])
ex.on_send = None
ex.plan = []
cid = next(iter(saved["orders"]))
ex._mk(cid, "BTC/USDT", "market", "buy", 1.0, None)  # emir aslinda borsaya ulasmis
sent0 = len(ex.sent)
a2, _ = live(ex, saved)
check("yeniden baslama: kilit defterde sadece bekleyen emir varken de alinir", a2.own)
a2.reconcile()
p = a2.st["positions"].get("BTC/USDT")
check("yeniden baslama: dolum bulundu, pozisyon + stop, emir tekrar gitmedi", p and p["stop_id"] and not a2.st["orders"] and not [x for x in ex.sent[sent0:] if x["typ"] == "market"], ex.sent[sent0:])
check("sonra diskte de temiz", not disk()["orders"] and "BTC/USDT" in disk()["positions"])

a, ex = live()
C_ = C.LIVE_ALLOWED
C.LIVE_ALLOWED = False  # izin kapali, bos defter, tek kayit bekleyen emir
a3, _ = live(ex, {"orders": {"kqbL1-z": {"s": "BTC/USDT", "typ": "market", "side": "buy", "qty": 1.0, "price": None, "t": time.time() - 3600, "act": {"do": "enter", "p": plan()}}}})
check("izin sonradan kapatilsa da bekleyen emir sorgulanir (kilit alinir)", a3.own)
a3.reconcile()
check("hic ulasmamis eski emir: temizlenir", not a3.st["orders"] and ex.queries)
C.LIVE_ALLOWED = C_

a, ex = live()
ex.plan = ["landed"]
saved = {"positions": {"BTC/USDT": pos(1.0)}, "oseq": 5}
a4, ex = live(ex, saved)
a4.exit("BTC/USDT", a4.st["positions"]["BTC/USDT"], "HEDEF")
check("sira numarasi diskten devam eder", ex.sent[0]["cid"].startswith("kqbL6-"), ex.sent[0]["cid"])
ex.plan = []

# ---- belirsiz satis: cift satis yok ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.plan = ["late"]
ex.fee = {"currency": "USDT", "rate": 0.001}
a.b.bid["BTC/USDT"] = 60.0
ex.last = 60.0
for i in range(4):
    a.manage_pending()
    a.manage_positions()
sells = [x for x in ex.sent if x["side"] == "sell" and x["typ"] == "market"]
check("stop altinda belirsiz satis: her saniye yeniden satilmaz", len(sells) == 1 and "BTC/USDT" in a.st["positions"], len(sells))
check("belirsiz satista koruma stopu da gonderilmez (cift satis olmasin)", not [x for x in ex.sent if x["typ"] == "STOP_LOSS_LIMIT"], ex.sent)
ex.land()
age(a)
a.manage_pending()
t = a.st["trades"][-1:]
check("satis bulununca tek kapanis, gercek komisyon", t and t[0]["reason"] == "STOP" and "BTC/USDT" not in a.st["positions"] and a.st["wins"] + a.st["losses"] == 1 and abs(t[0]["pnl"] - (-40.0 - 0.06)) < 1e-9, t)
a.manage_pending()
a.manage_positions()
check("kapanis tekrar islenmez, ek emir yok", a.st["wins"] + a.st["losses"] == 1 and len([x for x in ex.sent if x["side"] == "sell"]) == 1)

a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.plan = ["lost"]
a.b.bid["BTC/USDT"] = 60.0
ex.last = 60.0
a.manage_positions()
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
a.manage_positions()
sells = [x for x in ex.sent if x["side"] == "sell" and x["typ"] == "market"]
check("satis borsaya ulasmamis: sure dolunca yeni kimlikle satilir", len(sells) == 2 and sells[0]["cid"] != sells[1]["cid"] and "BTC/USDT" not in a.st["positions"], [x["cid"] for x in sells])

# ---- belirsiz durumda stop bosuna iptal edilmez ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id="st1")
a.st["orders"]["kqbL1-z"] = {"s": "BTC/USDT", "typ": "market", "side": "buy", "qty": 1.0, "price": None, "t": time.time(), "act": {"do": "add", "why": "t"}}
try:
    a.pf_sell("BTC/USDT", 30.0, "t")
    a.partial("BTC/USDT", p, 100.0)
except B.OrderUnknown:
    pass
check("belirsiz alim varken koruma disi satis stopu sokmez", not ex.cancels and p["stop_id"] == "st1" and not ex.sent, ex.cancels)
del a.st["orders"]["kqbL1-z"]
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id="st1")
a.st["orders"]["kqbL1-z"] = {"s": "BTC/USDT", "typ": "market", "side": "sell", "qty": 1.0, "price": None, "t": time.time(), "act": {"do": "sell"}, "free": 1.0}
try:
    a.exit("BTC/USDT", p, "HEDEF")
except B.OrderUnknown:
    pass
check("yolda olabilecek satis varken cikis denemesi borsa stopunu iptal etmez", not ex.cancels and p["stop_id"] == "st1" and not ex.sent, ex.cancels)


# ---- bulgu: koruma cikisi kilide takilmaz ----
def msells(ex):
    return [x for x in ex.sent if x["side"] == "sell" and x["typ"] == "market"]


def mstops(ex):
    return [x for x in ex.sent if x["typ"] == "STOP_LOSS_LIMIT"]


S = "BTC/USDT"
a, ex = live()
p = a.st["positions"][S] = pos(1.0)
p["stop_id"] = a.place_stop(S, p)
ex.plan = ["lost"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
r = a.pf_buy(S, 50.0, "t")
check("belirsiz alim + sorgu 503: stop geri kuruldu", r and p["stop_id"] and a.st["orders"], r)
a.b.bid[S] = ex.last = 40.0
p["stop_wait"], p["stop_chk"] = 0, time.time()
age(a, 10)
a.manage_pending()
a.manage_positions()
check("belirsiz alim + sorgu 503 + bosluk: yazilim stopu hemen piyasadan satar (120 sn yok)", len(msells(ex)) == 1 and S not in a.st["positions"] and a.st["trades"][-1]["reason"] == "STOP", msells(ex))
a.b.ask[S] = 40.0
check("belirsiz alim kaydi durur: yeni alim hala kilitli", a.st["orders"] and "bilinmiyor" in a.enter(S, dict(plan(), stop=30.0)))
ex.query_raises = None
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
check("alim gitmemis cikti: temiz", not a.st["orders"] and S not in a.st["positions"])

a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["late"]
a.pf_buy(S, 50.0, "t")
a.b.bid[S] = ex.last = 40.0
p["stop_wait"] = 0
a.manage_positions()
check("belirsiz alim, sorgu saglam: t=0'da satar", len(msells(ex)) == 1 and S not in a.st["positions"])
ex.land()
age(a)
a.manage_pending()
p = a.st["positions"].get(S)
check("alim sonradan bulundu, pozisyon satilmisti: yeni pozisyon + felaket stopu", p and p["kind"] == PF.KIND and p["stop_id"] and not a.st["orders"], p and p["stop_id"])

a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["lost"]
p["stop_id"] = a.place_stop(S, p)
a.b.bid[S] = ex.last = 40.0
p["stop_chk"] = time.time()
a.manage_positions()
check("sadece koruma stopu belirsiz: stop altinda piyasa satisi gider", len(msells(ex)) == 1 and S not in a.st["positions"], msells(ex))
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["late"]
p["stop_id"] = a.place_stop(S, p)
check("koruma stopu belirsizken panik calisir", a.close(S, "ELLE_KAPATILDI") is True and len(msells(ex)) == 1 and a.st["trades"][-1]["reason"] == "ELLE_KAPATILDI")
n = len(ex.cancels)
ex.land()
age(a)
a.reconcile()
check("sonradan gorunen belirsiz stop: sahipsiz kalmaz, iptal edilir", len(ex.cancels) == n + 1 and not a.st["orders"], ex.cancels)

a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["late"]
a.place_stop(S, p)
ex.free = 0.0  # stop borsada, coinler kilitli, sorgu henuz gormuyor
try:
    a.exit(S, p, "HABER_ACIL")
    check("coinler belirsiz stopta: hata beklenirdi", False)
except B.OrderUnknown:
    pass
check("coinler belirsiz stopta: satildi sayilmaz, pozisyon defterde", S in a.st["positions"] and not msells(ex) and not a.st["trades"])
ex.land()
ex.free = 1.0
age(a)
p["exit_try"] = 0
a.manage_positions()
check("belirsiz stop bulununca: iptal, sonra karar verilen satis", S not in a.st["positions"] and len(ex.cancels) == 1 and len(msells(ex)) == 1 and a.st["trades"][-1]["reason"] == "HABER_ACIL", ex.cancels)

# ---- bulgu: stop iptal + belirsiz satis: en cok ORDER_HOLD_SEC korumasiz ----
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
p["stop_id"] = a.place_stop(S, p)
ex.plan = ["lost"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
try:
    a.close(S, "HABER_ACIL")
except B.OrderUnknown:
    pass
check("cikis belirsiz: stop iptal, tek satis", p["stop_id"] is None and len(msells(ex)) == 1 and a.st["orders"])
n = len(mstops(ex))
p["stop_chk"], p["exit_try"] = 0, time.time()
a.manage_positions()
check("yolda olabilir (ORDER_HOLD_SEC): ikinci satis ve stop gitmez, deneme hakki yenmez", len(mstops(ex)) == n and len(msells(ex)) == 1 and not p.get("stop_try"), p.get("stop_try"))
age(a, B.ORDER_HOLD_SEC + 1)
p["stop_chk"], p["exit_try"] = 0, time.time()
a.manage_positions()
check("sorgu bozuk, sure doldu, bakiye dusmedi: stop yeniden kurulur", p["stop_id"] and len(mstops(ex)) == n + 1 and abs(mstops(ex)[-1]["qty"] - 1.0) < 1e-9, p["stop_id"])
p["exit_try"] = 0
a.manage_positions()
check("sorgu bozuk, sure doldu: karar verilen satis yeniden gider, stop once iptal", len(msells(ex)) == 2 and S not in a.st["positions"] and a.st["trades"][-1]["reason"] == "HABER_ACIL", msells(ex))

a, ex = live()
ex.free = 15.0  # hesapta bot disi 5 coin de var
a.st["positions"][S] = p = pos(10.0)
ex.plan = ["landed"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
try:
    a.exit(S, p, "HABER_ACIL")
except B.OrderUnknown:
    pass
ex.free = 5.0  # ilk satis aslinda doldu, sorgu goremiyor
age(a, 3600)
for _ in range(3):
    p["exit_try"], p["stop_chk"] = 0, 0
    a.manage_positions()
check("ilk satis doldu + sorgu bozuk: hesaptaki bot disi coin satilmaz, stop kurulmaz", len(msells(ex)) == 1 and not mstops(ex) and S in a.st["positions"], [x["qty"] for x in msells(ex)])
ex.query_raises = None
age(a)
a.manage_pending()
check("sorgu duzelince gercek dolumla tek kapanis", S not in a.st["positions"] and len(a.st["trades"]) == 1 and not a.st["orders"])

a, ex = live()
ex.free = 15.0
a.st["positions"][S] = p = pos(10.0)
ex.plan = ["lost"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
try:
    a.exit(S, p, "HABER_ACIL")
except B.OrderUnknown:
    pass
age(a, B.ORDER_HOLD_SEC + 1)
p["exit_try"] = 0
a.manage_positions()
check("ilk satis gitmemis + bot disi coin: ikinci satis sadece pozisyon kadar", len(msells(ex)) == 2 and abs(msells(ex)[1]["qty"] - 10.0) < 1e-9 and S not in a.st["positions"], [x["qty"] for x in msells(ex)])
ex.query_raises = None
ex.venue[msells(ex)[0]["cid"]] = {"id": "v77", "status": "closed", "filled": 10.0, "average": 100.0, "fee": None, "fees": []}
age(a)
a.manage_pending()
check("ilk satis yine de dolu cikti: kullanici uyarilir", any("kâr/zarar" in m and k == "uyari" for k, m in a.b.notes), a.b.notes[-1:])

a, ex = live()
a.st["positions"][S] = p = pos(10.0)
a.st["orders"]["kqbL1-a"] = {"s": S, "typ": "STOP_LOSS_LIMIT", "side": "sell", "qty": 10.0, "price": 64.0, "t": time.time(), "act": {"do": "stop", "sp": 65.0}, "free": 10.0}
ex.venue["kqbL1-a"] = {"id": "v8", "clientOrderId": "kqbL1-a", "status": "open", "filled": 0.0, "average": None, "fee": None, "fees": []}
p["stop_id"] = "st2"
a.reconcile()
check("gec bulunan stop, yeni stopun yerini almaz: iptal edilir", p["stop_id"] == "st2" and ex.cancels == ["v8"], (p["stop_id"], ex.cancels))

# ---- bulgu: kayit silinince stop hemen kurulur ----
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
p["stop_id"] = a.place_stop(S, p)
ex.plan = ["lost"]
try:
    a.close(S, "HABER_ACIL")
except B.OrderUnknown:
    pass
p["exit_try"] = time.time()
p["stop_chk"] = 0
a.manage_positions()  # kilitliyken deneme: dakikalik hak yenmez
n = len(mstops(ex))
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
check("gitmemis sayildi: kayit silindi", not a.st["orders"])
a.manage_positions()
check("ayni dongude stop kurulur (60 sn bekleme yok)", len(mstops(ex)) == n + 1 and p["stop_id"], len(mstops(ex)) - n)

a, ex = live()
a.st["positions"][S] = p = pos(1.0)
p["stop_id"] = a.place_stop(S, p)
n = len(mstops(ex))
ex.plan = ["lost"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
try:
    a.close(S, "HABER_ACIL")
except B.OrderUnknown:
    pass
p["exit_try"], p["stop_chk"] = time.time(), 0
a.manage_positions()  # kilitli deneme: 15 sn'lik kontrol hakki yenmez
age(a, B.ORDER_HOLD_SEC + 1)
a.manage_positions()  # stop_chk elle sifirlanmadi
check("kilit biter bitmez stop kurulur (15 sn'lik kontrol bir tur daha beklemez)", len(mstops(ex)) == n + 1 and p["stop_id"], len(mstops(ex)) - n)

a, ex = live()
a.st["positions"][S] = p = pos(10.0)
p["kind"] = None  # kademeli sadece strateji disi pozisyonda calisir
a.b.bid[S] = ex.last = 110.0
ex.plan = ["landed"]
ex.query_raises = ccxt.ExchangeNotAvailable("503")
try:
    a.partial(S, p, 110.0)
except B.OrderUnknown:
    pass
ex.free -= ex.sent[0]["qty"]
age(a, B.ORDER_HOLD_SEC + 1)
a.manage_positions()
st = mstops(ex)[-1]
check("kademeli doldu + sorgu bozuk: ara stop sadece kalan miktarda", abs(st["qty"] - (10.0 - ex.sent[0]["qty"])) < 1e-6 and p["stop_id"] and p["qty"] == 10.0, st["qty"])
ex.query_raises = None
age(a)
a.manage_pending()
bep = L.be_price(100.0, 1.0)
check("sorgu duzeldi: defter kalan miktar, stop basabasa", abs(p["qty"] - (10.0 - ex.sent[0]["qty"])) < 1e-6 and abs(p["sl"] - bep) < 1e-9 and p["be"] is True and not a.st["orders"], (p["qty"], p["sl"]))
a.manage_positions()
st = mstops(ex)[-1]
check("borsadaki ara stop en az basabasa tasinir (kalkan izi dahil), eski seviyede kalmaz", st["params"]["stopPrice"] >= round(bep, 2) and abs(st["params"]["stopPrice"] - p["sl"]) < 0.01 and abs(st["qty"] - p["qty"]) < 1e-6 and len(ex.cancels) == 1, st["params"])

# ---- bulgu: tek seferlik karar kaybolmaz, 'satildi' dogru ----
import haberler as HB


class HBot:
    def __init__(self, acct):
        self.accounts, self.lock, self.bid, self.notes = {"LIVE": acct}, threading.RLock(), acct.b.bid, []
        self.stop = threading.Event()

    def say(self, msg, *x, **k):
        self.notes.append(msg)

    def _publish(self, *x, **k):
        pass


C.HABER_LIVE = True
item = lambda: {"title": "test", "key": "k1", "src": "x", "ent": "x", "fam": "x", "yerel": -1.0, "t": time.time()}
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
i = item()
HB.emergency(HBot(a), i, "coin", S, time.time(), -0.02, 2)
check("HABER_ACIL tam dolum: satildi", i["ac"]["LIVE"] == "satıldı: BTC" and S not in a.st["positions"], i["ac"])
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["lost"]
i = item()
HB.emergency(HBot(a), i, "coin", S, time.time(), -0.02, 2)
check("HABER_ACIL belirsiz: 'satildi' denmez", "tamamlanamadı" in i["ac"]["LIVE"] and "satıldı" not in i["ac"]["LIVE"] and S in a.st["positions"], i["ac"])
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
p["exit_try"] = 0
a.manage_positions()
check("HABER_ACIL satisi gitmemis sayildi: karar yeniden denenir (fiyat stopun ustunde de)", S not in a.st["positions"] and a.st["trades"][-1]["reason"] == "HABER_ACIL" and len(msells(ex)) == 2, msells(ex))
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0)
a.st["positions"]["ETH/USDT"] = pos(1.0)
ex.fill_frac = 0.4
i = item()
HB.emergency(HBot(a), i, "piyasa", S, time.time(), -0.02, 2)
check("HABER_ACIL eksik dolum: 'satildi' denmez", "eksik" in i["ac"]["LIVE"] and "satıldı" not in i["ac"]["LIVE"], i["ac"])
ex.fill_frac = 1.0
p["exit_try"], p["stop_chk"] = 0, time.time()
a.manage_positions()
check("eksik dolumun kalani yeniden satilir", S not in a.st["positions"] and abs(msells(ex)[-1]["qty"] - p["qty"]) < 1e-9, msells(ex)[-1])
C.HABER_LIVE = False
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.fill_frac = 0.4
try:
    a.close(S, "CLAUDE_KAPATTI")
    check("close eksik dolumda True donmez", False)
except B.OrderUnknown as e:
    check("close eksik dolumda hata + kalan miktar", S in a.st["positions"] and "0.6" in str(e), e)
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["lost"]
a.b.bid[S] = ex.last = 40.0
try:
    a.exit(S, p, "STOP", stop=True)
except B.OrderUnknown:
    pass
check("yazilim stopu tek seferlik karar yazmaz (fiyat donerse _manage yeniden bakar)", not p.get("exit_want"))

import telegram_bot as TB
a, ex = live()
a.st["positions"][S] = pos(1.0)
ex.fill_frac = 0.4
TB.BOT, _ac = a.b, TB._accts
TB._accts = lambda role: [a]
msg = TB.do_panic("test", "admin")
TB._accts, TB.BOT = _ac, None
check("Telegram panik eksik dolum: kapatildi sayilmaz", "0 pozisyon" in msg and "eksik" in msg, msg)

class CBot(B.Bot):
    """Sadece Claude 'close' dali icin: motor kurulmaz, gercek apply calisir."""
    def __init__(self, acct):
        self.accounts, self.lock, self.market, self.ag = {"LIVE": acct}, threading.RLock(), {}, {"rejects": []}
        self.chain = type("X", (), {"whale": {}, "liq": {}})()
        self.macro = type("X", (), {"risk": lambda s: None})()

    def ctx(self):
        return {}


def claude_close(ff):
    a, ex = live()
    p = a.st["positions"][S] = pos(1.0)
    p["kind"] = None
    ex.fill_frac = ff
    r = CBot(a).apply({"type": "close", "symbol": S})
    return r, a


r, a = claude_close(1.0)
check("Claude kapatma tam dolum: kapatildi", r and r.get("result") == "kapatıldı" and S not in a.st["positions"], r and r.get("result"))
r, a = claude_close(0.4)
check("Claude kapatma eksik dolum: kapatildi denmez, kalan yeniden satilacak", r and "kapatıldı" not in r.get("result", "") and "eksik" in r.get("result", "") and a.st["positions"][S].get("exit_want") == "CLAUDE_KAPATTI", r and r.get("result"))

# ---- bulgu: deftere islenemeyen dolum coini kilitlemez ----
def step1(s, q):
    r = int(float(q))
    if r <= 0:
        raise ccxt.InvalidOrder("amount must be greater than minimum amount precision")
    return str(r)


a, ex = live()
a.b.ask[S] = a.b.bid[S] = ex.last = 10.0
p = a.st["positions"][S] = pos(10.0, entry=10.0, sl=6.5)
ex.amount_to_precision = step1
r = a.pf_buy(S, 10.0, "t")
check("artirma toza dustu (adim 1, komisyon coinden): kayit kapanir, miktar ayni, ucret islenir", r is None and not a.st["orders"] and p["qty"] == 10.0 and p["fee"] > 0 and any("adımının altında" in m and k == "uyari" for k, m in a.b.notes), r)
a, ex = live()
a.b.ask[S] = a.b.bid[S] = ex.last = 10.0
ex.free = 0.0
r = a.enter(S, dict(plan(100.0), stop=5.0))
check("alim doldu, coin elle satilmis (serbest 0): kayit kapanir, pozisyon yok, uyari", not a.st["orders"] and S not in a.st["positions"] and any("pozisyon açılmadı" in m for _, m in a.b.notes), r)
a, ex = live()
a.st["positions"][S] = p = pos(1.0)
ex.plan = ["late"]
a.pf_buy(S, 50.0, "t")
ex.land()
_grow = a._grow
def bad_grow(*x, **k):
    raise RuntimeError("bozuk defter")
a._grow = bad_grow
age(a)
a.manage_pending()
check("kalici defter hatasi: dolum kaybolmaz, kullanici uyarilir", a.st["orders"] and any("işlenemedi" in m and k == "uyari" for k, m in a.b.notes), a.b.notes[-1:])
check("kalici defter hatasi: yeni alim kilitli", "bilinmiyor" in a.enter(S, plan()))
a.b.bid[S] = ex.last = 40.0
p["stop_wait"] = 0
p["stop_chk"] = time.time()
age(a)
a.manage_positions()
check("kalici defter hatasi: cikis kilitlenmez", len(msells(ex)) == 1 and S not in a.st["positions"], msells(ex))

# ---- bulgu: sonradan bulunan kademeli satis ayni korumayi alir ----
a, ex = live()
a.st["positions"][S] = p = pos(10.0)
a.b.bid[S] = ex.last = 110.0
ex.plan = ["late"]
try:
    a.partial(S, p, 110.0)
except B.OrderUnknown:
    pass
ex.land()
age(a)
a.manage_pending()
bep = L.be_price(100.0, 1.0)
check("kademeli gec bulundu: stop basabasa, kalkan isareti (normal yolla ayni)", abs(p["sl"] - bep) < 1e-9 and p["be"] is True and p["part"], (p["sl"], p.get("be")))
a.manage_positions()
st = mstops(ex)[-1]
check("kademeli gec bulundu: borsa stopu basabasta, kalan miktarda", abs(st["params"]["stopPrice"] - round(bep, 2)) < 1e-9 and abs(st["qty"] - p["qty"]) < 1e-6, st["params"])
a, ex = live()
a.st["positions"][S] = p = pos(10.0)
a.b.bid[S] = ex.last = 110.0
p["partial_at"] = 105.0
ex.plan = ["lost"]
try:
    a.partial(S, p, 110.0)
except B.OrderUnknown:
    pass
age(a, B.ORDER_LOST_SEC + 1)
a.manage_pending()
check("kademeli satis gitmemis sayildi: yeniden denenebilir, miktar ve stop ayni", p["part"] is False and p["qty"] == 10.0 and p["sl"] == 65.0, p["part"])

# ---- bulgu: stop iptal sirasinda doldu: gercek komisyon her yolda ----
def fill_during_cancel(extra=None):
    """Borsa stopu iptal aninda doldu: iptal hata verir, emir 'closed', komisyon islem listesinde (tahminden farkli)."""
    a, ex = live()
    a.st["positions"][S] = p = pos(1.0, stop_id="stx")
    p["sl"] = p["stop_px"] = 90.0
    ex.venue["st"] = {"id": "stx", "clientOrderId": "st", "status": "closed", "filled": 1.0, "average": 90.0, "fee": None, "fees": []}
    ex.trades["stx"] = [{"fee": {"currency": "USDT", "cost": 0.07}}]
    def no_cancel(oid, s):
        raise ccxt.OrderNotFound("binance -2011 Unknown order sent")
    ex.cancel_order = no_cancel
    return a, ex, p


want = (90.0 - 100.0) - 0.07
for name, run in (("exit", lambda a, p: a.exit(S, p, "HEDEF")), ("kademeli", lambda a, p: a.partial(S, p, 100.0)), ("pf_sell", lambda a, p: a.pf_sell(S, 30.0, "t")),
                  ("pf_buy", lambda a, p: a.pf_buy(S, 50.0, "t")), ("_replace_stop", lambda a, p: a._replace_stop(S, p))):
    a, ex, p = fill_during_cancel()
    if name == "kademeli":
        p.update(qty=10.0, qty0=10.0)
        ex.venue["st"]["filled"] = 10.0
        want_ = 10 * (90.0 - 100.0) - 0.07
    else:
        want_ = want
    run(a, p)
    t = a.st["trades"][-1:]
    check(f"stop iptalde doldu ({name}): tek kapanis, dolum fiyati, gercek komisyon, ek emir yok", t and abs(t[0]["pnl"] - want_) < 1e-9 and t[0]["exit"] == 90.0 and S not in a.st["positions"] and not ex.sent, (t and t[0]["pnl"], ex.sent[:1]))

# ---- portfoy artirma: belirsiz ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
p["stop_id"] = a.place_stop("BTC/USDT", p)
old_stop = p["stop_id"]
ex.plan = ["late"]
r = a.pf_buy("BTC/USDT", 100.0, "agirlik")
stops = [x for x in ex.sent if x["typ"] == "STOP_LOSS_LIMIT"]
check("artirma belirsiz: iptal edilen felaket stopu hemen geri kurulur (belirsiz alimda serbest)", r and "bilinmiyor" in r and p["stop_id"] and p["stop_id"] != old_stop and len(stops) == 2, r)
check("defter hala eski miktar", p["qty"] == 1.0)
try:  # dengeleme bu hatayi yakalar (rebalance), stop yerinde kalir
    a.pf_buy("BTC/USDT", 100.0, "agirlik")
    check("ikinci artirma: hata beklenirdi", False)
except B.OrderUnknown:
    pass
check("ikinci artirma gonderilmez, stop sokulmez", len([x for x in ex.sent if x["typ"] == "market"]) == 1 and len(ex.cancels) == 1 and p["stop_id"])
ex.land()
age(a)
a.manage_pending()
stops = [x for x in ex.sent if x["typ"] == "STOP_LOSS_LIMIT"]
check("alim bulununca: miktar + ortalama + gercek komisyon, stop yeni toplam miktarda", abs(p["qty"] - 1.999) < 1e-9 and abs(p["fee"] - 0.1) < 1e-9 and abs(stops[-1]["qty"] - 1.999) < 1e-9 and p["stop_id"] and not a.st["orders"], (p["qty"], p["fee"], stops[-1]["qty"]))
check("eski stop iptal edildi", len(ex.cancels) == 2, ex.cancels)

a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.fee = {"currency": "BNB", "cost": 0.0002}
r = a.pf_buy("BTC/USDT", 100.0, "agirlik")
check("artirma BNB komisyonu: miktar tam, ucret kurla (sabit oran yok)", r is None and p["qty"] == 2.0 and abs(p["fee"] - 0.1) < 1e-9 and p["stop_id"], (r, p["qty"], p["fee"]))

a, ex = live()
a.st["orders"]["kqbL1-z"] = {"s": "BTC/USDT", "typ": "market", "side": "buy", "qty": 1.0, "price": None, "t": time.time(), "act": {"do": "add", "why": "t"}}
ex.venue["kqbL1-z"] = {"id": "v1", "clientOrderId": "kqbL1-z", "status": "closed", "filled": 1.0, "average": 100.0, "fee": None, "fees": []}
a.reconcile()
p = a.st["positions"].get("BTC/USDT")
check("artirma bulundu ama pozisyon stopla kapanmis: yeni pozisyon + felaket stopu", p and p["kind"] == PF.KIND and abs(p["sl"] - 100.0 * PF.STOP_FRAC) < 1e-6 and p["stop_id"], p and p["sl"])

# ---- kademeli satis / portfoy azaltma: belirsiz + eksik dolum ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id=None)
p["partial_at"] = 100.0
ex.fee = {"currency": "USDT", "rate": 0.0005}
a.partial("BTC/USDT", p, 100.0)
n = 10.0 * C.PARTIAL_FRAC
check("kademeli satis: gercek komisyon, kalan miktar, stop kalan miktarda", abs(p["qty"] - (10.0 - n)) < 1e-6 and abs(p["realized"] + n * 100 * 0.0005) < 1e-9 and ex.sent[-1]["typ"] == "STOP_LOSS_LIMIT" and abs(ex.sent[-1]["qty"] - p["qty"]) < 1e-6, (p["qty"], p["realized"]))
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id=None)
p["partial_at"] = 100.0
ex.plan = ["late"]
try:
    a.partial("BTC/USDT", p, 100.0)
    check("kademeli belirsiz: hata beklenirdi", False)
except B.OrderUnknown:
    pass
check("kademeli belirsiz: defter ayni, tekrar denenmez isareti", p["qty"] == 10.0 and p["part"])
ex.land()
age(a)
a.manage_pending()
check("kademeli bulundu: kalan dogru, pozisyon acik", abs(p["qty"] - (10.0 - n)) < 1e-6 and not a.st["orders"] and "BTC/USDT" in a.st["positions"], p["qty"])
a.manage_positions()
check("kademeli bulundu: stop bir sonraki dongude kuruldu", p["stop_id"] and ex.sent[-1]["typ"] == "STOP_LOSS_LIMIT" and abs(ex.sent[-1]["qty"] - p["qty"]) < 1e-6, ex.sent[-1])
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id=None)
ex.fee = {"currency": "USDT", "rate": 0.001}
r = a.pf_sell("BTC/USDT", 300.0, "agirlik")
check("portfoy azaltma: gercek komisyon", r is None and abs(p["qty"] - 7.0) < 1e-6 and abs(p["realized"] + 0.3) < 1e-9, (p["qty"], p["realized"]))
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id=None)
ex.fill_frac = 0.5
ex.fee = {"currency": "USDT", "rate": 0.001}
r = a.pf_sell("BTC/USDT", 300.0, "agirlik")
check("azaltma eksik doldu: sadece dolan kisim duser", r is None and abs(p["qty"] - 8.5) < 1e-6 and abs(p["realized"] + 0.15) < 1e-9, (p["qty"], p["realized"]))
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(10.0, stop_id=None)
ex.fill_frac = 0.4
ex.fee = {"currency": "USDT", "rate": 0.001}
a.exit("BTC/USDT", p, "PORTFOY_CIKIS")
check("tam cikis eksik doldu: pozisyon kapanmis sayilmaz, kalan korunur", "BTC/USDT" in a.st["positions"] and abs(p["qty"] - 6.0) < 1e-6 and not a.st["trades"], (p["qty"], a.st["trades"][-1:]))
a.manage_positions()
check("kalan icin stop yeniden kurulur", p["stop_id"] and ex.sent[-1]["typ"] == "STOP_LOSS_LIMIT" and abs(ex.sent[-1]["qty"] - 6.0) < 1e-6, ex.sent[-1])

# ---- piyasa emri dolmadi ----
a, ex = live()
ex.plan = ["expired"]
r = a.enter("BTC/USDT", plan())
check("piyasa emri dolmadan bitti: pozisyon yok, kayit yok, 'dolmadı'", "BTC/USDT" not in a.st["positions"] and not a.st["orders"] and r and "dolmadı" in r, r)
a, ex = live()
ex.plan = ["ack"]
r = a.enter("BTC/USDT", plan())
p = a.st["positions"].get("BTC/USDT")
check("cevapta dolum bilgisi yok: varsayilmaz, kimlikle sorulur", r is None and p and ex.queries and abs(p["qty"] - 0.999) < 1e-9, (r, p and p["qty"]))

a, ex = live()
ex.plan = ["ack"]
_mk = ex._mk
def _mk_open(cid, *x, **k):  # piyasa emri borsada hala islemde
    o = _mk(cid, *x, **k)
    ex.venue[cid].update(status="open", filled=0.4)
    return o
ex._mk = _mk_open
r = a.enter("BTC/USDT", plan())
check("piyasa emri hala islemde: kismi dolum nihai sayilmaz, kayit kalir", "bilinmiyor" in r and a.st["orders"] and "BTC/USDT" not in a.st["positions"], r)
ex.venue[ex.sent[0]["cid"]].update(status="closed", filled=1.0)
age(a)
a.manage_pending()
check("islem bitince tam miktar", abs(a.st["positions"]["BTC/USDT"]["qty"] - 0.999) < 1e-9 and not a.st["orders"], a.st["positions"]["BTC/USDT"]["qty"])

# ---- borsa stopu: belirsiz ----
a, ex = live()
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
ex.plan = ["late"]
check("stop belirsiz: None doner, kayit kalir", a.place_stop("BTC/USDT", p, quiet=True) is None and a.st["orders"])
check("stop belirsizken ikinci stop gitmez", a.place_stop("BTC/USDT", p, quiet=True) is None and len(ex.sent) == 1)
ex.land()
age(a)
a.reconcile()
check("stop bulununca pozisyona baglanir, yeni stop kurulmaz", p["stop_id"] == ex.venue[ex.sent[0]["cid"]]["id"] and p["stop_px"] and len(ex.sent) == 1 and not a.st["orders"], p["stop_id"])
a, ex = live()
a.st["orders"]["kqbL1-z"] = {"s": "BTC/USDT", "typ": "STOP_LOSS_LIMIT", "side": "sell", "qty": 1.0, "price": 65.0, "t": time.time(), "act": {"do": "stop", "sp": 65.0}}
ex.venue["kqbL1-z"] = {"id": "v5", "clientOrderId": "kqbL1-z", "status": "open", "filled": 0.0, "average": None, "fee": None, "fees": []}
a.reconcile()
check("sahipsiz stop (pozisyon kapanmis): iptal edilir", ex.cancels == ["v5"], ex.cancels)
a, ex = live()
ex.plan = ["late"]
r = a.enter("BTC/USDT", plan(entry="maker"))
check("limit belirsiz: bekleyen yok, kayit var", "bilinmiyor" in r and not a.st["pending"] and a.st["orders"], r)
ex.land()
age(a)
a.manage_pending()
check("limit bulununca bekleyen emre girer, borsa numarasiyla", a.st["pending"]["BTC/USDT"]["id"] == ex.venue[ex.sent[0]["cid"]]["id"] and not a.st["orders"])

# ---- defter islenemezse dolum kaybolmaz ----
a, ex = live()
_open, n = a.open, []
def bad_open(*x, **k):
    n.append(1)
    if len(n) == 1:
        raise RuntimeError("disk")
    return _open(*x, **k)
a.open = bad_open
r = a.enter("BTC/USDT", plan())
check("dolum oldu, defter hata verdi: kayit kalir, 'reddetti' denmez", a.st["orders"] and r and "reddetti" not in r and "işlenemedi" in r, r)
age(a)
a.manage_pending()
check("sonraki dongu isler, emir tekrar gitmez", "BTC/USDT" in a.st["positions"] and not a.st["orders"] and len([x for x in ex.sent if x["typ"] == "market"]) == 1)

# ---- disk yazilamiyor ----
a, ex = live()
def no_disk():
    raise OSError(28, "No space left on device")
a.b.save = no_disk
r = a.enter("BTC/USDT", plan())
check("kayit yazilamadi: alim gitmez", not ex.sent and r and "yazılamadı" in r and not a.st["orders"], r)
a.st["positions"]["BTC/USDT"] = p = pos(1.0, stop_id=None)
a.exit("BTC/USDT", p, "HEDEF")
check("kayit yazilamadi: satis engellenmez, uyari", ex.sent and ex.sent[0]["side"] == "sell" and "BTC/USDT" not in a.st["positions"] and any("yazılamadı" in m for _, m in a.b.notes))

# ---- is parcacigi: gonderim surerken mutabakat emre dokunmaz ----
a, ex = live()
ex.venue_seen = None
def during(cid):
    if ex.venue_seen is not None:
        return
    ex._mk(cid, "BTC/USDT", "market", "buy", 1.0, None)
    ex.plan = ["ok"]
    a.reconcile()  # baska is parcacigindan dongu
    ex.venue_seen = ("BTC/USDT" in a.st["positions"], len(ex.queries))
ex.on_send = during
r = a.enter("BTC/USDT", plan())
ex.on_send = None
check("gonderim surerken mutabakat sorgulamaz, cift isleme yok", ex.venue_seen == (False, 0) and r is None and abs(a.st["positions"]["BTC/USDT"]["qty"] - 0.999) < 1e-9, ex.venue_seen)

# ---- Bot.save: kayit kilit icinde, durum dosyasi bozulmaz ----
a, ex = live()
errs = []
def saver():
    for _ in range(60):
        try:
            a.b.save()
        except Exception as e:
            errs.append(e)
ths = [threading.Thread(target=saver) for _ in range(4)]
[t.start() for t in ths]
[t.join() for t in ths]
check("es zamanli kayit: hata yok, dosya gecerli", not errs and "LIVE" in json.load(open(C.STATE_FILE, encoding="utf-8")), errs[:1])

# ---- tek kapi ----
src = open(os.path.join(PROJ, "bot.py"), encoding="utf-8").read()
check("create_order tek yerde", len(re.findall(r"\.create_order\(", src)) == 1)
check("gercek hesap emir kodunda sabit oran alim miktarina uygulanmiyor", "(1 - C.FEE), free" not in src and 'o["filled"] or qty) * (1 - C.FEE)' not in src)
cfg = open(os.path.join(PROJ, "config.py"), encoding="utf-8").read()
check("varsayilan LIVE kapali kaldi", re.search(r"^LIVE_ALLOWED = False", cfg, re.M) and re.search(r"^PORTFOY_LIVE = False", cfg, re.M))
check("eski durum dosyasi (orders alani yok) acilir", (lambda x: x.st["orders"] == {} and x.st["oseq"] == 0)(live(saved={"cash": 5.0, "positions": {}})[0]))

# ---- sanal hesap muhasebesi: birebir ayni ----


def paper_run(mod):
    """Ayni senaryo verilen bot modulunde: alis, limit, artirma, kademeli, azaltma, stop, tam cikis."""
    ex = FakeEx()
    ex.create_order = _no_net  # sanal hesap borsaya emir veremez
    ex.fetch_ohlcv = lambda *x, **k: []
    stub = Stub(ex)
    stub.market = {}
    pa = mod.Account(stub, "PAPER", {})
    pa.st["cash"] = 10_000.0
    out = []
    px = {"BTC/USDT": 100.0, "ETH/USDT": 200.0}

    def snap():
        st = pa.st
        out.append(round(st["cash"], 8))
        out.append(round(st["pnl"], 8))
        out.append({s: {k: round(p[k], 8) for k in ("qty", "qty0", "entry", "fee", "realized", "sl", "tp")} for s, p in st["positions"].items()})
        out.append([(t["sym"], t["reason"], round(t["entry"], 8), round(t["exit"], 8), round(t["pnl"], 8), round(t["pct"], 8)) for t in st["trades"]])
        out.append({s: (p["qty"], p["price"]) for s, p in st["pending"].items()})

    stub.bid.update(px)
    stub.ask.update(px)
    assert pa.enter("BTC/USDT", plan(1000.0)) is None
    snap()
    assert pa.pf_buy("BTC/USDT", 500.0, "t") is None
    snap()
    assert pa.pf_sell("BTC/USDT", 300.0, "t") is None
    snap()
    p = pa.st["positions"]["BTC/USDT"]
    p["partial_at"], p["exit"] = 100.0, {}
    pa.partial("BTC/USDT", p, 110.0)
    snap()
    pa.exit("BTC/USDT", p, "HEDEF")
    snap()
    assert pa.enter("ETH/USDT", dict(plan(400.0, "maker"), symbol="ETH/USDT", stop=150.0)) is None
    snap()
    stub.ask["ETH/USDT"] = 199.0
    pa.manage_pending()
    snap()
    p = pa.st["positions"]["ETH/USDT"]
    stub.bid["ETH/USDT"] = p["sl"] * 0.999
    pa.exit("ETH/USDT", p, "STOP", stop=True)
    snap()
    out.append(sorted(k for k in pa.st if k not in ("orders", "oseq", "mut", "toz", "dis")))  # sadece canli alanlar; muhasebe noktalari ayni kalmali
    return out, pa


new, pa = paper_run(B)
check("sanal hesap: emir kaydi hic olusmaz", pa.st["orders"] == {} and pa.st["oseq"] == 0)
qty = 10.0
check("sanal alis formulu ayni (nakit - tutar - komisyon, kayma dahil)", abs(new[0] - (10_000.0 - qty * 100 * (1 + C.SLIPPAGE) * (1 + C.FEE))) < 1e-6, new[0])
old_fn = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_oncesi_emir.py")
if os.path.exists(old_fn):
    spec = importlib.util.spec_from_file_location("bot_oncesi_emir", old_fn)
    OLD = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(OLD)
    old, _ = paper_run(OLD)
    diff = [(i, x, y) for i, (x, y) in enumerate(zip(old, new)) if x != y]
    # 2026-11-08 bilerek: sanal portfoy tam cikisi de kaymali (kismi satisla ayni). Fark sadece bu cikista (nakit + net), kayma kadar
    # Fark: HEDEF cikisindan (nokta 20) sonra nakit ve net sabit d kadar dusuk, o islemin cikis fiyati x(1-kayma); geri kalan birebir
    d = old[20] - new[20]
    def ayni(i, x, y):
        if i % 5 in (0, 1):
            return abs((x - y) - d) < 1e-6
        if i % 5 == 3:
            return len(x) == len(y) and all(a == b or (a[0] == "BTC/USDT" and a[1] == "HEDEF" and abs(b[3] - a[3] * (1 - C.SLIPPAGE)) < 1e-9)
                                            for a, b in zip(x, y))
        return False
    beklenen = 0 < d < 1 and min(i for i, _, _ in diff) == 20 and all(ayni(i, x, y) for i, x, y in diff)
    check(f"sanal hesap: eski kodla ayni sonuc, tek fark portfoy cikis kaymasi ({len(old)} nokta)",
          len(old) == len(new) and beklenen, [i for i, _, _ in diff])
else:
    print("ATLANDI eski kod kopyasi yok")

print("\nSONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
