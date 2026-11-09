"""Kotu veri ertelenir, tasfiye etmez: bayat/eksik mum, evren, model yasi, ic None, atomik model, gunde bir egitim, kullanici uyarisi.
Sahte borsa: ag yok, emir yok, anahtar yok. Kullanim: KQB_APP_DIR=<test> python -I veri-model_test.py <proje>"""
import os
import shutil
import sys
import tempfile
import threading
import time
import zlib

sys.path.insert(0, sys.argv[1])
import config as C
for f in os.listdir(C.APP_DIR):
    if f.startswith(("state", "durdur", "ders_defteri", "gecmis", "trading_memory", "haberler")):  # haber_test'in acil durum engeli buraya tasmasin
        os.remove(os.path.join(C.APP_DIR, f))
import joblib
import numpy as np
import agent as A
import bot as B
import portfoy as PF

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


DAY = 86_400_000
TMP = tempfile.mkdtemp(prefix="vm_", dir=os.path.dirname(os.path.abspath(__file__)))
PF.TRAIN_MARK = os.path.join(TMP, "deneme")
PF.MODEL_FILE = os.path.join(TMP, "model.joblib")
PF.BUNDLED = os.path.join(TMP, "paketli.joblib")
PF.DATA = os.path.join(TMP, "veri")
os.makedirs(PF.DATA)


def series(sym, n=500):
    r = np.random.RandomState(zlib.crc32(sym.encode()))
    core = sym.startswith(("BTC", "ETH"))  # cekirdek yukselis trendinde: rejim acik
    return 100 * np.exp(np.cumsum((0.004 if core else r.uniform(-0.002, 0.003)) + r.normal(0, 0.01 if core else 0.02, n)))


