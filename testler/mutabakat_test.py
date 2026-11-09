"""Bakiye mutabakati + dengeleme defteri. Tamamen sahte borsa: ag yok (soket kapali), anahtar yok, gercek emir yok, ucretli cagri yok.
Canli ayarlar sadece bu surecin belleginde, dosyaya yazilmaz. Kullanim: KQB_APP_DIR=<test> python -I mutabakat_test.py <proje>"""
import json
import math
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
import config as C

for f in os.listdir(C.APP_DIR):
    if f.startswith(("state", "durdur", "ders_defteri", "gecmis", "trading_memory")):
        os.remove(os.path.join(C.APP_DIR, f))
EV = os.path.join(C.APP_DIR, "ev")  # hesap sahibi kaydi kullanicinin gercek ev klasorune yazilmasin
shutil.rmtree(EV, ignore_errors=True)
os.makedirs(EV)
os.environ["HOME"] = os.environ["USERPROFILE"] = EV
C.REBAL_DIR = os.path.join(C.APP_DIR, "dengeleme_defteri_test")
shutil.rmtree(C.REBAL_DIR, ignore_errors=True)
C.MODEL_FILE = os.path.join(C.APP_DIR, "yok_model.pkl")
C.SETTINGS_FILE = os.path.join(C.APP_DIR, "yok_ayar.json")

cfg = open(os.path.join(PROJ, "config.py"), encoding="utf-8").read()
import agent as A
import bot as B
import portfoy as PF


def _no_net(*a, **k):
    raise AssertionError("testte gercek baglanti yok")


socket.socket.connect = _no_net  # Teknik/Stream kendi (bagsiz) borsa nesnesini kurar; baglanti denemesi burada patlar
socket.create_connection = _no_net
ok_all = True
KEY = "sahte-mutabakat-anahtari"
C.API_KEY, C.API_SECRET, C.TELEGRAM_TOKEN = KEY, "sahte-gizli", ""
C.LIVE_ALLOWED, C.PORTFOY_LIVE, C.LIVE_MAX_USDT, C.LIVE_MAX_EQUITY, C.HABER_LIVE = True, True, 1e6, 1.0, False  # sahte borsa icin, kaydedilmez
A.brain = lambda: "local"


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


class FakeEx:
    """Dolum + komisyon + stop kilidini bakiyeye isleyen sahte borsa."""
    PX = {"BTC/USDT": 100_000.0, "ETH/USDT": 4000.0, "SOL/USDT": 200.0, "RAW/USDT": 2.0, "UZAK/USDT": 50.0}
    STEP = {"BTC/USDT": 0.00001, "ETH/USDT": 0.0001, "SOL/USDT": 0.001, "RAW/USDT": 1.0, "UZAK/USDT": 0.01}

    def __init__(self, usdt=100_000.0):
        self.bal = {"USDT": {"free": usdt, "used": 0.0}}
        self.markets = {s: {"active": True, "info": {"status": "TRADING"}} for s in self.PX}
        self.markets["USDC/USDT"] = {"active": True}
        self.markets["ESKI/USDT"] = {"active": False}
        self.orders, self.cancels, self.reads, self.n, self.stops = [], [], 0, 0, {}
        self.fee_coin, self.bal_err, self.ticker_err, self.tick_calls = True, None, None, 0
        self.bal_fail = 0

    def set(self, coin, free=0.0, used=0.0):
        self.bal[coin] = {"free": free, "used": used}

    def fetch_balance(self):
        self.reads += 1
        if self.bal_err:
            raise self.bal_err
        if self.bal_fail > 0:
            self.bal_fail -= 1
            raise ccxt.NetworkError("zaman asimi")
        out = {"info": {"ham": 1}, "timestamp": None, "datetime": None, "free": {}, "used": {}, "total": {}}
        for k, v in self.bal.items():
            out[k] = {"free": v["free"], "used": v["used"], "total": v["free"] + v["used"]}
        return out

    def create_order(self, s, typ, side, qty, price=None, params=None):
        self.n += 1
        oid, coin = str(self.n), s.split("/")[0]
        self.orders.append({"s": s, "typ": typ, "side": side, "qty": qty, "id": oid})
        c = self.bal.setdefault(coin, {"free": 0.0, "used": 0.0})
        if typ == "STOP_LOSS_LIMIT":
            c["free"] -= qty
            c["used"] += qty
            self.stops[oid] = (coin, qty)
            return {"id": oid, "filled": 0.0, "status": "open"}
        px = self.PX[s]
        if side == "buy":
            cut = qty * 0.001 if self.fee_coin else 0.0
            c["free"] += qty - cut
            self.bal["USDT"]["free"] -= qty * px
            fees = [{"currency": coin, "cost": cut}] if self.fee_coin else [{"currency": "BNB", "cost": 0.0}]
        else:
            c["free"] -= qty
            self.bal["USDT"]["free"] += qty * px * 0.999
            fees = [{"currency": "USDT", "cost": qty * px * 0.001}]
        return {"id": oid, "filled": qty, "average": px, "status": "closed", "fees": fees}

    def cancel_order(self, oid, s):
        self.cancels.append(oid)
        coin, qty = self.stops.pop(oid, (s.split("/")[0], 0.0))
        self.bal[coin]["free"] += qty
        self.bal[coin]["used"] -= qty
        return {"id": oid, "status": "canceled", "filled": 0.0}

    def fetch_order(self, oid, s=None, params=None):
        return {"id": oid, "status": "open", "filled": 0.0}

    def fetch_my_trades(self, *a, **k):
        return []

    def fetch_ticker(self, s):
        return {"last": self.PX[s], "bid": self.PX[s], "ask": self.PX[s]}

    def fetch_tickers(self, syms):
        self.tick_calls += 1
        if self.ticker_err:
            raise self.ticker_err
        return {s: {"last": self.PX[s]} for s in syms}

    def public_get_exchangeinfo(self, p):
        return {"symbols": [{"status": "TRADING"}]}

    def sapi_get_account_apirestrictions(self):
        return {"enableWithdrawals": False}

    def price_to_precision(self, s, p):
        return f"{p:.2f}"

    def amount_to_precision(self, s, q):
        st = self.STEP[s]
        return f"{math.floor(q / st + 1e-9) * st:.8f}"

    def market(self, s):
        return {"precision": {"amount": self.STEP.get(s)}, "limits": {"cost": {"min": 5}}}


