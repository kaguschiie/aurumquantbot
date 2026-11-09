"""Portfoy stratejisi: hedef agirlik -> al / artir / azalt / cik muhasebesi, korumalar, gercek hedeflerle uctan uca dengeleme.
Gercek Binance fiyatlari (anahtarsiz), emir yok. Kullanim: KQB_APP_DIR=<test> python -I portfoy_test.py <proje>"""
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
import config as C
for f in os.listdir(C.APP_DIR):
    if f.startswith(("state", "durdur", "ders_defteri", "gecmis", "trading_memory")):
        os.remove(os.path.join(C.APP_DIR, f))
import agent as A
import bot as B
import portfoy as PF
import quant as Q

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:300])


A.brain = lambda: "local"
A.cli_ready = lambda: False
C.TELEGRAM_TOKEN = ""
C.PAPER_BALANCE = 10_000.0
b = B.Bot()
b.symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
b.refresh_prices()
pa = b.accounts["PAPER"]
pa.st.update(cash=10_000.0, positions={}, trades=[])
pa.mark()
eq0 = pa.equity()
check("ayarlar: portfoy acik, saatlik otomatik kapali, gercek hesapta kapali", C.PORTFOY and not C.OTO_SAATLIK and not C.PORTFOY_LIVE)

# ---- ML gunu: hafta Persembe 00:00 UTC'de doner, eski kayitta sepet korunur ----
import calendar
hk = lambda d: time.strftime("%G-%V", time.gmtime(calendar.timegm(time.strptime(d, "%Y-%m-%d %H:%M")) - (PF.ML_DOW + 1) * 86400))
check("hafta anahtari Carsamba gecesi donuyor", hk("2026-11-04 23:59") != hk("2026-11-05 00:05") and hk("2026-11-05 00:05") == hk("2026-11-11 23:59") != hk("2026-11-12 00:05"))
_rt, seen_ml = PF.targets, []
PF.targets = lambda ex, st, ml_day, core_frac=None: (seen_ml.append(ml_day) or {}, {"core": {}, "ml": {"picks": [], "reg": False, "note": "", "t": 0}})
b.ag["pf"] = {"ml_week": "2026-45", "ml": {"picks": [], "reg": False}}
b.portfolio()
b.portfolio()
check("eski kayit: dagitimda sepet yenilenmez (Persembe ise ilk turda secim yapilir)", seen_ml == [time.gmtime().tm_wday == 3, False] and "ml_hafta" in b.ag["pf"], seen_ml)
b.ag["pf"]["ml_hafta"] = "2000-01"
b.portfolio()
b.portfolio()
check("yeni hafta: bir kez yenilenir", seen_ml[2:] == [True, False], seen_ml)
b.ag["pf"] = {}
b.portfolio()
check("bos durum: hemen secim yapar", seen_ml[-1] is True, seen_ml)
PF.targets = _rt
m_ = PF.load_model()
check("paketli model yeni yapida", m_ and m_.get("v") == PF.MODEL_V and m_["model"].n_iter_ == 100, m_ and m_.get("v"))
_sd, _tr, calls = PF.sync_data, PF.train, []
PF.sync_data, PF.train = (lambda say: calls.append("veri")), (lambda say: calls.append("egitim"))
import threading as _th
PF._M.update(v=1, t=time.time())
PF.maybe_train(lambda t: None)
[x.join(5) for x in _th.enumerate() if x.name == "portfoy-egitim"]
ok1 = calls == ["veri", "egitim"]
calls.clear()
PF._M.update(v=PF.MODEL_V, t=time.time())
PF.maybe_train(lambda t: None)
check("eski yapidaki taze model hemen yeniden egitilir, yenisi egitilmez", ok1 and not calls, calls)
PF.sync_data, PF.train = _sd, _tr
PF._M.clear()
PF.load_model()

# ---- 1) sahte hedeflerle muhasebe ----
real_targets = PF.targets
PF.targets = lambda ex, st, ml_day, core_frac=None: (
    {"BTC/USDT": 0.30, "ETH/USDT": 0.20, "SOL/USDT": 0.06},
    {"core": {"BTC/USDT": {"w": 0.3, "trend": 1.0, "vol": 0.3}, "ETH/USDT": {"w": 0.2, "trend": 0.75, "vol": 0.4}},
     "ml": {"picks": [["SOL/USDT", 0.6]], "reg": True, "note": "test", "t": time.time()}})
