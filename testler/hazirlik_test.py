"""Gercek paraya hazirlik raporu. Sahte borsa: ag yok, anahtar yok, gercek emir yok."""
import csv
import json
import os
import shutil
import sys
import threading
import time

PROJ = sys.argv[1]
sys.path.insert(0, PROJ)
import ccxt
import config as C
import hazirlik as H

ok_all = True
D = os.path.join(C.APP_DIR, "hz_test")
shutil.rmtree(D, ignore_errors=True)
os.makedirs(D)


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


class Ex:
    def __init__(self, red=None, yedek_red=None, auth=None):
        self.calls, self.red, self.yedek_red, self.auth, self.emir = [], red, yedek_red, auth, 0

    def create_order(self, *a, **k):
        self.emir += 1
        raise AssertionError("gercek emir yolu kullanilmamali")

    def private_post_order_test(self, p):
        self.calls.append(p)
        if self.auth:
            raise self.auth
        if p["type"] == "STOP_LOSS_LIMIT" and self.red:
            raise self.red
        if p["type"] == "STOP_LOSS" and self.yedek_red:
            raise self.yedek_red
        return {}

    def fetch_ticker(self, s):
        return {"last": 100.0}

    def market(self, s):
        return {"id": s.replace("/", ""), "limits": {"cost": {"min": 5}}}

    def price_to_precision(self, s, p):
        return f"{p:.2f}"

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"


class A:
    def __init__(self, ex=None, eq=1000.0, key_ok=True):
        self.ex, self.eq, self.key_ok, self.key_na, self.key_err, self.synced, self.error = ex or Ex(), eq, key_ok, False, "", True, ""

    def equity(self):
        return self.eq


class B:
    def __init__(self, lv=None):
        self.accounts = {"PAPER": object(), **({"LIVE": lv} if lv else {})}


def csv_yaz(rows, ad="gozlem.csv"):
    f = os.path.join(D, ad)
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["zaman", "hesap", "kasa_net", "btc", "engel", "uyarilar"])
        w.writerows(rows)
    return f


# ---- gozlem ----
check("kayit yoksa None", H.gozlem(os.path.join(D, "yok.csv")) is None)
f = csv_yaz([["2026-11-02 23:00", "PAPER", 500, 50, "", ""],  # baslangictan once: sayilmaz
             ["2026-11-03 00:00", "PAPER", 1000, 100, "", ""], ["2026-11-03 00:00", "LIVE", 9, 100, "", ""],
             ["2026-11-03 01:00", "PAPER", "bozuk", 100, "", ""],
             ["2026-11-03 02:00", "PAPER", 1100, 101, "", "a uyari | b uyari"], ["2026-11-03 03:00", "PAPER", 990, 99, "kilit", "a uyari"],
             ["2026-11-04 00:00", "PAPER", 1050, 110, "", ""]])
g = H.gozlem(f)
check("baslangic oncesi ve baska hesap sayilmaz", g["kasa_bas"] == 1000 and g["bas"] == "2026-11-03 00:00", g)
check("getiri ve BTC", g["kasa_pct"] == 5.0 and g["btc_pct"] == 10.0, g)
check("en derin dusus zirveden", g["dusus"] == 10.0, g["dusus"])
check("gun, engel saati", g["gun"] == 1.0 and g["engel_saat"] == 1, g)
check("uyarilar sayilir, siklikla", g["uyarilar"][0] == {"m": "a uyari", "n": 2}, g["uyarilar"])
check("tek satir yetmez", H.gozlem(csv_yaz([["2026-11-03 00:00", "PAPER", 1000, 100, "", ""]], "t.csv")) is None)

# ---- dengeleme ----
K = os.path.join(D, "defter")
os.makedirs(K)
for gun, sat in (("2026-11-03", [{"hesap": "PAPER", "asama": "karar"}, {"hesap": "PAPER", "asama": "sonuc", "sonuc": ["BTC al"]}]),
                 ("2026-11-05", [{"hesap": "LIVE", "asama": "sonuc", "sonuc": ["x"]}, {"hesap": "PAPER", "asama": "sonuc", "sonuc": None}]),
                 ("2026-11-06", [{"hesap": "PAPER", "asama": "sonuc", "sonuc": ["borsa hatası: x"]}])):
    with open(os.path.join(K, gun + ".jsonl"), "w", encoding="utf-8") as fh:
        for r in sat:
            fh.write(json.dumps(r) + "\n")
        fh.write("bozuk satir\n")
dg = {x["g"]: x for x in H.dengelemeler(K, "2026-11-03", "2026-11-07")}
check("tamam gun", dg["2026-11-03"]["ok"] and not dg["2026-11-03"]["hata"])
check("dosyasiz gun eksik", not dg["2026-11-04"]["ok"])
check("yarim kalan ve baska hesap sayilmaz", not dg["2026-11-05"]["ok"])
check("hatali gun yakalanir", dg["2026-11-06"]["hata"], dg["2026-11-06"])
uzun = H.dengelemeler(K, "2026-01-01", "2026-12-31")
check("uzun donemde gun atlanmaz", len({x["g"] for x in uzun}) == len(uzun) >= 364, len(uzun))