def fresh(usdt=100_000.0, base=True):
    """Motor + sahte borsa. Sanal hesap ayri sahte baglanti kullanir."""
    ex, pub = FakeEx(usdt), FakeEx()
    B.D.exchange = lambda auth=False: ex if auth else pub
    if os.path.exists(C.STATE_FILE):  # her senaryo temiz defterle
        os.remove(C.STATE_FILE)
    b = B.Bot()
    b.bid.update(FakeEx.PX)
    b.ask.update(FakeEx.PX)
    a = b.accounts["LIVE"]
    a.sync()
    if base:
        a.balance_check()
    return b, a, ex, pub


def pos_(qty, entry=100_000.0, stop_id=None):
    return {"qty": qty, "qty0": qty, "entry": entry, "atr": 1.0, "sl": entry * 0.65, "sl0": entry * 0.65, "tp": entry * 1000, "part": False,
            "hi": entry, "fee": 0.0, "realized": 0.0, "t": time.time(), "stop_id": stop_id, "stop_px": entry * 0.65, "partial_at": None,
            "kind": PF.KIND, "manual": False, "max_hours": 1e6, "exit": {}, "seen": time.time()}


def alerts(b):
    return [n["text"] for n in b.notes if "bakiye uyuşmazlığı:" in n["text"]]


def solved(b):
    return [n["text"] for n in b.notes if "giderildi" in n["text"]]


def toz_notes(b):
    return [n["text"] for n in b.notes if "hesaptaki artık" in n["text"]]


def dis_notes(b):
    return [n["text"] for n in b.notes if "bot dışında duran" in n["text"]]


def plan(s="BTC/USDT", usdt=1000.0):
    px = FakeEx.PX[s]
    return {"type": "buy", "symbol": s, "kind": PF.KIND, "entry": "taker", "usdt": usdt, "stop": px * 0.65, "tp2": px * 1000, "tp1": 0, "risk_pct": 0,
            "max_hours": 1e6, "reason": "test", "conviction": None}


BTC = "BTC/USDT"
# ---- ayarlar: canli varsayilanlari hala kapali ----
check("varsayilan canli ayarlar degismedi", re.search(r"^LIVE_ALLOWED = False", cfg, re.M) and re.search(r"^PORTFOY_LIVE = False", cfg, re.M) and re.search(r"^HABER_LIVE = False", cfg, re.M))

# ---- esit, kilitli dahil, tolerans ----
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.002, used=0.008)
check("serbest + kilitli = defter: uyusmazlik yok", a.balance_check() and not a.st["mut"], a.st["mut"])
ex.set("BTC", free=0.0, used=0.01)
a.balance_check()
check("tamami stop emrinde kilitli: uyusmazlik yok", not a.st["mut"], a.st["mut"])
ex.set("BTC", free=0.01 + 0.00004)  # 4 USDT fazla: en kucuk emir tutari (5) altinda, toz
a.balance_check()
check("fark toz sinirinda: sayilmaz (fazla)", not a.st["mut"], a.st["mut"])
ex.set("BTC", free=0.01 - 0.00004)
a.balance_check()
check("fark toz sinirinda: sayilmaz (eksik)", not a.st["mut"], a.st["mut"])
ex.set("BTC", free=0.01 + 0.00006)
a.balance_check()
check("toz sinirinin ustu fazla: sayilir", a.st["mut"].get(BTC, {}).get("n") == 1, a.st["mut"])
ex.set("BTC", free=0.01)
a.balance_check()
ex.set("BTC", free=0.01 - 0.00006)
a.balance_check()
check("toz sinirinin ustu eksik: sayilir", a.st["mut"].get(BTC, {}).get("n") == 1, a.st["mut"])
a.st["mut"].clear()
del a.st["positions"][BTC]
ex.set("BTC", 0.0)
# adim tozdan buyukse adim esas: RAW adim 1, fiyat 2 -> toz 2,5 > adim; UZAK icin bu kez adim buyuk sahte market
ex.STEP = dict(ex.STEP, **{"SOL/USDT": 0.5})
a.st["positions"]["SOL/USDT"] = pos_(10.0, entry=200.0)
ex.set("SOL", free=10.4)  # fark 0,4: toz siniri 5/200 = 0,025, adim 0,5
a.balance_check()
check("borsa adimi tozdan buyukse adim esas alinir", not a.st["mut"], a.st["mut"])
ex.set("SOL", free=10.6)
a.balance_check()
check("adimin ustu sayilir", a.st["mut"].get("SOL/USDT", {}).get("n") == 1, a.st["mut"])

# ---- 3 kontrol: tek uyari, yasak; satis yok ----
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.02)
ex.set("ETH", free=0.0)
for i in range(2):
    a.balance_check()
check("2 kontrol: uyari yok, yasak yok", not alerts(b) and not a.mut_why(BTC) and a.st["mut"][BTC]["n"] == 2, a.st["mut"])
a.balance_check()
check("3. kontrol: tek uyari + yasak", len(alerts(b)) == 1 and a.mut_why(BTC), alerts(b))
check("uyari telefona gidecek turde (uyari) ve hesapli", [n for n in b.notes if "bakiye uyuşmazlığı:" in n["text"]][0]["kind"] == "uyari"
      and [n for n in b.notes if "bakiye uyuşmazlığı:" in n["text"]][0]["acct"] == "LIVE")
for i in range(5):
    a.balance_check()