b.portfolio(force_ml=True)
pos = pa.st["positions"]
w = {s: p["qty"] * b.bid[s] / pa.equity() for s, p in pos.items()}
check("ilk dengeleme: 3 pozisyon, agirliklar hedefe yakin", set(pos) == {"BTC/USDT", "ETH/USDT", "SOL/USDT"}
      and abs(w["BTC/USDT"] - 0.30) < 0.01 and abs(w["ETH/USDT"] - 0.20) < 0.01, {s: round(v, 3) for s, v in w.items()})
check("pozisyon turu portfoy, felaket stopu %35 alti", all(p["kind"] == PF.KIND and abs(p["sl"] / p["entry"] - 0.65) < 0.01
                                                         for p in pos.values()))
cost = eq0 - pa.equity()
check("maliyet sadece komisyon+kayma (alis %0.15 + satis komisyonu %0.1)", 0 < cost < 0.56 * 10_000 * 0.0035, round(cost, 2))
d = b.ag["decisions"][-1]
check("karar kaydi: portfoy, ozet + islem sonucu", d["kind"] == "portfoy" and "yükseliş gücü" in d["view"] and d["actions"],
      d["actions"][0]["result"][:200])
n_tr = len(pa.st["trades"])
# ---- 2) saniyelik yonetim dokunmaz; Claude/yerel motor kapatamaz ----
for _ in range(3):
    pa.manage_positions()
check("kalkan/sure/kismi kar uygulanmaz", len(pa.st["positions"]) == 3 and len(pa.st["trades"]) == n_tr)
out = b.apply({"type": "close", "symbol": "BTC/USDT", "reason": "test"})
check("Claude kapatmasi reddedilir", "kendi kuralıyla" in out["result"], out["result"])
check("Claude stop degisikligi reddedilir", "kendi kuralıyla" in (pa.adjust("BTC/USDT", b.bid["BTC/USDT"] * 0.9, 0) or ""))
b.market.setdefault("BTC/USDT", {})["tech"] = {"1h": -0.6, "15m": -0.6}
qd = Q.decide(b)
check("yerel motor tez-bozuldu kapatmasi yapmaz", not any(a["type"] == "close" and a["symbol"] == "BTC/USDT" for a in qd["actions"]))
check("portfoy pozisyonlari pozisyon limitine sayilmaz", pa.blocked() is None, pa.blocked())

# ---- 3) azalt / artir / cik ----
PF.targets = lambda ex, st, ml_day, core_frac=None: ({"BTC/USDT": 0.10, "ETH/USDT": 0.35}, {
    "core": {"BTC/USDT": {"w": 0.1, "trend": 0.25, "vol": 0.3}, "ETH/USDT": {"w": 0.35, "trend": 1.0, "vol": 0.4}},
    "ml": {"picks": [], "reg": False, "note": "test", "t": time.time()}})
q_btc, q_eth = pos["BTC/USDT"]["qty"], pos["ETH/USDT"]["qty"]
b.portfolio(force_ml=True)
w = {s: p["qty"] * b.bid[s] / pa.equity() for s, p in pa.st["positions"].items()}
check("SOL hedef 0 -> tam cikis (PORTFOY_CIKIS)", "SOL/USDT" not in pa.st["positions"]
      and pa.st["trades"][-1]["reason"] == "PORTFOY_CIKIS", pa.st["trades"][-1]["reason"] if pa.st["trades"] else "-")
check("BTC azaltildi, ETH artirildi", pa.st["positions"]["BTC/USDT"]["qty"] < q_btc and pa.st["positions"]["ETH/USDT"]["qty"] > q_eth
      and abs(w["BTC/USDT"] - 0.10) < 0.01 and abs(w["ETH/USDT"] - 0.35) < 0.01, {s: round(v, 3) for s, v in w.items()})
eq1 = pa.equity()
b.portfolio()
check("hedefe yakinken islem yok (bant)", abs(pa.equity() - eq1) < 1e-6 and "değişiklik yok" in b.ag["decisions"][-1]["actions"][0]["result"],
      b.ag["decisions"][-1]["actions"][0]["result"])
# ---- 4) korumalar: acil durdurma varken alis yok, satis var ----
open(C.pause_flag("PAPER"), "w").close()
PF.targets = lambda ex, st, ml_day, core_frac=None: ({"BTC/USDT": 0.0, "ETH/USDT": 0.5}, {
    "core": {}, "ml": {"picks": [], "reg": False, "note": "test", "t": time.time()}})
