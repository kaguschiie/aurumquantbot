"""2026-11-09: toplam acik risk siniri, bayat fiyat kapisi (alim + yerel cikis), log maskesi. Sahte borsa, PAPER; ag yok."""
import logging
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
import config as C
import bot as B
import dersler as L
import portfoy as PF

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:240], flush=True)


class FakeEx:
    def price_to_precision(self, s, p):
        return f"{p:.4f}"

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"

    def market(self, s):
        return {"limits": {"cost": {"min": 5}}}

    def create_order(self, *a, **k):
        raise AssertionError("sanal hesap emir veremez")


class Stub:
    def __init__(self):
        self.ex = FakeEx()
        self.bid, self.ask, self.px_t = {}, {}, {}
        self.market, self.lock, self.stop, self.accounts, self.notes = {}, threading.RLock(), threading.Event(), {}, []

    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        self.notes.append(msg)

    def save(self):
        pass


L.init()
SYMS = [f"C{i}/USDT" for i in range(8)]


def hesap():
    b = Stub()
    for s in SYMS + ["BTC/USDT"]:
        b.bid[s] = b.ask[s] = 100.0
        b.px_t[s] = time.time()
    a = B.Account(b, "PAPER", {})
    b.accounts["PAPER"] = a
    a.st["cash"] = 10_000.0
    a.st["principal"] = a.st["peak"] = 10_000.0
    return a, b


def plan(s, stop=90.0, kind="sinyal", risk=1.0):
    return {"type": "buy", "symbol": s, "kind": kind, "entry": "taker", "stop": stop, "tp2": 110.0, "tp1": 0, "risk_pct": risk,
            "max_hours": 48, "reason": "test", "conviction": None, "max_pos_pct": 100}


# 1) toplam acik risk
a, b = hesap()
C.MAX_POSITIONS, C.MAX_POS_PCT = 20, 100.0
risk = lambda: sum(p["qty"] * (p["entry"] - p["sl"]) for p in a.st["positions"].values() if p.get("kind") != PF.KIND) / a.equity() * 100
res, red = [], None
for s in SYMS[:7]:
    r = a.enter(s, plan(s))
    res.append(r)
    if r:
        red = s
        break
check("islemler acilir, toplam acik risk hic %5'i asmaz", 3 <= len(a.st["positions"]) <= 7 and risk() <= 5.0 + 1e-9, (len(a.st["positions"]), round(risk(), 2)))
check("sinira tasacak ilk islem reddedilir, gerekce yazilir", red and "toplam açık risk" in res[-1] and red not in a.st["positions"], res[-1])
SYMS6 = red
r7 = a.enter(SYMS6, plan(SYMS6, kind=PF.KIND))
check("portfoy alimi acik risk sinirina takilmaz (tasarim geregi felaket stopu)", r7 is None and SYMS6 in a.st["positions"], r7)
for s in [x for x in a.st["positions"] if a.st["positions"][x].get("kind") != PF.KIND]:
    a.st["positions"][s]["sl"] = a.st["positions"][s]["entry"] * 1.001  # kalkan basabasa cekti: risk 0
r8 = a.enter("C7/USDT", plan("C7/USDT"))
check("basabastaki pozisyonlar risk saymaz, yeni islem acilir", r8 is None, r8)
check("sinir ayardan okunur", C.MAX_OPEN_RISK_PCT == 5.0)
a, b = hesap()
for s in SYMS[:6]:
    a.enter(s, plan(s))
rm = a.enter("C6/USDT", dict(plan("C6/USDT", stop=65.0), usdt=2000, manual=True))
check("elle alim (felaket stopu, buyuk tutar) sinira takilmaz", rm is None and "C6/USDT" in a.st["positions"], rm)
ra = a.enter("C7/USDT", dict(plan("C7/USDT", stop=92.0), usdt=3000, ai=True))
check("yapay zekaya birakilan elle alim sinira takilmaz", ra is None, ra)
a, b = hesap()
a.enter("C0/USDT", dict(plan("C0/USDT", stop=65.0), usdt=3000, manual=True))
check("acik elle pozisyon otomatik alimlari kilitlemez", a.enter("C1/USDT", plan("C1/USDT")) is None)
a, b = hesap()
a.st["orders"]["x1"] = {"s": "C5/USDT", "side": "buy", "typ": "market", "t": time.time(),
                        "act": {"do": "enter", "p": {"qty": 50.0, "price": 100.0, "stop": 90.0, "kind": "sinyal"}}}  # %5 risk, sonucu bekliyor