check("sonraki kontroller yeni uyari uretmez", len(alerts(b)) == 1, len(alerts(b)))
check("mutabakat tek emir bile vermedi, stop iptal etmedi", not ex.orders and not ex.cancels, (ex.orders, ex.cancels))
check("yeniden baslama sonrasi da tek uyari", a.mut_why(BTC) and json.loads(json.dumps(a.st))["mut"][BTC]["n"] >= 3)
a2 = B.Account(b, "LIVE", json.loads(json.dumps(a.st)))
a2.own, a2.key_ok = True, True
n0 = len(alerts(b))
a2.balance_check()
check("yasak diskten gelen durumla surer, uyari tekrarlanmaz", a2.mut_why(BTC) and len(alerts(b)) == n0, a2.st["mut"])

# ---- yasak: tum alim yollari kapali, satislar acik ----
check("emir kapisi alimi reddeder", "uyuşmazlığı" in (a.fence(BTC, "buy", 0.001, 100_000.0) or ""), a.fence(BTC, "buy", 0.001, 100_000.0))
check("coin kilidi (karar ve arayuz yolu) gorur", "uyuşmazlığı" in (a.locked(BTC) or ""), a.locked(BTC))
n_note = len(b.notes)
r = a.enter(BTC, plan())
check("yeni alis (enter) borsaya gitmez, ek uyari yok", r and "uyuşmazlığı" in r and not ex.orders and len(b.notes) == n_note, r)
r = a.pf_buy(BTC, 1000.0, "test")
check("portfoy artirma borsaya gitmez, felaket stopu iptal edilmez", r and "uyuşmazlığı" in r and not ex.orders and not ex.cancels, r)
r = a.enter(BTC, dict(plan(), manual=True))
check("elle alis da ayni kapidan gecer", r and "uyuşmazlığı" in r and not ex.orders, r)
check("baska coinde alim serbest", a.fence("ETH/USDT", "buy", 0.1, 4000.0) is None, a.fence("ETH/USDT", "buy", 0.1, 4000.0))
check("yasak satisi engellemez", a.fence(BTC, "sell", 0.005) is None, a.fence(BTC, "sell", 0.005))
check("yasak stopu engellemez", a.fence(BTC, "sell", 0.01, 60_000.0, 65_000.0) is None)
r = a.rebalance({BTC: 0.5, "ETH/USDT": 0.05}, True)
check("dengeleme: yasakli coin alinmaz, digeri alinir", any(x.startswith("BTC/USDT al:") and "uyuşmazlığı" in x for x in r) and "ETH/USDT" in a.st["positions"]
      and not [o for o in ex.orders if o["s"] == BTC], r)
n_o = len(ex.orders)
a.st["positions"][BTC]["kind"] = PF.KIND
r = a.pf_sell(BTC, None, "hedef 0")  # strateji satisi serbest; satilan sadece defterdeki miktar, hesaptaki fazlalik degil
sold = sum(o["qty"] for o in ex.orders[n_o:] if o["s"] == BTC and o["side"] == "sell" and o["typ"] == "market")
check("strateji satisi calisir, fazlalik satilmaz", BTC not in a.st["positions"] and abs(sold - 0.01) < 1e-9 and abs(ex.bal["BTC"]["free"] - 0.01) < 1e-9, (sold, ex.bal["BTC"]))
a.balance_check()
check("pozisyon kapaninca fazlalik hala uyusmazlik, hala satilmaz", a.mut_why(BTC) and abs(ex.bal["BTC"]["free"] - 0.01) < 1e-9, a.st["mut"].get(BTC))

# ---- cozulme ----
n_a = len(alerts(b))
ex.set("BTC", 0.0)
a.balance_check()
check("eslesince yasak kalkar, bir bilgi notu", not a.mut_why(BTC) and BTC not in a.st["mut"] and len(solved(b)) == 1 and len(alerts(b)) == n_a, solved(b))
a.balance_check()
check("bilgi notu tekrarlanmaz", len(solved(b)) == 1)
check("alim yeniden serbest (kapi; cikis sonrasi bekleme ayri kural)", a.fence(BTC, "buy", 0.001, 100_000.0) is None and "uyuşmazlığı" not in (a.locked(BTC) or ""), a.locked(BTC))
ex.set("BTC", 0.02)
for i in range(3):
    a.balance_check()
check("yeni uyusmazlik yeni tek uyari", len(alerts(b)) == n_a + 1, len(alerts(b)))

# ---- gecici fark: sayac sifirlanir ----
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
for v in (0.02, 0.02, 0.01, 0.02, 0.02, 0.01, 0.02, 0.02):
    ex.set("BTC", free=v)
    a.balance_check()
check("ust uste olmayan fark uyari uretmez (3 kontrol kurali)", not alerts(b) and not a.mut_why(BTC) and a.st["mut"][BTC]["n"] == 2, a.st["mut"])

# ---- belirsiz durumlar sayilmaz ----
ex.set("BTC", free=0.05)
a.st["orders"]["x"] = {"s": BTC, "side": "buy", "typ": "market", "t": time.time(), "qty": 0.04, "chk": time.time()}
for i in range(5):
    a.balance_check()
check("sonucu bilinmeyen emir: sayac durur", a.st["mut"][BTC]["n"] == 2 and not alerts(b), a.st["mut"])
del a.st["orders"]["x"]
a.st["pending"][BTC] = {"id": "1", "qty": 0.01, "price": 90_000.0, "t": time.time()}
for i in range(5):
    a.balance_check()
check("bekleyen limit alim: sayac durur", a.st["mut"][BTC]["n"] == 2 and not alerts(b), a.st["mut"])
del a.st["pending"][BTC]
ex.bal_err = ccxt.NetworkError("zaman asimi")
check("bakiye okunamadi: False, sayac degismez, uyari yok", a.balance_check() is False and a.st["mut"][BTC]["n"] == 2 and not alerts(b), a.st["mut"])
ex.bal_err = None
a.balance_check()
check("okuma geri geldi: sayim surer", a.st["mut"][BTC]["n"] == 3 and len(alerts(b)) == 1, a.st["mut"])
ex.bal_err = ccxt.NetworkError("zaman asimi")
a.balance_check()
check("okuma hatasi yasagi kaldirmaz", a.mut_why(BTC), a.st["mut"])
ex.bal_err = None