class FakeEx:
    """publicGetKlines Binance gibi: son limit satir, sondaki olusan gun mumu dahil."""

    def __init__(self, n_alt=60):
        self.syms = ["BTC/USDT", "ETH/USDT"] + [f"A{i:02d}/USDT" for i in range(n_alt)]
        self.back, self.short, self.empty, self.flat, self.boom, self.orders = {}, {}, set(), set(), set(), []
        self.crash = set()  # son 12 gunde %40 dusus: tum ortalamalarin altinda

    def publicGetKlines(self, p):
        sym = p["symbol"].replace("USDT", "/USDT")
        if sym in self.boom:
            raise ValueError("bozuk cevap")
        if sym in self.empty:
            return []
        c = series(sym)
        if sym in self.flat:
            c = c.copy()
            c[-40:] = c[-41]
        if sym in self.crash:
            c = c.copy()
            c[-12:] *= 0.6
        today = int(time.time() // 86400) * DAY
        rows = [[today - (len(c) - 1 - i) * DAY, x, x * 1.01, x * 0.99, x, 1.0, 0, 2e7 + 1e5 * (zlib.crc32(sym.encode()) % 100), 1, 1.0, 1e7, 0]
                for i, x in enumerate(c)]
        rows = rows[:len(rows) - self.back.get(sym, 0)]
        if sym in self.short:
            rows = rows[-self.short[sym]:]
        return rows[:1] if p.get("startTime") == 0 else rows[-p["limit"]:]

    def fetch_tickers(self):
        return {s: {"quoteVolume": 1e9 - i} for i, s in enumerate(self.syms)}

    def fetch_ticker(self, s):
        x = float(series(s)[-1])
        return {"bid": x, "ask": x, "last": x}

    def fetch_ohlcv(self, *a, **k):
        return []

    def amount_to_precision(self, s, q):
        return f"{q:.6f}"

    def price_to_precision(self, s, p):
        return f"{p:.6f}"

    def market(self, s):
        return {"limits": {"cost": {"min": 5}}}

    def create_order(self, *a, **k):
        self.orders.append(a)
        raise AssertionError("testte emir yok")


class FakeModel:
    def predict(self, X):
        return X["r7"].fillna(0).values


def fresh_model(**kw):
    PF._M.clear()
    PF._M.update({"model": FakeModel(), "feats": PF.FEATS, "v": PF.MODEL_V, "t": time.time(), "n": 1, "end": "x", "ic": 0.2}, **kw)


A.brain = lambda: "local"
A.cli_ready = lambda: False
C.TELEGRAM_TOKEN = ""
C.PAPER_BALANCE = 10_000.0
_real_maybe = PF.maybe_train
PF.maybe_train = lambda say: None  # egitim sadece kendi bolumunde
_real_sync, _real_train = PF.sync_data, PF.train
PF.sync_data = PF.train = lambda *a: (_ for _ in ()).throw(AssertionError("ag yok"))
ex = FakeEx()
b = B.Bot()
b.ex = ex
pa = b.accounts["PAPER"]
pa.ex = ex
pa.st.update(cash=10_000.0, positions={}, trades=[])
pa.mark()
check("sadece sanal hesap; gercek hesap ayarlari kapali", list(b.accounts) == ["PAPER"] and not C.PORTFOY_LIVE and not C.LIVE_ALLOWED)
fresh_model()


def snap():
    return round(pa.st["cash"], 6), {s: p["qty"] for s, p in pa.st["positions"].items()}, len(pa.st["trades"])


def alerts(word=""):
    return [n["text"] for n in b.notes if n["kind"] == "uyari" and word in n["text"]]


def run():
    b.ag["pf_d"], b.ag["pf_wait"] = "bugun", 0
    assert b.run_portfolio()
    b._pf_thread.join(30)


# ---- 1) saglam veri: sonuc degismez ----
st = {}
tg, info = PF.targets(ex, st, True)
cw = {s: PF.core_weight(PF.klines(ex, s, 260)["c"])[0] for s in PF.CORE_SYMS}
check("saglam veri: cekirdek agirlik eski hesapla ayni", all(abs(tg[s] - round(C.PORTFOY_CORE * 0.5 * cw[s], 4)) < 1e-9 for s in cw), tg)
check("saglam veri: 5 ML secimi, uyari yok, durum calisiyor", len(st["ml"]["picks"]) == 5 and not info["uyari"] and st["ml"]["durum"] == "calisiyor", info["uyari"])
check("evren gozlemi kaydedildi (sahte hacimde esitlik olabilir)", 45 <= st["ml_n"] <= 50, st["ml_n"])
top = sorted(((s, float(series(s)[-1] / series(s)[-8] - 1)) for s in ex.syms), key=lambda x: -x[1])
b.ag["pf"] = {}
b.portfolio(force_ml=True)
pa_pos0 = snap()
check("ilk dengeleme pozisyon acti", len(pa.st["positions"]) >= 5, list(pa.st["positions"]))
week_key = b.ag["pf"]["ml_hafta"]

# ---- 2) bayat / eksik cekirdek verisi: o coine dokunulmaz, gerisi islenir, sadece o coin tekrar denenir ----
def reset():
    pa.st.update(cash=10_000.0, positions={}, trades=[])
    b.bid.clear()
    b.ask.clear()
    b.ag["pf"] = {}
    for k in ("pf_bekle", "pf_uyari", "pf_wait"):
        b.ag.pop(k, None)
    b.ag["pf_d"] = ""
    ex.back.clear(), ex.short.clear(), ex.empty.clear(), ex.flat.clear(), ex.crash.clear()
    b.portfolio(force_ml=True)


def qty(s):
    return (pa.st["positions"].get(s) or {}).get("qty")


def tick_run(retry):
    assert b.run_portfolio(retry)
    b._pf_thread.join(30)


class Clock:
    """pf_tick zaman kapisi testin saatine bagli olmasin: gunun ilk 5 dk'si disinda."""
    def __getattr__(self, k):
        return getattr(time, k)

    def time(self):
        now = time.time()
        return max(now, now // 86400 * 86400 + 301)


real_review = B.KT.review
reviews = []
B.KT.review = lambda ask, buys, ctx: (reviews.append([x[0] for x in buys]) or ("", {}, "", 0.0))
BTC, ETH = "BTC/USDT", "ETH/USDT"
for name, setup in [
    ("ETH son mumu 3 gun once", lambda: ex.back.update({ETH: 3})),
    ("ETH seri kisa (100 gun)", lambda: ex.short.update({ETH: 100})),
    ("ETH bos cevap", lambda: ex.empty.add(ETH)),
    ("ETH donuk fiyat", lambda: ex.flat.add(ETH)),
]:
    reset()
    check(f"{name}: baslangicta BTC, ETH, ML elde", qty(BTC) and qty(ETH) and len(pa.st["positions"]) >= 5)
    eth0, week0 = qty(ETH), b.ag["pf"]["ml_hafta"]
    ml0 = [s for s in pa.st["positions"] if s not in PF.CORE_SYMS]
    setup()
    ex.crash.add(BTC)  # BTC ortalamalarin altina: cekirdek payi duser, ML rejimi kapanir
    b.ag["pf"]["ml_hafta"] = "2000-01"
    n_a = len(alerts("yeniden denenecek"))
    run()
    check(f"{name}: BTC azaltildi + ML rejim cikisi yapildi, ETH'e dokunulmadi", qty(BTC) is None and qty(ETH) == eth0 and not any(s in pa.st["positions"] for s in ml0),
          list(pa.st["positions"]))
    check(f"{name}: yeniden denenecek, tek uyari", b.ag["pf_d"] == "" and b.ag["pf_wait"] > time.time() + 300 and b.ag["pf_bekle"]["syms"] == [ETH]
          and len(alerts("yeniden denenecek")) == n_a + 1 and "alım da satım da yapılmadı" in alerts("yeniden denenecek")[-1], alerts("yeniden denenecek")[-1:])
    check(f"{name}: ekranda ETH simdiki agirlik, panelde sebep", abs(b.ag["pf"]["targets"][ETH] - b._pf_now()[ETH]) < 0.01 and ETH in b.ag["pf"]["bekle"], b.ag["pf"]["targets"])
    check(f"{name}: son bilinen trend korunur (haber kapisi)", ETH in b.ag["pf"]["info"]["core"], b.ag["pf"]["info"]["core"].keys())

# hic elde yokken bayat: alim yok
reset()
pa.st.update(cash=10_000.0, positions={}, trades=[])
ex.back[ETH] = 2
run()
check("ETH bayat, elde yok: ETH alinmaz, BTC alinir", qty(ETH) is None and qty(BTC), list(pa.st["positions"]))

# yeniden denemede: duzelmeyen = islem yok, publish yok, Claude yok; duzelen = sadece o coin
reset()
real_brain = A.brain
A.brain = lambda: "cli"
ex.back[ETH] = 2
pa.st["cash"] += pa.st["positions"].pop(ETH)["qty"] * b.bid[ETH]
reviews.clear()
run()
n_r = len(reviews)
check("ilk tur: kontrolore bozuk coin sorulmaz", all(ETH not in r for r in reviews) and n_r <= 1, reviews)
s0, dec0 = snap(), len(b.ag["decisions"])
tick_run(True)
tick_run(True)
check("duzelmeyince: islem yok, karar kaydi yok, Claude cagrisi yok", snap() == s0 and len(b.ag["decisions"]) == dec0 and len(reviews) == n_r, (s0 == snap(), len(reviews)))
check("duzelmeyince: bekleme yenilenir, uyari tekrar etmez", b.ag["pf_d"] == "" and b.ag["pf_wait"] > time.time() + 300)
others = {s: q for s, q in snap()[1].items()}
cash0 = pa.st["cash"]
A.brain = lambda: "local"
reviews.clear()
ex.back.clear()
ex.crash.add(BTC)  # duzelen ETH disinda hedef degisse de bu turda sadece ETH islenir
tick_run(True)
check("duzelince: ETH alinir, BTC aynen; 3 kapanis altinda sepet cikar", qty(ETH) and qty(BTC) == others[BTC] and not (set(pa.st["positions"]) - {ETH, BTC}), list(pa.st["positions"]))
check("duzelince: bugunku tur biter", b.ag["pf_d"] == time.strftime("%Y-%m-%d", time.gmtime()) and "pf_bekle" not in b.ag and not b.ag["pf"]["bekle"])
A.brain = real_brain

# kontrolor ayni gunde duzelen coin icin en fazla bir kez
reset()
A.brain = lambda: "cli"
pa.st["cash"] += pa.st["positions"].pop(ETH)["qty"] * b.bid[ETH]
ex.back[ETH] = 2
reviews.clear()
run()
n_r = len(reviews)
other = [s for s in pa.st["positions"] if s not in PF.CORE_SYMS][0]
pa.st["cash"] += pa.st["positions"].pop(other)["qty"] * b.bid[other]  # bugunku turda islenmis coin: yeniden denemede sorulmaz, alinmaz
ex.back.clear()
tick_run(True)
A.brain = real_brain
check("duzelen coin icin bir kontrolor cagrisi, sadece o coin", len(reviews) == n_r + 1 and reviews[-1] == [ETH] and qty(other) is None and qty(ETH), reviews)

# BTC bozuk: ETH islenir, rejim bilinmiyor: ML gunu harcanmaz, yeni ML alimi yok
reset()
btc0, eth0 = qty(BTC), qty(ETH)
week0 = b.ag["pf"]["ml_hafta"]
ml0 = {s: qty(s) for s in pa.st["positions"] if s not in PF.CORE_SYMS}
drop = sorted(ml0)[0]
pa.st["cash"] += pa.st["positions"].pop(drop)["qty"] * b.bid[drop]
b.ag["pf"]["ml_hafta"] = "2000-01"
ex.back[BTC] = 2
ex.crash.add(ETH)
run()
check("BTC bayat: BTC'ye dokunulmaz, ETH satisi gider", qty(BTC) == btc0 and qty(ETH) is None, list(pa.st["positions"]))
check("BTC bayat: ML gunu harcanmadi, yeni ML alimi yok", b.ag["pf"]["ml_hafta"] == "2000-01" and qty(drop) is None and b.ag["pf"]["ml"].get("kept"), b.ag["pf"]["ml_hafta"])
tick_run(True)
check("BTC hala bayat: hafta yine harcanmadi", b.ag["pf"]["ml_hafta"] == "2000-01")
ex.back.clear()
ex.crash.clear()
tick_run(True)
check("BTC duzelince tam tur: ML gunu islenir, secim yeni", b.ag["pf"]["ml_hafta"] == week0 and not b.ag["pf"]["ml"].get("kept") and "pf_bekle" not in b.ag, b.ag["pf"]["ml_hafta"])
check("BTC duzelince tam turda eksik ML alinir", qty(drop) is not None, list(pa.st["positions"]))

# elle basma: tam tur, geri bildirim
reset()
ex.back[ETH] = 2
ex.crash.add(BTC)
n_d = len(b.ag["decisions"])
run()
n_d1 = len(b.ag["decisions"])
run()
check("elle basma: her seferinde karar kaydi, sebep gorunur", len(b.ag["decisions"]) == n_d1 + 1 and n_d1 == n_d + 1 and "dokunulmadı" in b.ag["decisions"][-1]["view"], b.ag["decisions"][-1]["view"][:160])
b.ag["pf_bekle"] = {"d": "2000-01-01", "syms": [ETH]}
s0 = snap()
tick_run(True)
check("eski gunun bekleme kaydi: tam tur gibi calisir", b.ag["pf_bekle"]["d"] == time.strftime("%Y-%m-%d", time.gmtime()), b.ag["pf_bekle"])
ex.back.clear(), ex.crash.clear()

# ---- 2b) dongu kapisi: pf_tick gercekten calistirilir ----
calls = []
real_run = b.run_portfolio
b.run_portfolio = lambda retry=False: calls.append(retry) or True
_time = B.time
B.time = Clock()
today_s = time.strftime("%Y-%m-%d", time.gmtime())
b.ag.update(pf_d="", pf_wait=time.time() + 600)
b.ag.pop("pf_bekle", None)
check("kapi: bekleme suresi dolmadan calismaz", not b.pf_tick() and not calls and b.ag["pf_d"] == "", calls)
b.ag["pf_wait"] = time.time() - 1
check("kapi: sure dolunca calisir, gun isaretlenir", b.pf_tick() and calls == [False] and b.ag["pf_d"] == today_s, calls)
calls.clear()
check("kapi: ayni gun tekrar calismaz", not b.pf_tick() and not calls)
b.ag["pf_d"] = ""
b.ag["pf_bekle"] = {"d": today_s, "syms": [ETH]}
b.pf_tick()
check("kapi: bugunku bekleme varsa retry", calls == [True], calls)
calls.clear()
b.ag["pf_bekle"] = {"d": "2000-01-01", "syms": [ETH]}
b.ag["pf_d"] = "dun"
b.pf_tick()
check("kapi: eski bekleme kaydi retry degil", calls == [False], calls)
b.run_portfolio = lambda retry=False: False
b.ag["pf_d"] = "dun"
check("kapi: is parcacigi mesgulse gun isaretlenmez", not b.pf_tick() and b.ag["pf_d"] == "dun", b.ag["pf_d"])
real_port = C.PORTFOY
C.PORTFOY = False
b.ag["pf_d"], n_c = "", len(calls)
check("kapi: strateji kapaliysa calismaz", not b.pf_tick())
C.PORTFOY = real_port
b.run_portfolio = real_run
B.time = _time
# yaris: is parcacigi dongu isaretlemeden biterse yeniden deneme isareti ezilmemeli
reset()
ex.back[ETH] = 2


def sync_run(retry=False):
    r = real_run(retry)
    b._pf_thread.join(30)
    return r


b.run_portfolio = sync_run
b.ag.update(pf_d="", pf_wait=0)
B.time = Clock()
b.pf_tick()
B.time = _time
b.run_portfolio = real_run
check("yaris: hizli biten is parcacigi sonrasi yeniden deneme isareti duruyor", b.ag["pf_d"] == "" and b.ag["pf_wait"] > time.time() + 300, b.ag["pf_d"])
ex.back.clear()
B.KT.review = real_review
reset()
pa_pos0 = snap()
week_key = b.ag["pf"]["ml_hafta"]

# ---- 2c) reddedilen secim: rejim kapisi taze, yas siniri, yeni alim yok ----
prev0 = [("A01/USDT", 0.9), ("A02/USDT", 0.8), ("A03/USDT", 0.7), ("A04/USDT", 0.6), ("A05/USDT", 0.5)]
mk0 = lambda age=8, reg=True: {"ml": {"picks": list(prev0), "reg": reg, "note": "eski", "t": time.time() - age * 86400}, "ml_n": 50}
exd = FakeEx(n_alt=20)
exd.crash.add(BTC)
st = mk0()
tg, info = PF.targets(exd, st, True)
check("secim reddedildi, BTC SMA100 altinda: rejim kapanir, ML hedefi yok", st["ml"]["reg"] is False and set(tg) <= set(PF.CORE_SYMS) and st["ml"]["picks"] == prev0, tg)
check("rejim kapisi uyarisi", "nakde" in info["uyari"][0], info["uyari"])
st = mk0(reg=False)
exu = FakeEx(n_alt=20)
tg, info = PF.targets(exu, st, True)
check("secim reddedildi, BTC yukarida: rejim taze True, secim korunur, yeni alim yok denir", st["ml"]["reg"] is True and st["ml"]["kept"] and "yeni alım" in info["uyari"][0], info["uyari"])
PF._M.clear()
st = mk0()
exc = FakeEx()
exc.crash.add(BTC)
tg, info = PF.targets(exc, st, True)
check("model yok + BTC dusuk: rejim kapanir", st["ml"]["reg"] is False and set(tg) <= set(PF.CORE_SYMS), tg)
fresh_model()
st = mk0()
exb = FakeEx()
exb.crash.add(BTC)
exb.boom.add("A10/USDT")
tg, _ = PF.targets(exb, st, True)
check("gercek ml_pick hatasi + BTC dusuk: rejim kapanir", st["ml"]["reg"] is False and set(tg) <= set(PF.CORE_SYMS), tg)
st = mk0(age=20)
exu = FakeEx(n_alt=20)
tg, info = PF.targets(exu, st, True)
check("20 gunluk secim: korunur", st["ml"]["picks"] == prev0 and all(s in tg for s, _ in prev0))
st = mk0(age=22)
st["ml_n"] = 50
tg, info = PF.targets(exu, st, True)
check("22 gunluk secim, yenilenemiyor: nakit, uyari", st["ml"]["picks"] == [] and set(tg) <= set(PF.CORE_SYMS) and "21 gündür" in info["uyari"][-1], info["uyari"])
_, info = PF.targets(exu, st, False)
check("yas siniri uyarisi tek sefer", not info["uyari"], info["uyari"])
st = mk0(age=22)
tg, info = PF.targets(ex, st, True)
check("22 gunluk secim, secim basarili: yeni secim, uyari yok", st["ml"]["picks"] and st["ml"]["picks"] != prev0 and not info["uyari"], info["uyari"])
st = mk0(age=22)
PF._M.clear()
tg, info = PF.targets(ex, st, False)
check("model yok ama ML gunu degil: yas siniri yine calisir", st["ml"]["picks"] == [], st["ml"]["picks"])
fresh_model()
st = mk0(age=2)
tg, info = PF.targets(ex, st, False)
check("saglikli hafta ici: secim ve rejim dokunulmaz (normal yol degismez)", st["ml"]["picks"] == prev0 and st["ml"]["reg"] is True and not st["ml"].get("kept") and all(abs(tg[s] - 0.06) < 1e-9 or s in PF.CORE_SYMS for s, _ in prev0), tg)
# bot: stopla satilan secim geri alinmaz, reddedilende satis yine gider
reset()
held_ml = [s for s in pa.st["positions"] if s not in PF.CORE_SYMS]
gone = held_ml[0]
pa.st["cash"] += pa.st["positions"].pop(gone)["qty"] * b.bid[gone]  # felaket stopu satti gibi
keep = {s: qty(s) for s in held_ml[1:]}
b.ag["pf"]["ml_hafta"] = "2000-01"
ex.boom.add("A10/USDT") if "A10/USDT" not in held_ml else ex.boom.add("A11/USDT")
n_a = len(alerts("yeni alım yapılmıyor"))
b.portfolio()
check("reddedilen secim: stopla satilan geri alinmaz", qty(gone) is None and all(qty(s) == q for s, q in keep.items()), list(pa.st["positions"]))
check("reddedilen secim: uyari + ekranda nakit dogru", len(alerts("yeni alım yapılmıyor")) == n_a + 1 and b.ag["pf"]["targets"].get(gone, 0) == 0, b.ag["pf"]["targets"])
check("reddedilen secim: ozet soyler", "yeni alım yok" in b.ag["decisions"][-1]["view"], b.ag["decisions"][-1]["view"][-120:])
ex.crash.add(BTC)
b.ag["pf"]["ml_hafta"] = "2000-01"
b.portfolio()
ex.boom.clear(), ex.crash.clear()
check("reddedilen secim + BTC dusuk: ML satilir (satis engellenmez)", not any(s in pa.st["positions"] for s in held_ml), list(pa.st["positions"]))
check("arayuz sebebi gosterir", "pf.bekle" in open(os.path.join(sys.argv[1], "ui.html"), encoding="utf-8").read())
reset()
pa_pos0 = snap()
week_key = b.ag["pf"]["ml_hafta"]

# ---- 3) evren: kucukse onceki secim korunur ----
prev = [("A01/USDT", 0.9), ("A02/USDT", 0.8), ("A03/USDT", 0.7), ("A04/USDT", 0.6), ("A05/USDT", 0.5)]
mk = lambda: {"ml": {"picks": list(prev), "reg": True, "note": "eski", "t": time.time() - 8 * 86400}, "ml_n": 50}
exs = FakeEx(n_alt=20)  # BTC+ETH+20 = 22 coin; 50'nin %70'inden az
st = mk()
tg, info = PF.targets(exs, st, True)
check("evren 22 (onceki 50): onceki secim, ML agirligi korunur", st["ml"]["picks"] == prev and all(s in tg for s, _ in prev), st["ml"]["note"])
check("evren: kullaniciya uyari, not", info["uyari"] and "Önceki seçim korunuyor" in info["uyari"][0] and st["ml"]["note"].startswith("hata"), info["uyari"])
tg2, info2 = PF.targets(exs, st, True)
check("ertesi gun yeniden deneme: dar evren hala reddedilir", st["ml_n"] == 50 and st["ml"]["picks"] == prev, st.get("ml_n"))
st["ml_n_t"] = time.time() - 7 * 86400
tg2, info2 = PF.targets(exs, st, True)
check("kalici dusus: bir hafta sonra kabul edilir, takilmaz", st["ml_n"] == 22 and st["ml"]["picks"] != prev and "ml_n_t" not in st and not info2["uyari"], st["ml"]["picks"][:2])
st = mk()
del st["ml_n"]
PF.targets(exs, st, True)
check("eski kayit (ml_n yok, secim var): TOP ile karsilastirilir", st["ml"]["picks"] == prev)
st = mk()
exs2 = FakeEx(n_alt=6)
tg, info = PF.targets(exs2, st, True)
check("evren < 10: tasfiye yok (eski kod secimi bosaltiyordu)", st["ml"]["picks"] == prev and st["ml"]["reg"], st["ml"]["note"])
st = {"ml_n": 50, "ml": {"picks": list(prev), "reg": True, "t": 1}}
exs3 = FakeEx(n_alt=38)  # 40 coin, %80: yakin
PF.targets(exs3, st, True)
check("evren yakin (40/50): yeni secim", st["ml"]["picks"] != prev and st["ml_n"] == 40, st["ml"]["note"])
st = {}
PF.targets(exs, st, True)
check("onceki secim yoksa evren karsilastirmasi yok", len(st["ml"]["picks"]) == 5)

# ---- 4) ML hatasi kullaniciya gorunur; coin verisi bayat ----
ex2 = FakeEx()
for s in ex2.syms[2:]:
    ex2.back[s] = 3
st = mk()
tg, info = PF.targets(ex2, st, True)
check("altlar bayat, cekirdek taze: cekirdek calisir, onceki secim korunur", st["ml"]["picks"] == prev and tg["BTC/USDT"] > 0 and info["uyari"], info["uyari"])
ex2b = FakeEx()
ex2b.fetch_tickers = lambda: {s: {"quoteVolume": 1e9} for s in ex2b.syms[2:]}
for s in ex2b.syms[2:]:
    ex2b.back[s] = 3
st = mk()
PF.targets(ex2b, st, True)
check("tum coin tablosu bayat: secim korunur", st["ml"]["picks"] == prev and "bayat" in st["ml"]["note"], st["ml"]["note"])
b.ag["pf"]["ml_hafta"] = "2000-01"
b.ag["pf"]["ml"] = {"picks": list(prev), "reg": True, "note": "", "t": time.time() - 8 * 86400}
ex.boom.add("A10/USDT")
n_a = len(alerts("yenilenemedi"))
b.portfolio()
ex.boom.clear()
txt = alerts("yenilenemedi")
check("haftalik ML hatasi say ile gorunur (bir kez)", len(txt) == n_a + 1 and "bozuk cevap" in txt[-1], txt[-1:])
check("hata: onceki secim korundu, hedefte duruyor", b.ag["pf"]["ml"]["picks"] == prev and all(s in b.ag["pf"]["targets"] for s, _ in prev), list(b.ag["pf"]["targets"]))
b.portfolio()
check("ayni hafta tekrar uyari yok", len(alerts("yenilenemedi")) == n_a + 1)
st = {}
ex.boom.add("A10/USDT")
tg, info = PF.targets(ex, st, True)
ex.boom.clear()
check("ilk secim hata verirse: nakit ve uyari", set(tg) == set(PF.CORE_SYMS) and "nakitte" in info["uyari"][0], info["uyari"])

# ---- 5) model yasi ----
fresh_model(t=time.time() - 89 * 86400)
st = {}
tg, info = PF.targets(ex, st, True)
check("89 gunluk model calisir", st["ml"]["durum"] == "calisiyor" and len(tg) > 2 and not info["uyari"], st["ml"]["durum"])
fresh_model(t=time.time() - 91 * 86400)
tg, info = PF.targets(ex, st, False)
check("91 gunluk model: ML nakit, cekirdek durur", st["ml"]["durum"] == "eski" and st["ml"]["rest"] and set(tg) == set(PF.CORE_SYMS), tg)
check("eski model: bir uyari", len(info["uyari"]) == 1 and "90 günden eski" in info["uyari"][0], info["uyari"])
_, info = PF.targets(ex, st, False)
_, info2 = PF.targets(ex, st, True)
check("eski model: ikinci/ucuncu turda uyari yok", not info["uyari"] and not info2["uyari"], (info["uyari"], info2["uyari"]))
fresh_model()
tg, info = PF.targets(ex, st, False)
check("yeni model gelince sepet geri doner", st["ml"]["durum"] == "calisiyor" and not st["ml"]["rest"] and len(tg) > 2)
fresh_model(t=time.time() - 91 * 86400)
_, info = PF.targets(ex, st, False)
check("yeniden eskirse yeniden bir uyari", len(info["uyari"]) == 1)
PF._M.pop("t")
check("zamani olmayan model eski sayilir", PF.ml_state(PF._M) == "eski")
fresh_model(t=time.time() - 91 * 86400)
b.ag["pf"].pop("ml_uyari", None)
n_a = len(alerts("90 günden eski"))
b.portfolio()
b.portfolio()
d = b.ag["decisions"][-1]["view"]
check("bot: eski model bir say, ML satildi, ozet dogru sebep", len(alerts("90 günden eski")) == n_a + 1 and "90 günden eski" in d
      and all(s in PF.CORE_SYMS for s in pa.st["positions"]), d[-160:])

# ---- 6) ic None acik durum: agirlik degismez, gizli gecmez ----
fresh_model(ic=None)
st = {}
tg, info = PF.targets(ex, st, True)
check("ic None: durum saglik_yok, sepet calisir, uyari var", st["ml"]["durum"] == "saglik_yok" and not st["ml"]["rest"] and len(tg) > 2 and "sağlık" in info["uyari"][0], info["uyari"])
_, info = PF.targets(ex, st, False)
check("ic None: tek uyari", not info["uyari"])
PF._M.pop("ic")
check("ic anahtari hic yok: ayni durum", PF.ml_state(PF._M) == "saglik_yok")
fresh_model(ic=float("nan"))
check("ic NaN gizlice gecmez", PF.ml_state(PF._M) == "saglik_yok")
fresh_model(ic=0.01)
tg, info = PF.targets(ex, st, False)
check("ic dusuk: dinlenme, nakit (eski davranis)", st["ml"]["durum"] == "dinlenme" and st["ml"]["rest"] and set(tg) == set(PF.CORE_SYMS))
fresh_model(ic=None)
b.ag["pf"].pop("ml_uyari", None)
b.ag["pf"]["ml"] = {"picks": list(prev), "reg": True, "note": "", "t": time.time() - 8 * 86400}
b.portfolio()
check("bot ozeti saglik_yok gosterir", "sağlık kontrolü ölçülmemiş" in b.ag["decisions"][-1]["view"], b.ag["decisions"][-1]["view"][-180:])
check("arayuz durumu gorur", "durum_not" in b.ag["pf"]["ml"] and "ml.durum_not" in open(os.path.join(sys.argv[1], "ui.html"), encoding="utf-8").read())

# ---- 7) model hic yok: secim korunur ----
PF._M.clear()
st = mk()
tg, info = PF.targets(ex, st, True)
check("model yok: onceki secim korunur, durum yok, uyari", st["ml"]["durum"] == "yok" and st["ml"]["picks"] == prev and all(s in tg for s, _ in prev) and len(info["uyari"]) == 2, info["uyari"])
st = {}
tg, info = PF.targets(ex, st, True)
check("model yok, secim yok: nakit", set(tg) == set(PF.CORE_SYMS) and st["ml"]["picks"] == [])
fresh_model()

# ---- 8) egitim: gercek train sentetik veriyle, atomik yazma ----
PF.train = _real_train
today = int(time.time() // 86400) * DAY
n = 1500
for i in range(15):
    sym = "BTCUSDT" if i == 0 else f"C{i:02d}USDT"
    c = series(sym, n)
    t = np.array([today - (n - j) * DAY for j in range(n)], dtype=float)
    np.savez(os.path.join(PF.DATA, sym + ".npz"), t=t, o=c, h=c, l=c, c=c, qv=np.full(n, 5e7 + i), tbq=np.full(n, 2e7 + i))
says = []
m1 = PF.train(says.append)
check("train: dosya yazildi, gecici dosya yok, ic olculdu", os.path.exists(PF.MODEL_FILE) and not os.path.exists(PF.MODEL_FILE + ".tmp")
      and m1["ic"] == m1["ic"] and m1["v"] == PF.MODEL_V, (m1["ic"], m1["end"]))
disk = joblib.load(PF.MODEL_FILE)
check("train: diskteki model ayni", disk["t"] == m1["t"] and disk["model"].n_iter_ == 100)
time.sleep(0.01)
real_dump = joblib.dump


def broken(obj, fn):
    with open(fn, "wb") as fh:
        fh.write(b"yarim")
    raise OSError("disk doldu")


PF.joblib.dump = broken
try:
    PF.train(says.append)
    raised = False
except OSError:
    raised = True
PF.joblib.dump = real_dump
disk2 = joblib.load(PF.MODEL_FILE)
check("yazma yarim kalirsa eski model saglam, bellek degismedi", raised and disk2["t"] == m1["t"] and PF._M["t"] == m1["t"], raised)
PF._M.clear()
check("load_model gecici dosyayi okumaz", PF.load_model()["t"] == m1["t"])

# ---- 9) egitim verisi guncellenemediyse taze model yok ----
PF._ls, _ls0 = (lambda sess, prefix, keys=False: []), PF._ls
try:
    _real_sync(lambda t: None)
    ok1 = True
except ValueError:
    ok1 = False
z = dict(np.load(os.path.join(PF.DATA, "BTCUSDT.npz")))
np.savez(os.path.join(PF.DATA, "BTCUSDT.npz"), **{k: (v - 20 * DAY if k == "t" else v) for k, v in z.items()})
try:
    _real_sync(lambda t: None)
    raised = False
except ValueError as e:
    raised = "güncellenemedi" in str(e)
PF._ls = _ls0
check("sync: taze veri gecer, 20 gun eski veri hata", ok1 and raised, (ok1, raised))

# ---- 10) basarisiz egitim gunde en fazla bir ----
calls, says = [], []
PF.maybe_train = _real_maybe


def boom(say):
    calls.append("veri")
    raise ValueError("indirme bozuk")


def wait():
    for x in threading.enumerate():
        if x.name == "portfoy-egitim":
            x.join(10)


PF.sync_data, PF.train = boom, lambda say: calls.append("egitim")
fresh_model(v=PF.MODEL_V - 1)
if os.path.exists(PF.TRAIN_MARK):
    os.remove(PF.TRAIN_MARK)
for _ in range(5):
    PF.maybe_train(says.append)
    wait()
check("MODEL_V farkli, egitim basarisiz: 5 cagrida bir deneme, bir uyari", calls == ["veri"] and len(says) == 1 and "başarısız" in says[0], (calls, says))
check("basarisizlik isareti diskte (yeniden baslatmaya dayanir)", time.time() - PF.last_try() < 60)
fresh_model(t=time.time() - 200 * 86400)
PF.maybe_train(says.append)
wait()
check("eski model de ayni gun tekrar denemez", calls == ["veri"], calls)
open(PF.TRAIN_MARK, "w").write(str(time.time() - 25 * 3600))
PF.maybe_train(says.append)
wait()
check("24 saat sonra bir deneme daha", calls == ["veri", "veri"], calls)
open(PF.TRAIN_MARK, "w").write("bozuk")
PF.sync_data = lambda say: calls.append("veri")
calls.clear()
PF.maybe_train(says.append)
wait()
check("basari isareti siler; yeni MODEL_V hemen egitilebilir", calls == ["veri", "egitim"] and not os.path.exists(PF.TRAIN_MARK), calls)
fresh_model()
calls.clear()
PF.maybe_train(says.append)
wait()
check("taze model egitilmez", not calls)
PF.maybe_train = lambda say: None

check("hicbir emir borsaya gitmedi", not ex.orders and not exs.orders, ex.orders)
shutil.rmtree(TMP, ignore_errors=True)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