# ---- binance kontrolu ----
ex = Ex()
r = H.binance_kontrol(A(ex), {"BTC/USDT": 100.0, "ETH/USDT": 100.0})
p = ex.calls[0]
check("gecerse ok, iki coin", r["ok"] and len(r["koin"]) == 2 and len(ex.calls) == 2, r)
check("canli stopla ayni: %65 stop, limit %0,5 alti", p["type"] == "STOP_LOSS_LIMIT" and p["stopPrice"] == "65.00" and p["price"] == "64.67" and p["timeInForce"] == "GTC" and p["side"] == "SELL", p)
check("gercek emir gitmez", ex.emir == 0)
ex = Ex(red=ccxt.InvalidOrder("PERCENT_PRICE_BY_SIDE"))
r = H.binance_kontrol(A(ex), {"BTC/USDT": 100.0, "ETH/USDT": 100.0})
check("red: ok degil, yedek denenir", not r["ok"] and r["koin"][0]["yedek"] is True and "PERCENT" in r["koin"][0]["hata"], r)
ex = Ex(red=ccxt.InvalidOrder("a"), yedek_red=ccxt.InvalidOrder("b"))
r = H.binance_kontrol(A(ex), {"BTC/USDT": 100.0, "ETH/USDT": 100.0})
check("yedek de red", r["koin"][0]["yedek"] is False)
r = H.binance_kontrol(A(Ex(auth=ccxt.AuthenticationError("-2015"))), {"BTC/USDT": 100.0})
check("anahtar reddi: ayri mesaj", not r["ok"] and "anahtarı" in r["hata"], r)
ex = Ex()
ex.fetch_ticker = lambda s: (_ for _ in ()).throw(ccxt.NetworkError("yok"))
r = H.binance_kontrol(A(ex), {})
check("ag hatasi: gecti sayilmaz", not r["ok"] and r["koin"][0].get("belirsiz"), r)

# ---- rapor ----
_g, _d = H.gozlem, H.dengelemeler
iyi_g = {"gun": 7.5, "kapsam": 99, "dusus": 5.0, "uyarilar": []}
H.gozlem, H.dengelemeler = lambda: dict(iyi_g), lambda: [{"g": "2026-11-03", "ok": True, "hata": []}]
C.API_KEY, C.LIVE_MAX_USDT, C.LIVE_MAX_EQUITY = "sahte", 500.0, 0.45
tamam = {"ok": True, "koin": []}
rp = H.rapor(B(A()), tamam)
check("hepsi tamamsa hazir", rp["hazir"], rp["maddeler"])
check("rapor ayar degistirmez, anahtar icermez", "sahte" not in json.dumps(rp) and rp["ayar"]["LIVE_ALLOWED"] is False, rp["ayar"])
durum = lambda r, ad: next(m["durum"] for m in r["maddeler"] if m["ad"] == ad)
check("kontrol yapilmadan hazir degil", not H.rapor(B(A())).get("hazir") and durum(H.rapor(B(A())), "Felaket stopu (Binance)") == "bekle")
rr = H.rapor(B(A()), {"ok": False, "koin": []})
check("stop reddi uyari", not rr["hazir"] and durum(rr, "Felaket stopu (Binance)") == "uyari")
check("anahtar yoksa bekle", not H.rapor(B()).get("hazir") and durum(H.rapor(B()), "Binance anahtarı") == "bekle")
C.LIVE_MAX_USDT = 100.0
check("sinir kasaya kucukse uyari", durum(H.rapor(B(A()), tamam), "Tek emir sınırı") == "uyari" and not H.rapor(B(A()), tamam)["hazir"])
C.LIVE_MAX_USDT = 500.0
check("cekim izni acik uyari", durum(H.rapor(B(A(key_ok=False)), tamam), "Para çekme izni") == "uyari")
C.LIVE_MAX_EQUITY = 0.40
check("kasa payi %40 sinirina takilma uyarisi", durum(H.rapor(B(A()), tamam), "Kasa payı sınırı") == "uyari")
C.LIVE_MAX_EQUITY = 0.45
H.gozlem = lambda: {**iyi_g, "gun": 3.0}
check("kisa gozlem bekle", durum(H.rapor(B(A()), tamam), "Sanal gözlem süresi") == "bekle")
H.gozlem = lambda: {**iyi_g, "dusus": 40.0}
check("buyuk dusus uyari", durum(H.rapor(B(A()), tamam), "Düşüş") == "uyari")
H.gozlem, H.dengelemeler = lambda: None, lambda: []
rb = H.rapor(B())
check("kayit yokken patlamaz", not rb["hazir"] and rb["gozlem"] is None)
H.gozlem, H.dengelemeler = _g, _d

# ---- erisim ----
src = open(os.path.join(PROJ, "app.py"), encoding="utf-8").read()
check("sadece admin rotasi", 'routes["/api/hazirlik"]' in src and src.index("if admin:\n                routes[\"/api/users\"]") < src.index('routes["/api/hazirlik"]') or 'if admin:\r\n' in src)
hz = open(os.path.join(PROJ, "hazirlik.py"), encoding="utf-8").read()
check("hazirlikta gercek emir cagrisi yok", "create_order" not in hz and ".order(" not in hz)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