# ---- defterde olmayan coin ----
b, a, ex, pub = fresh()
ex.set("SOL", free=3.0)  # 600 USDT, defterde yok (izleniyor)
ex.set("RAW", free=500.0)  # izlenmeyen coin: fiyat ayri okunur
ex.set("UZAK", free=0.05)  # 2,5 USDT toz
ex.set("USDC", free=900.0)  # sabit para
ex.set("LDBTC", free=1.0)  # paritesi yok (Earn)
ex.set("ESKI", free=99.0)  # kapali parite
del b.bid["RAW/USDT"], b.bid["UZAK/USDT"]
for i in range(3):
    a.balance_check()
check("defterde olmayan coin yakalanir (kaybolmus pozisyon), fiyati okunarak", set(a.st["mut"]) == {"SOL/USDT", "RAW/USDT"} and a.mut_why("SOL/USDT") and len(alerts(b)) == 2, a.st["mut"])
check("USDT, sabit para, paritesiz, kapali parite ve toz coin sayilmaz", not ({"USDT/USDT", "USDC/USDT", "LDBTC/USDT", "ESKI/USDT", "UZAK/USDT"} & set(a.st["mut"])), a.st["mut"])
ex.set("RAW", 0.0)
ex.ticker_err = ccxt.NetworkError("zaman asimi")
a.balance_check()
check("fiyat okunamazsa o coin bu tur sayilmaz, yasak bozulmaz", "RAW/USDT" in a.st["mut"], a.st["mut"])
ex.ticker_err = None
a.balance_check()
check("fiyat gelince cozulur", "RAW/USDT" not in a.st["mut"], a.st["mut"])
check("emir yok", not ex.orders and not ex.cancels)

# ---- inceleme: go-live aninda hesapta duran bot disi coin (taban) ----
b, a, ex, pub = fresh(base=False)
check("taban henuz alinmadi", a.st["dis"] is None)
ex.set("BTC", free=0.02)  # kullanicinin kendi BTC'si
ex.set("RAW", free=1.0)  # 2 USDT toz: notta gorunmez
ex.set("USDC", free=500.0)
a.st["pending"]["ETH/USDT"] = {"id": "1", "qty": 0.1, "price": 3900.0, "t": time.time()}
a.balance_check()
check("yolda emir varken taban alinmaz, sayac da islemez", a.st["dis"] is None and not a.st["mut"], a.st["dis"])
del a.st["pending"]["ETH/USDT"]
for i in range(5):
    a.balance_check()
check("bot disi coin: yasak yok, uyari yok", not a.st["mut"] and not a.mut_why(BTC) and not alerts(b), a.st["mut"])
check("taban kaydedildi, sabit para haric", abs(a.st["dis"][BTC] - 0.02) < 1e-12 and "USDC/USDT" not in a.st["dis"], a.st["dis"])
check("tek not: coin adiyla, toz haric", len(dis_notes(b)) == 1 and "BTC 0.02" in dis_notes(b)[0] and "RAW" not in dis_notes(b)[0], dis_notes(b))
check("not telefona gider", [n for n in b.notes if "bot dışında duran" in n["text"]][0]["kind"] == "uyari")
base_btc = dict(a.st["dis"])
a2 = B.Account(b, "LIVE", json.loads(json.dumps(a.st)))
a2.own, a2.key_ok = True, True
a2.balance_check()
check("taban yeniden baslamada korunur, not tekrarlanmaz", a2.st["dis"] == base_btc and len(dis_notes(b)) == 1, a2.st["dis"])
r = a.rebalance({BTC: 0.05}, True)
check("dengeleme coini alabilir (cekirdek sifira kilitlenmez)", BTC in a.st["positions"] and any(x.startswith("BTC/USDT al:") and "uyuşmazlığı" not in x for x in r), r)
for i in range(5):
    a.balance_check()
check("alimdan sonra bot disi + pozisyon: uyusmazlik yok", not a.st["mut"], (a.st["mut"], ex.bal["BTC"]))
r = a.rebalance({BTC: 0.0}, True)
check("hedef 0: sadece defterdeki satilir, bot disi hesapta kalir", BTC not in a.st["positions"] and ex.bal["BTC"]["free"] >= 0.02 - 1e-9, (r, ex.bal["BTC"]))
a.balance_check()
a.st["cooldown"].clear()
r = a.rebalance({BTC: 0.03}, True)
check("hedef yeniden yukselince alim gider (geri donmez kuculme yok)", BTC in a.st["positions"] and not a.mut_why(BTC), r)
qty = a.st["positions"][BTC]["qty"]
tz = a.st["toz"].get(BTC, 0.0)
ex.set("BTC", free=qty + tz + 0.02 + 0.01)  # sonradan yatirilan coin
for i in range(3):
    a.balance_check()
check("sonradan gelen fazlalik uyusmazlik: uyari + yasak, metinde bot disi miktar", a.mut_why(BTC) and len(alerts(b)) == 1 and "bot dışı kayıtlı 0.02" in alerts(b)[0], alerts(b))
ex.set("BTC", free=qty + tz + 0.02)
a.balance_check()
check("fazlalik tasininca yasak kalkar", not a.mut_why(BTC), a.st["mut"])
ex.set("BTC", free=qty + tz + 0.005)
a.balance_check()
check("bot disi coin satildi: eksik sayilmaz", not a.st["mut"], a.st["mut"])
ex.set("BTC", free=qty / 2)
for i in range(3):
    a.balance_check()
check("taban pozisyon eksigini gizlemez", a.mut_why(BTC) and len(alerts(b)) == 2, a.st["mut"])
b, a, ex, pub = fresh(base=False)
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.01)
for i in range(3):
    a.balance_check()