b.portfolio()
r = b.ag["decisions"][-1]["actions"][0]["result"]
check("duraklatilmis: BTC satildi, ETH alimi yapilmadi", "BTC/USDT" not in pa.st["positions"] and "alışlar yapılmadı" in r, r)
os.remove(C.pause_flag("PAPER"))
# ---- 4b) Claude itirazi hesap basina: alimi erteler / yariya indirir, satisa donusmez ----
PF.targets = lambda ex, st, ml_day, core_frac=None: ({"BTC/USDT": 0.30, "ETH/USDT": 0.20}, {
    "core": {}, "ml": {"picks": [], "reg": False, "note": "test", "t": time.time()}})
b.portfolio(force_ml=True)
wg = lambda s: pa.st["positions"][s]["qty"] * b.bid[s] / pa.equity() if s in pa.st["positions"] else 0.0
q0 = {s: p["qty"] for s, p in pa.st["positions"].items()}
pa.rebalance({"BTC/USDT": 0.40, "ETH/USDT": 0.20}, True, {"BTC/USDT": "ertele"})
check("ertele: alim yapilmaz, pozisyon korunur", pa.st["positions"]["BTC/USDT"]["qty"] == q0["BTC/USDT"], round(wg("BTC/USDT"), 3))
pa.rebalance({"BTC/USDT": 0.10, "ETH/USDT": 0.20}, True, {"BTC/USDT": "ertele"})
check("ertele satisi engellemez (hedef dustuyse satar)", abs(wg("BTC/USDT") - 0.10) < 0.01, round(wg("BTC/USDT"), 3))
pa.rebalance({"BTC/USDT": 0.30, "ETH/USDT": 0.20}, True, {"BTC/USDT": "yari"})
check("yari: farkin yarisi alinir", abs(wg("BTC/USDT") - 0.20) < 0.01, round(wg("BTC/USDT"), 3))
pa.rebalance({"BTC/USDT": 0.003, "ETH/USDT": 0.20}, True)
check("kucuk hedef: itirazsiz tam cikis", "BTC/USDT" not in pa.st["positions"])
pa.rebalance({"BTC/USDT": 0.03, "ETH/USDT": 0.20}, True, {"BTC/USDT": "ertele"})
check("hic pozisyon yokken ertele: alim yok, satis yok", "BTC/USDT" not in pa.st["positions"])
# gercek hesap senaryosu: sanal hesapta coin yok (cur=0), LIVE elinde tutuyor; ertele LIVE'i sattirmamali
pa.rebalance({"BTC/USDT": 0.20, "ETH/USDT": 0.20}, True)
q1 = pa.st["positions"]["BTC/USDT"]["qty"]
pa.rebalance({"BTC/USDT": 0.30, "ETH/USDT": 0.20}, True, {"BTC/USDT": "ertele"})
check("ertele elde tutulan coini sattirmaz (eski hata)", pa.st["positions"]["BTC/USDT"]["qty"] == q1)
seen, real_reb, real_brain, real_rev = {}, pa.rebalance, A.brain, B.KT.review
pa.rebalance = lambda tg, ml_day, objs=None, skip=None: seen.update(tg=dict(tg), objs=objs) or ["sahte"]
A.brain = lambda: "api"
B.KT.review = lambda ask, buys, ctx: ("ozet", {"BTC/USDT": "ertele"}, "Claude itiraz etti", 0.0)
PF.targets = lambda ex, st, ml_day, core_frac=None: ({"BTC/USDT": 0.40, "ETH/USDT": 0.20}, {
    "core": {}, "ml": {"picks": [], "reg": False, "note": "test", "t": time.time()}})
b.portfolio(force_ml=True)
check("portfolio: hedef bozulmadan, itiraz ayri gecer", seen["tg"]["BTC/USDT"] == 0.40 and seen["objs"] == {"BTC/USDT": "ertele"}, seen)
check("ekrandaki hedef sanal hesaba gore itirazli", abs(b.ag["pf"]["targets"]["BTC/USDT"] - 0.20) < 0.01, b.ag["pf"]["targets"])
pa.rebalance, A.brain, B.KT.review = real_reb, real_brain, real_rev
# ---- 4c) 24 saatlik zarar limiti gece yarisi sifirlanmaz ----
now, eqn = time.time(), pa.equity()
pa.st["day"] = {"d": B.today(), "eq": eqn}
sv = pa.st["curve"]
pa.st["curve"] = [[now - 90000, eqn * 1.5, 0], [now - 80000, eqn * 1.04, 0], [now - 900, eqn, 0]]
check("kasa 24 saatte %3+ dustu: dengeleme alimi yok (gun tabani sifir olsa da)", "24 saatte" in (pa.blocked(portfolio=True) or "")
      and abs(pa.day_pct()) < 0.01, pa.blocked(portfolio=True))
