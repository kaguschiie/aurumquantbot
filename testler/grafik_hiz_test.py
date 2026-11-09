"""Grafik hizi: onbellek kilidi, REST hatasi, likidasyon arka planda, son=1, state git calistirmaz (ag ve motor sahte)."""
import sys
import threading
import time

sys.path.insert(0, sys.argv[1])
import app
import guncelle
import requests
import stream
import zincir

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, info)


# anahtar basina kilit: ayni anahtarda tek cagri
n = []
def yavas():
    n.append(1)
    time.sleep(0.2)
    return 7
th = [threading.Thread(target=lambda: app.cached("a", 60, yavas)) for _ in range(6)]
[t.start() for t in th]
[t.join() for t in th]
check("6 es zamanli cagri, tek dis istek", len(n) == 1, len(n))

# ic ice farkli anahtarlar kilitlenmez; iki yonlu sira degil, tek yon
sonuc = []
t = threading.Thread(target=lambda: sonuc.append(app.cached("dis", 60, lambda: app.cached("ic", 60, lambda: 3))), daemon=True)
t.start()
t.join(3)
check("ic ice farkli anahtar kilitlenmiyor", sonuc == [3], sonuc)

# farkli anahtar yavas olana takilmaz
t = threading.Thread(target=lambda: app.cached("yavas", 60, lambda: time.sleep(1)), daemon=True)
t.start()
time.sleep(0.05)
t0 = time.time()
app.cached("hizli", 60, lambda: 1)
check("farkli anahtar yavas anahtari beklemiyor", time.time() - t0 < 0.3, round(time.time() - t0, 3))
t.join()


# hata: onceki deger korunur, onceki deger yoksa hata
def patla():
    raise requests.ConnectionError("yok")
app.CACHE["h"] = (time.time() - 100, [1, 2])
check("hata atarsa onceki deger", app.cached("h", 15, patla) == [1, 2])
check("hatadan sonra hemen yeniden denemez", time.time() - app.CACHE["h"][0] < 1)
try:
    app.cached("h2", 15, patla)
    check("onceki deger yoksa hata yukari gider", False)
except requests.ConnectionError:
    check("onceki deger yoksa hata yukari gider", True)


# _klines ve candles: sahte Binance
class R:
    def __init__(self, j, code=200):
        self.j, self.status_code = j, code
    def json(self):
        return self.j
    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(str(self.status_code))


ist = []
T0 = 1700000000000
def sahte_get(url, params=None, timeout=None):
    ist.append((url, params, timeout))
    return R([[T0 + i * 3600000, "1", "2", "0.5", str(1.5 + i), "10", 0, "0", 0, "0", "0", "0"] for i in range(500)])
requests.get = sahte_get
c = app._klines("ETH/USDT", "1h")
check("klines dogrudan REST, timeout 10", ist[-1][1] == {"symbol": "ETHUSDT", "interval": "1h", "limit": 500} and ist[-1][2] == 10, ist[-1][1:])
check("ccxt satir bicimi", c[0] == [T0, 1.0, 2.0, 0.5, 1.5, 10.0], c[0])
check("ccxt ve exchange() kalkti", not hasattr(app, "exchange"))


class SahteStream:
    kline = {}
    def watch_kline(self, s, tf):
        pass
stream.Stream.shared = classmethod(lambda cls: SahteStream())
z = zincir.Zincir.shared()
bekle = threading.Event()
liq_n = []
def sahte_liq(sym, ttl=600):
    liq_n.append(sym)
    bekle.wait(5)
    z.liq[sym] = {"t": time.time(), "price": 1, "long": [[1.0, 0.9, 5]], "short": [[2.0, 0.8, 5]]}
    return z.liq[sym]
z.liq_map = sahte_liq
app.ENGINE.bot = None
t0 = time.time()
o = app.candles("ETH/USDT", "1h", "PAPER")
check("likidasyon mumu bekletmiyor", time.time() - t0 < 1 and len(o["candles"]) == 500 and "liq" not in o, round(time.time() - t0, 3))
for _ in range(3):
    app.candles("ETH/USDT", "1h", "PAPER")
app.CACHE.pop("liq:ETH/USDT")
app.candles("ETH/USDT", "1h", "PAPER")  # is suruyor: ikinci is parcacigi baslamamali
time.sleep(0.1)
check("coin basina tek is parcacigi", len(liq_n) == 1, liq_n)
bekle.set()
time.sleep(0.2)
o = app.candles("ETH/USDT", "1h", "PAPER", True)
check("sonraki yoklamada likidasyon gelir", o.get("liq", {}).get("long") == [[1.0, 0.9, 5]] and len(o["levels"]) == 2, o.get("liq"))
check("son=1: son 2 mum", [x["close"] for x in o["candles"]] == [1.5 + 498, 1.5 + 499], o["candles"][-1])
SahteStream.kline = {("ETH/USDT", "1h"): {"time": T0 // 1000 + 500 * 3600, "open": 1, "high": 2, "low": 1, "close": 9, "volume": 1, "rt": time.time()}}
o = app.candles("ETH/USDT", "1h", "PAPER", True)
check("son=1: canli yeni mum dahil, en cok 2", len(o["candles"]) == 2 and o["candles"][-1]["close"] == 9, len(o["candles"]))
check("tam istek 501 mum", len(app.candles("ETH/USDT", "1h", "PAPER")["candles"]) == 501)
z.liq.pop("XRP/USDT", None)
liq_n.clear()
z.liq["XRP/USDT"] = {"t": time.time() - 700, "price": 1, "long": [], "short": [], "none": True}
app.candles("XRP/USDT", "1h", "PAPER")
time.sleep(0.1)
check("600 sn'den eski harita arka planda yenilenir (liq_map suresi ayni)", liq_n == ["XRP/USDT"], liq_n)

# state: git calistirmaz, beyin durumu beklemez, update anlami ayni
guncelle.status = lambda: (_ for _ in ()).throw(AssertionError("git calisti"))
sess = {"user": "x", "role": "admin"}
guncelle.STATE["latest"] = {"version": "abc", "newer": True}
check("yeni surum: admin gorur", app.state(sess, {})["update"] == "abc")
check("yeni surum: kullanici gormez", app.state({"user": "y", "role": "user"}, {})["update"] is None)
guncelle.STATE["latest"] = {"version": "abc", "newer": False}
check("yeni degilse update yok", app.state(sess, {})["update"] is None)
guncelle.STATE["latest"] = None
check("latest yoksa update yok", app.state(sess, {})["update"] is None)

import agent as A
kapi = threading.Event()
def yavas_status():
    kapi.wait(5)
    return True, "Claude CLI"
A.status, A.brain = yavas_status, lambda: "cli"
app.CACHE.pop("claude", None)
t0 = time.time()
d = app.beyin_durumu()
check("beyin: ilk deger bekletmez", time.time() - t0 < 0.5 and d == (True, "Beyin kontrol ediliyor", None), d)
kapi.set()
time.sleep(0.2)
check("beyin: arka planda dolar", app.beyin_durumu() == (True, "Claude CLI", "cli"), app.beyin_durumu())
app.CACHE.pop("claude", None)  # ayar kaydinda oldugu gibi: yeniden kontrol
A.status = lambda: (True, "Claude API")
A.brain = lambda: "api"
app.beyin_durumu()
time.sleep(0.2)
check("beyin: ayar sonrasi yenilenir", app.beyin_durumu()[2] == "api")

print("\nSONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