check("bot disi coin yoksa not da yok, taban bos", a.st["dis"] == {} and not dis_notes(b) and not a.st["mut"], a.st["dis"])
check("taban tek emir vermedi", not ex.orders and not ex.cancels)

# ---- inceleme: esikte gezinen fiyat uyari/giderildi dondurmez ----
b, a, ex, pub = fresh()
ex.set("UZAK", free=0.1)
for p_ in (51, 51, 51, 49) * 3:
    b.bid["UZAK/USDT"] = float(p_)
    a.balance_check()
check("0.1 UZAK fiyat 51/49: tek uyari, giderildi yok, yasak surer", len(alerts(b)) == 1 and not solved(b) and a.mut_why("UZAK/USDT"), (alerts(b), solved(b)))
b.bid["UZAK/USDT"] = 39.0  # 3,9 USDT: daralan sinirin (4) altinda
a.balance_check()
check("deger gercekten toz olunca yasak kalkar, tek not", not a.mut_why("UZAK/USDT") and len(solved(b)) == 1, a.st["mut"])
for p_ in (49, 41, 49, 41):
    b.bid["UZAK/USDT"] = float(p_)
    a.balance_check()
check("yasak yokken toz sinirinin alti sayilmaz", not a.st["mut"] and len(alerts(b)) == 1, a.st["mut"])
b, a, ex, pub = fresh()
ex.STEP = dict(ex.STEP, **{"SOL/USDT": 0.5})
a.st["positions"]["SOL/USDT"] = pos_(10.0, entry=200.0)
ex.set("SOL", free=10.6)
for i in range(3):
    a.balance_check()
ex.set("SOL", free=10.4)  # adim 0,5: adim payi daralmaz, yasak takili kalmaz
a.balance_check()
check("yasak kalkarken borsa adimi payi daralmaz", not a.mut_why("SOL/USDT") and len(solved(b)) == 1, a.st["mut"])

# ---- gercek dolumlar: yuvarlama artigi yanlis alarm uretmez ----
b, a, ex, pub = fresh(1_000_000.0)
r = a.enter(BTC, plan(usdt=1234.0))
check("alim: pozisyon + stop, bakiye kilitli", r is None and BTC in a.st["positions"] and ex.bal["BTC"]["used"] > 0, (r, ex.bal.get("BTC")))
a.balance_check()
check("dolumdan hemen sonra uyusmazlik yok", not a.st["mut"], (a.st["mut"], a.st["positions"][BTC]["qty"], ex.bal["BTC"]))
for i in range(40):
    r = a.pf_buy(BTC, 1234.0 + i * 7.3, "artir")
    assert r is None, r
    a.balance_check()
toz = a.st["toz"].get(BTC, 0.0)
tot = ex.bal["BTC"]["free"] + ex.bal["BTC"]["used"]
qty = a.st["positions"][BTC]["qty"]
check("40 alimda biriken artik toz siniri ustunde (test anlamli)", (tot - qty) * 100_000 > 5, f"{(tot - qty) * 100_000:.2f} USDT")
check("artik kayitli: yanlis alarm yok", not a.st["mut"] and not alerts(b) and abs(toz - (tot - qty)) < 1e-9, (a.st["mut"], toz, tot - qty))
ex.bal["BTC"]["free"] += 0.001  # gercek fark (100 USDT): artik onu gizlemez
a.balance_check()
check("artik gercek farki gizlemez", a.st["mut"].get(BTC, {}).get("n") == 1, a.st["mut"])
ex.bal["BTC"]["free"] -= 0.001
a.balance_check()
check("fazlalik gidince sayac sifirlanir", BTC not in a.st["mut"], a.st["mut"])
n_s = len(ex.orders)
a.exit(BTC, a.st["positions"][BTC], "PORTFOY_CIKIS")
a.balance_check()
check("tam cikis sonrasi artik kalir, uyusmazlik yok, satilmaz", BTC not in a.st["positions"] and not a.st["mut"] and ex.bal["BTC"]["free"] + ex.bal["BTC"]["used"] > 0.00005, (a.st["mut"], ex.bal["BTC"]))
ex.set("BTC", 0.0)  # kullanici kalan artigi Binance'te satti
for i in range(5):
    a.balance_check()
check("cikis sonrasi artik elle satildi: tek uyari, yasak yok, artik sifir", not a.st["mut"] and not a.st["toz"].get(BTC) and len(toz_notes(b)) == 1 and not alerts(b), (a.st["mut"], a.st["toz"], toz_notes(b)))

# ---- inceleme: artik eksigi gizlemez, ama geri donmez yasak da yapmaz ----
b, a, ex, pub = fresh(1_000_000.0)
a.enter(BTC, plan(usdt=1234.0))
for i in range(40):
    assert a.pf_buy(BTC, 1234.0 + i * 7.3, "artir") is None
a.balance_check()
toz = a.st["toz"][BTC]
check("artik 5 USDT'den buyuk (senaryo anlamli)", toz * 100_000 > 5, toz * 100_000)
ex.bal["BTC"]["free"] -= 0.0003  # 30 USDT elle satildi; bakiye defterin 5 USDT'den az altinda
qty = a.st["positions"][BTC]["qty"]
for i in range(2):
    a.balance_check()
check("artik eksigi sayilir (2 kontrol: henuz uyari yok)", a.st["mut"].get(BTC, {}).get("n") == 2 and not toz_notes(b), a.st["mut"])
for i in range(3):
    a.balance_check()
check("artik eksigi: tek uyari, yasak yok", len(toz_notes(b)) == 1 and not a.mut_why(BTC) and not a.st["mut"] and not alerts(b), (toz_notes(b), a.st["mut"]))
x = ex.bal["BTC"]["free"] + ex.bal["BTC"]["used"]
check("artik gercek bakiyeye duzeltildi", abs(a.st["toz"][BTC] - max(x - qty, 0.0)) < 1e-12, a.st["toz"])
ex.bal["BTC"]["free"] -= 0.0003  # artik kalmadi: ayni eksik simdi pozisyondan
for i in range(3):
    a.balance_check()