check("saatlik kol bu kuraldan etkilenmez", "24 saatte" not in (pa.blocked() or ""))
pa.st["curve"] = [[now - 80000, eqn * 1.02, 0], [now - 900, eqn, 0]]
check("limit altinda alim serbest", pa.blocked(portfolio=True) is None, pa.blocked(portfolio=True))
pa.st["curve"] = []
check("gecmis yoksa engel yok", pa.blocked(portfolio=True) is None)
pa.st["curve"] = sv
# ---- 4d) canli ozellikler: 160 gunluk tabloda 200 gunluk BTC ortalamasi dolu, bos ozellik yakalanir ----
import numpy as np
import pandas as pd
ix = pd.date_range("2025-01-01", periods=260, freq="D")
btc_full = pd.Series(np.linspace(100, 200, 260), index=ix)
U4 = pd.DataFrame(True, index=ix[-160:], columns=["A", "B"])
F4 = {"r1": pd.DataFrame(1.0, index=U4.index, columns=U4.columns)}
Fr4 = PF.ranked(F4, U4, btc_full)
exp = btc_full.iloc[-1] / btc_full.iloc[-200:].mean() - 1
check("btc_sma200 kisa tabloda da dolu ve dogru", abs(Fr4["btc_sma200"].iloc[-1, 0] - exp) < 1e-9, Fr4["btc_sma200"].iloc[-1, 0])
check("btc_r30 tam seriyle ayni", abs(Fr4["btc_r30"].iloc[-1, 0] - (btc_full.iloc[-1] / btc_full.iloc[-31] - 1)) < 1e-9)
check("egitim gibi hizali seride sonuc degismez", abs(PF.ranked(F4, U4, btc_full.reindex(U4.index))["btc_r30"].iloc[-1, 0]
                                                      - Fr4["btc_r30"].iloc[-1, 0]) < 1e-9)
# ---- 5) kapanis muhasebesi: tum pozisyonlar kapaninca kasa = nakit ----
PF.targets = lambda ex, st, ml_day, core_frac=None: ({}, {"core": {}, "ml": {"picks": [], "reg": False, "note": "", "t": 0}})
b.portfolio()
check("hedef bos -> hepsi nakit", not pa.st["positions"] and abs(pa.equity() - pa.st["cash"]) < 1e-6, round(pa.st["cash"], 2))
tot = sum(t["pnl"] for t in pa.st["trades"])
check("islem kayitlari kasa ile tutarli", abs((pa.st["cash"] - 10_000) - tot) < 0.05, (round(pa.st["cash"] - 10_000, 2), round(tot, 2)))

# ---- 6) gercek hedefler (Binance verisi + egitilmis ML modeli) uctan uca ----
PF.targets = real_targets
t0 = time.time()
b.portfolio(force_ml=True)
pf = b.ag["pf"]
check("gercek hedefler: cekirdek BTC/ETH + ML sepeti 5 coin", set(pf["info"]["core"]) == {"BTC/USDT", "ETH/USDT"}
      and len(pf["ml"]["picks"]) == 5, (pf["targets"], f"{time.time() - t0:.0f} sn"))
check("toplam agirlik <= 1 ve sanal hesapta pozisyonlar", sum(pf["targets"].values()) <= 1.0001
      and all(p["kind"] == PF.KIND for p in pa.st["positions"].values()), pa.st["positions"].keys())
seen_X = {}
real_pred = PF._M["model"].predict
PF._M["model"].predict = lambda X: seen_X.update(X=X.copy()) or real_pred(X)
live = {}
picks_new = PF.ml_pick(b.ex, pf.setdefault("age", {}))
check("canli tahmin: hicbir ozellik tamamen bos degil", not [k for k in seen_X["X"] if seen_X["X"][k].isna().all()], seen_X["X"].isna().mean().round(2).to_dict())
old_pred = lambda X: real_pred(X.assign(btc_sma200=np.nan))
sc_new = pd.Series(real_pred(seen_X["X"]), index=seen_X["X"].index).sort_values(ascending=False).head(5)
sc_old = pd.Series(old_pred(seen_X["X"]), index=seen_X["X"].index).sort_values(ascending=False).head(5)
print("BILGI secim yeni:", list(sc_new.index), "| eski (bos ozellik):", list(sc_old.index), "| ortak:", len(set(sc_new.index) & set(sc_old.index)))
PF._M["model"].predict = real_pred
tr = PF.track(pa.st["trades"], pa.st["curve"], b.bid.get(C.BTC))
check("karne hesaplanir", "n" in tr, tr)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