r = a.enter("C0/USDT", plan("C0/USDT"))
check("sonucu bekleyen alim emri acik riske sayilir", r and "toplam açık risk" in r, r)

# 2) bayat fiyat
a, b = hesap()
b.px_t["C0/USDT"] = time.time() - 120
r = a.enter("C0/USDT", plan("C0/USDT"))
check("120 sn bayat fiyatla alim yok", r and "güncellenmiyor" in r and not a.st["positions"], r)
b.px_t["C0/USDT"] = time.time()
b.ex.fetch_ticker = lambda s: {"bid": 101.0, "ask": 101.0, "last": 101.0}
b.px_t["C2/USDT"] = time.time() - 999
r2 = a.enter("C2/USDT", plan("C2/USDT"))
check("izlemeden dusmus coin: bayatsa borsadan okunur, alim olur", r2 is None and time.time() - b.px_t["C2/USDT"] < 5 and b.ask["C2/USDT"] == 101.0, r2)
del b.ex.fetch_ticker
check("taze fiyatla alim olur", a.enter("C0/USDT", plan("C0/USDT")) is None)
p = a.st["positions"]["C0/USDT"]
b.bid["C0/USDT"] = 90.0  # stopun altinda
b.px_t["C0/USDT"] = time.time() - 120
p["seen"] = seen0 = time.time() - 500
a._manage("C0/USDT", p)
check("bayat fiyatla yerel stop karari verilmez", "C0/USDT" in a.st["positions"])
check("kesintide seen ilerlemez (yeniden baslatmada bosluk oynatilir)", p["seen"] == seen0)
b.px_t["C0/USDT"] = time.time()
a._manage("C0/USDT", p)
check("fiyat tazelenince stop calisir", "C0/USDT" not in a.st["positions"] and a.st["trades"][-1]["reason"] == "STOP", a.st["trades"][-1:])
del b.px_t["C1/USDT"]
check("tazelik bilgisi olmayan (eski kayit) kapidan gecer", a.enter("C1/USDT", plan("C1/USDT")) is None)

# 3) log maskesi
kayit = []


class Tut(logging.Handler):
    def emit(self, r):
        kayit.append(self.format(r))


h = Tut()
h.addFilter(C.Maske())
lg = logging.getLogger("maske_test")
lg.addHandler(h)
lg.warning("Ag hatasi: %s", "GET https://api.binance.com/api/v3/account?timestamp=1&signature=0a1b2c3d4e")
lg.warning("tg https://api.telegram.org/bot123456789:AAH_x-y/sendMessage")
lg.warning("anahtar sk-ant-api03-abc_DEF-123 ve {'X-MBX-APIKEY': 'abcDEF123'}")
lg.warning("normal metin %d", 5)
check("imza maskelenir", "signature=***" in kayit[0] and "0a1b2c" not in kayit[0], kayit[0])
check("Telegram jetonu maskelenir", "/bot***/sendMessage" in kayit[1] and "AAH" not in kayit[1], kayit[1])
check("Claude anahtari ve API anahtari basligi maskelenir", "abc_DEF" not in kayit[2] and "abcDEF123" not in kayit[2], kayit[2])
check("normal metin degismez", kayit[3] == "normal metin 5", kayit[3])
h.setFormatter(logging.Formatter("%(message)s"))
try:
    raise RuntimeError("binance GET https://api.binance.com/api/v3/order?x=1&signature=deadbeef99")
except RuntimeError:
    lg.exception("Dongu hatasi")
check("traceback icindeki imza da maskelenir", "deadbeef99" not in kayit[-1] and "signature=***" in kayit[-1], kayit[-1][-120:])
src = open(sys.argv[1] + "/app.py", encoding="utf-8").read() + open(sys.argv[1] + "/agresif.py", encoding="utf-8").read()
check("maske uygulama ve agresif gunluk isleyicisine bagli", src.count("addFilter(C.Maske())") == 2)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