check("ikinci 30 USDT eksik: pozisyon eksigi, uyari + yasak", a.mut_why(BTC) and len(alerts(b)) == 1 and len(toz_notes(b)) == 1, a.st["mut"])
ex.bal["BTC"]["free"] += 0.00035  # defterin ustune geri
a.balance_check()
check("eksik geri gelince yasak kalkar", not a.mut_why(BTC) and len(solved(b)) == 1, a.st["mut"])
ex.bal["BTC"]["free"] += 0.02
for i in range(3):
    a.balance_check()
check("yasak (fazlalik) varken: uyari", a.mut_why(BTC) and len(alerts(b)) == 2)
ex.bal["BTC"]["free"] -= 0.02
a.st["toz"][BTC] = 0.001  # kayitli artik var ama hesapta yok
n_t = len(toz_notes(b))
a.balance_check()
check("yasakliyken fark artiga dustu: yasak hemen kalkar, takili kalmaz", not a.mut_why(BTC) and BTC not in a.st["mut"] and len(toz_notes(b)) == n_t + 1, a.st["mut"])
check("artik testleri tek satis bile vermedi", not [o for o in ex.orders if o["side"] == "sell" and o["typ"] == "market"])
ex2 = fresh(1_000_000.0)
b2, a2, ex2, _ = ex2
ex2.fee_coin = False
a2.enter(BTC, plan(usdt=1234.0))
for i in range(5):
    a2.pf_buy(BTC, 1000.0, "artir")
a2.balance_check()
check("komisyon BNB ile: artik yok, uyusmazlik yok", not a2.st["toz"].get(BTC) and not a2.st["mut"], (a2.st["toz"], a2.st["mut"]))

# ---- dengeleme oncesi kontrol, okunamazsa alim yok ----
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.01)
ex.reads = 0
a.rebalance({BTC: 0.0}, True, None, lambda s: True)
check("dengeleme basinda bakiye bir kez okunur", ex.reads >= 1, ex.reads)
a.st["positions"]["SOL/USDT"] = pos_(50.0, entry=200.0)
ex.set("SOL", free=50.0)
real_bc, order_seen = a.balance_check, []


def spy():
    order_seen.append(len(ex.orders))
    return real_bc()


a.balance_check = spy
r = a.rebalance({BTC: 0.0, "ETH/USDT": 0.2}, True)
check("kontrol emirlerden once calisir", order_seen == [0] and len(ex.orders) > 0, (order_seen, len(ex.orders)))
a.balance_check = real_bc
ex.bal_err = None
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.01)
B.MUT_RETRY = (3, 0.01)
bc_n = []
a.balance_check = lambda: bc_n.append(1) or False
r = a.rebalance({BTC: 0.0, "ETH/USDT": 0.2}, True)
check("bakiye hep okunamazsa: satis yapilir, alim yapilmaz", BTC not in a.st["positions"] and "ETH/USDT" not in a.st["positions"] and any("okunamadı" in x for x in r), r)
check("alimdan once yeniden denendi (1 + 3 okuma), tek telefon uyarisi", len(bc_n) == 4 and len([n for n in b.notes if "dakika okunamadı" in n["text"] and n["kind"] == "uyari"]) == 1, len(bc_n))
del a.balance_check
b, a, ex, pub = fresh()
a.st["positions"][BTC] = pos_(0.01)
ex.set("BTC", free=0.01)
ex.bal_fail, ex.reads = 1, 0
r = a.rebalance({BTC: 0.0, "ETH/USDT": 0.2}, True)
check("tek okuma hatasi: satis da alim da yapilir, uyari yok", BTC not in a.st["positions"] and "ETH/USDT" in a.st["positions"] and any(x.startswith("ETH/USDT al:") and "okunamadı" not in x for x in r)
      and not [n for n in b.notes if "okunamadı" in n["text"]], r)
b, a, ex, pub = fresh()
bc_n = []
a.balance_check = lambda: bc_n.append(1) or False
a.rebalance({}, True)
check("alim yoksa bekleme yok", len(bc_n) == 1, len(bc_n))
a.st["pause_until"] = time.time() + 3600
bc_n.clear()
r = a.rebalance({"ETH/USDT": 0.2}, True)
check("alim zaten engelliyse bekleme yok", len(bc_n) == 1 and any("alışlar yapılmadı" in x for x in r), (len(bc_n), r))
a.st["pause_until"] = 0
b.stop.set()
bc_n.clear()
r = a.rebalance({"ETH/USDT": 0.2}, True)
check("uygulama kapanirken beklemez, yanlis uyari yok", len(bc_n) == 1 and "ETH/USDT" not in a.st["positions"] and not [n for n in b.notes if "dakika okunamadı" in n["text"]], len(bc_n))
b.stop.clear()
del a.balance_check

# ---- gercek hesap kilidi yoksa / sanal hesap: borsaya hic sorulmaz ----
b, a, ex, pub = fresh()
ex.reads, pub.reads = 0, 0
pa = b.accounts["PAPER"]
check("sanal hesap kontrolu True, borsaya sormaz", pa.balance_check() is True and pub.reads == 0)
pa.st.update(cash=10_000.0, positions={}, trades=[])
pa.rebalance({BTC: 0.3, "ETH/USDT": 0.2}, True)
check("sanal dengeleme mutabakat kullanmaz, defter alanlari bos, bakiye okumaz", pub.reads == 0 and not pa.st["mut"] and not pa.st["toz"] and len(pa.st["positions"]) == 2, pa.st["positions"].keys())
check("sanal hesapta kilit degismez", pa.mut_why(BTC) is None and pa.locked("SOL/USDT") is None)
a.own = False
ex.reads = 0
check("kilit bizde degil (pasif kopya): borsaya sormaz, sayac tutmaz", a.balance_check() is True and ex.reads == 0)
a.own = True

# ---- motor dongusu: acilista bir, sonra MUT_SEC'te bir ----


class _Dummy:
    def start(self, *a):
        pass


def run_bot(mut_sec, wait=0.6):
    bb, aa, xx, _ = fresh()
    bb.stream, bb.tech, bb.macro, bb.chain = _Dummy(), _Dummy(), _Dummy(), _Dummy()
    bb.tech.opps = []
    bb.refresh_prices = lambda: None
    bb._background = lambda name, fn: True
    bb.pf_tick = lambda: False
    bb.observe = lambda: None
    calls = []
    rc = aa.balance_check
    aa.balance_check = lambda: calls.append(time.time()) or rc()
    B.MUT_SEC = mut_sec
    th = threading.Thread(target=bb.run, daemon=True)
    th.start()
    time.sleep(wait)
    bb.stop.set()
    th.join(10)
    return calls, xx, th


_loops = B.L.loop, B.HB.loop, B.G.load, B.PF.load_model, B.PF.maybe_train, B.MC.calibrate, C.TICK_SEC, B.MUT_SEC
B.L.loop = B.HB.loop = lambda *a, **k: None
B.G.load = B.PF.load_model = B.MC.calibrate = lambda *a, **k: None
B.PF.maybe_train = lambda *a, **k: None
C.TICK_SEC = 0.01
calls, xx, th = run_bot(1e9)
check("acilista bir kontrol, emir yok", len(calls) == 1 and not th.is_alive() and not xx.orders, len(calls))
calls, xx, th = run_bot(0.05)
check("dongude periyodik kontrol, emir yok", len(calls) >= 3 and not xx.orders, len(calls))
B.L.loop, B.HB.loop, B.G.load, B.PF.load_model, B.PF.maybe_train, B.MC.calibrate, C.TICK_SEC, B.MUT_SEC = _loops
check("periyot birkac dakika, 3 kontrol kurali", 60 <= B.MUT_SEC <= 600 and B.MUT_N == 3, (B.MUT_SEC, B.MUT_N))

# ---- dengeleme defteri (uctan uca Bot.portfolio) ----
shutil.rmtree(C.REBAL_DIR, ignore_errors=True)
b, a, ex, pub = fresh()
b.accounts["PAPER"].st.update(cash=10_000.0, positions={}, trades=[])
real_t = PF.targets
PF.targets = lambda ex_, st, ml_day, core_frac=None: (
    {BTC: 0.2, "ETH/USDT": 0.1}, {"core": {BTC: {"w": 0.2, "trend": 1.0, "vol": 0.3}, "ETH/USDT": {"w": 0.1, "trend": 0.5, "vol": 0.4}},
                                  "ml": {"picks": [], "reg": False, "note": "t", "t": time.time()}})
b.portfolio(force_ml=True)
fn = os.path.join(C.REBAL_DIR, B.today() + ".jsonl")
rows = [json.loads(x) for x in open(fn, encoding="utf-8")]
check("gunluk dosya: uygulama veri klasorunde, gune gore adli", os.path.dirname(os.path.dirname(fn)) == C.APP_DIR and os.path.basename(fn) == time.strftime("%Y-%m-%d", time.gmtime()) + ".jsonl", fn)
check("her hesap icin once karar sonra sonuc", [(r["hesap"], r["asama"]) for r in rows] == [("PAPER", "karar"), ("PAPER", "sonuc"), ("LIVE", "karar"), ("LIVE", "sonuc")], [(r["hesap"], r["asama"]) for r in rows])
lk, ls = rows[2], rows[3]
check("karar: hedef + ml gunu + itiraz + pozisyon (emirden once)", lk["hedef"] == {BTC: 0.2, "ETH/USDT": 0.1} and lk["ml_gunu"] is True and lk["itiraz"] == {} and lk["pozisyon"] == {} and "kasa" in lk, lk)
check("sonuc: emir sonuclari + pozisyon + kasa", any(x.startswith("BTC/USDT al:") for x in ls["sonuc"]) and BTC in ls["pozisyon"] and ls["kasa"] > 0, ls)
txt = open(fn, encoding="utf-8").read()
check("defterde anahtar yok", KEY not in txt and "sahte-gizli" not in txt)
before = open(fn, "rb").read()
b.portfolio()
after = open(fn, "rb").read()
check("sadece sona eklenir", after.startswith(before) and len(after) > len(before), (len(before), len(after)))
for x in after.decode("utf-8").splitlines():
    json.loads(x)
check("her satir gecerli JSON", True)
for i in range(3):
    ex.set("SOL", free=3.0)
    a.balance_check()
b.portfolio()
last = [json.loads(x) for x in open(fn, encoding="utf-8")][-1]
check("uyusmazlik defterde gorunur", last["uyusmazlik"] == ["SOL/USDT"], last)
b3, a3, ex3, _ = fresh()
pa3 = b3.accounts["PAPER"]
pa3.st.update(cash=10_000.0, positions={}, trades=[])
a3.st["positions"]["SOL/USDT"] = pos_(5.0, entry=200.0)
ex3.set("SOL", free=5.0)
ex3.bal_fail = 1
b3.portfolio(force_ml=True)
check("ML gunu tek okuma hatasi: eski sepet satilir, gunun alimlari gider", "SOL/USDT" not in a3.st["positions"] and BTC in a3.st["positions"] and "ETH/USDT" in a3.st["positions"], b3.ag["pf"]["result"])
check("sanal hesap ayni hedeflerle alir", BTC in pa3.st["positions"] and "ETH/USDT" in pa3.st["positions"], pa3.st["positions"].keys())
real_reb, a.rebalance = a.rebalance, lambda *x, **k: (_ for _ in ()).throw(RuntimeError("motor hatasi"))
try:
    b.portfolio()
except RuntimeError:
    pass
last = [json.loads(x) for x in open(fn, encoding="utf-8")][-1]
check("dengeleme motor hatasiyla yarim kaldi: sonuc None olarak kayitta", last["hesap"] == "LIVE" and last["asama"] == "sonuc" and last["sonuc"] is None, last)
a.rebalance = real_reb
real_dir, C.REBAL_DIR = C.REBAL_DIR, os.path.join(fn, "yazilamaz")  # dosyanin altinda klasor acilamaz
n0 = len(b.notes)
b.portfolio()
b.portfolio()
yaz = [n["text"] for n in b.notes[n0:] if "Dengeleme defteri yazılamadı" in n["text"]]
check("defter yazilamazsa dengeleme surer, hesap basina saatte bir uyari", len(yaz) == 2 and b.ag["pf"].get("result"), yaz)
C.REBAL_DIR = real_dir

# ---- gec dengeleme: gercek hesap ayri izlenir ----
d0 = time.time() // 86400 * 86400
n1 = d0 + 1860


def geciken(bb):
    bb.t_start = d0 - 86400
    return bb.pf_late(max(n1, time.time()))


b5, a5, ex5, _ = fresh()
b5.portfolio()
check("iki hesap da bitti: gec yok", geciken(b5) == [] and set(b5.ag["pf"]["son"]) == {"PAPER", "LIVE"}, b5.ag["pf"]["son"])
b5, a5, ex5, _ = fresh()
a5.synced = False
b5.portfolio()
check("gercek hesap senkron degil, atlandi: sadece o gec", geciken(b5) == ["LIVE"] and "LIVE" not in b5.ag["pf"]["result"], b5.ag["pf"]["son"])
b5.notes[:] = []
b5.pf_watch(max(n1, time.time()))
b5.pf_watch(max(n1, time.time()))
gec = [n for n in b5.notes if "Dengeleme gecikti" in n["text"]]
check("uyari gercek hesabi soyler, gunde bir, telefona gider", len(gec) == 1 and "gerçek hesabın" in gec[0]["text"] and "sanal" not in gec[0]["text"] and "Son biten: yok" in gec[0]["text"] and gec[0]["kind"] == "uyari", gec)
h5 = b5.health()["dengeleme"]
check("health gercek hesabin gecikmesini gosterir", (not (time.time() % 86400 >= B.PF_LATE)) or (h5["gec"] is True and h5["geciken"] == ["LIVE"]), h5)
b5, a5, ex5, _ = fresh()
a5.rebalance = lambda *x, **k: (_ for _ in ()).throw(ccxt.ExchangeError("sahte"))
b5.portfolio()
check("borsa hatasi: sonuc yazilir ama bitmis sayilmaz", geciken(b5) == ["LIVE"] and "borsa hatası" in b5.ag["pf"]["result"]["LIVE"][0], b5.ag["pf"]["result"])


def reddet(s, typ, side, *x, **k):
    raise ccxt.InvalidOrder("sahte red")


b5, a5, ex5, _ = fresh()
ex5.set("SOL", free=5.0)
a5.balance_check()
a5.st["positions"]["SOL/USDT"] = pos_(5.0, 200.0)
_al = ex5.create_order
ex5.create_order = lambda s, typ, side, *x, **k: reddet(s, typ, side) if typ == "market" and side == "sell" else _al(s, typ, side, *x, **k)
b5.portfolio()
check("her coin satisi reddedildi: bitmis sayilmaz", geciken(b5) == ["LIVE"] and a5.pf_err >= 1 and "SOL/USDT" in a5.st["positions"], b5.ag["pf"]["result"]["LIVE"])
b5, a5, ex5, _ = fresh()
ex5.create_order = reddet
b5.portfolio()
check("alim borsada reddedildi: bitmis sayilmaz", geciken(b5) == ["LIVE"] and a5.pf_err >= 1, b5.ag["pf"]["result"]["LIVE"])
del ex5.create_order
b5.portfolio()
check("sonraki basarili tur gecikmeyi kaldirir", geciken(b5) == [] and a5.pf_err == 0, b5.ag["pf"]["son"])
b5, a5, ex5, _ = fresh()
_m, C.LIVE_MAX_USDT = C.LIVE_MAX_USDT, 10.0
b5.portfolio()
C.LIVE_MAX_USDT = _m
check("tutar siniri (bilerek gonderilmeyen alim) gecikme sayilmaz", geciken(b5) == [] and not a5.st["positions"], b5.ag["pf"]["result"]["LIVE"])
b5, a5, ex5, _ = fresh()
ex5.bal_err = ccxt.NetworkError("zaman asimi")
a5._bal_retry = lambda: False
b5.portfolio()
ex5.bal_err = None
check("bakiye okunamadi, alimlar yapilmadi: bitmis sayilmaz", geciken(b5) == ["LIVE"], b5.ag["pf"]["result"]["LIVE"])
a5.own = False
check("hesap kilidi baska motorda: bu motor beklemez", geciken(b5) == [])
a5.own = True
_pl, C.PORTFOY_LIVE = C.PORTFOY_LIVE, False
check("portfoy gercek hesapta kapali: beklenmez", geciken(b5) == [])
C.PORTFOY_LIVE = _pl
_la, C.LIVE_ALLOWED = C.LIVE_ALLOWED, False
check("gercek islem izni kapali: beklenmez", geciken(b5) == [])
C.LIVE_ALLOWED = _la

# ---- pf_buy gerekcesi gercek hesapta da korunur ----
b5, a5, ex5, _ = fresh()
ex5.set("BTC", free=0.01)
a5.balance_check()
a5.st["positions"][BTC] = pos_(0.01)
r = a5.pf_buy(BTC, 1000.0, "GEREKCE-CANLI")
check("gercek hesap artirma: gerekce bildirimde, None yok", r is None and any("ARTIRILDI" in n["text"] and "GEREKCE-CANLI" in n["text"] for n in b5.notes) and not any("| None" in n["text"] for n in b5.notes), r)
PF.targets = real_t
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
