"""Portfoy Stratejisi (2017-2026 arastirmasi, delist edilen coinler dahil, maliyetli, egitim 2018-21 / test 2022-26):
1) Cekirdek (%70): BTC ve ETH yari yariya. Her biri icin SMA20/50/100/200 ustunde kalma orani x oynaklik hedefi
   (%40 yillik / 30 gunluk gerceklesen oynaklik, en fazla 1). Gunluk kapanista (00:05 UTC) hesaplanir.
2) ML sepeti (%30): haftalik (Pazartesi) en likit 50 coin icinde gelecek 7 gun getiri SIRASI tahmini (HistGradientBoosting,
   ozellikler evren icinde yuzdelik sira). En iyi 5 coin esit agirlik; BTC 100 gunluk ortalamanin altindaysa sepet nakit.
Test 2022-2026: yillik %25, en derin dusus %-24 (BTC al-tut %11 / %-67). Saatlik islem yok; kararlar gun kapanisinda."""
import io
import logging
import os
import re
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor

import joblib
import numpy as np
import pandas as pd
import requests

import config as C

log = logging.getLogger("portfoy")
KIND = "portfoy"
CORE_SYMS = ("BTC/USDT", "ETH/USDT")
SMAS = (20, 50, 100, 200)
VT = 0.4            # cekirdek hedef yillik oynaklik
K, TOP = 5, 50      # ML sepeti: en iyi K coin, en likit TOP coin icinde
BAND, BAND_ML = 0.03, 0.01   # agirlik farki bu kadardan kucukse islem yok (gunluk / haftalik yeniden dengeleme)
STOP_FRAC = 0.65    # felaket stopu: ortalama girisin %35 alti (strateji kendi gunluk kuraliyla cikar)
RETRAIN_DAYS = 30  # her ay yeni veriyle yeniden egitim + saglik kontrolu
MIN_IC = 0.02      # son 6 ay tahmin isabeti (sira korelasyonu) bunun altindaysa ML sepeti dinlenmede (nakit)
MODEL_FILE = os.path.join(C.APP_DIR, "portfoy_model.joblib")
DATA = os.path.join(C.APP_DIR, "gunluk_veri")
BUNDLED = os.path.join(C.BASE_DIR, "portfoy_model.joblib")
BACKTEST = {"donem": "2022-01 → 2026-10 (test, eğitimde görülmedi)", "strateji": {"cagr": 25.2, "dd": -24.1, "sharpe": 0.97},
            "btc": {"cagr": 11.4, "dd": -66.9, "sharpe": 0.47}, "cekirdek": {"cagr": 16.7, "dd": -22.1},
            "ml": {"cagr": 43.4, "dd": -41.1},
            "yillar": {2022: (-23, -65), 2023: (55, 154), 2024: (77, 112), 2025: (17, -7), 2026: (14, -10)}}
STABLE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT", "PAXUSDT", "USDSUSDT", "USDSBUSDT", "DAIUSDT", "FDUSDUSDT",
          "EURUSDT", "GBPUSDT", "AUDUSDT", "EURIUSDT", "AEURUSDT", "USTUSDT", "SUSDUSDT", "USD1USDT", "XUSDUSDT",
          "BFUSDUSDT", "RLUSDUSDT", "USDEUSDT", "TRYUSDT", "BRLUSDT", "UAHUSDT"}
LEVER = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STOCK = ("BNCB", "CRCLB", "INTCB", "KORUB", "MSTRB", "MUB", "NBISB", "NVDAB", "SKHYB", "SNDKB", "SOXLB", "SPCXB")
STATE = {"training": False, "msg": "", "model_t": 0}
_M = {}


def ok_sym(s):
    return s.endswith("USDT") and not s.endswith(LEVER) and s not in STABLE and not s.startswith(STOCK)


# ---------------- veri ----------------
def klines(ex, sym, limit=260):
    """Kapanmis gunluk mumlar (ham Binance: alici agresif hacmi dahil). sym: 'BTC/USDT'."""
    k = ex.publicGetKlines({"symbol": sym.replace("/", ""), "interval": "1d", "limit": limit})
    a = np.array([[float(x[0]), *(float(x[i]) for i in (1, 2, 3, 4, 7, 10))] for x in k])
    if len(a) and a[-1, 0] + 86_400_000 > time.time() * 1000:  # olusan gun mumu kullanilmaz
        a = a[:-1]
    return pd.DataFrame(a[:, 1:], columns=["o", "h", "l", "c", "qv", "tbq"], index=pd.to_datetime(a[:, 0], unit="ms"))


def first_day(ex, sym, cache):
    """Coinin Binance'teki ilk gunluk mumu (yas ozelligi); kalici onbellek."""
    if sym not in cache:
        k = ex.publicGetKlines({"symbol": sym.replace("/", ""), "interval": "1d", "startTime": 0, "limit": 1})
        cache[sym] = int(k[0][0]) if k else int(time.time() * 1000)
    return cache[sym]


# ---------------- 1) cekirdek ----------------
def core_weight(c):
    """c: gunluk kapanislar (Series). Donus: (agirlik 0..1, trend orani, oynaklik)."""
    on = float(np.mean([c.iloc[-1] > c.rolling(n).mean().iloc[-1] for n in SMAS]))
    vol = float(c.pct_change().iloc[-30:].std() * np.sqrt(365))
    return on * min(1.0, VT / vol) if vol > 0 else 0.0, on, vol


# ---------------- 2) ML sepeti: ozellikler (egitim ve canli ayni tanim) ----------------
FEATS = ["r1", "r3", "r7", "r14", "r30", "r60", "r90", "vol30", "vol7", "dhi90", "sma50", "akis7", "vsp7", "adv", "age",
         "btc_r30", "btc_sma200"]


def raw_feats(c, qv, tbq, age):
    """Gun x coin tablolari -> ozellik tablolari (sadece gecmis veri)."""
    ret = c.pct_change(fill_method=None)
    F = {f"r{L}": c / c.shift(L) - 1 for L in (1, 3, 7, 14, 30, 60, 90)}
    F["vol30"] = ret.rolling(30, min_periods=20).std()
    F["vol7"] = ret.rolling(7, min_periods=5).std()
    F["dhi90"] = c / c.rolling(90, min_periods=30).max() - 1
    F["sma50"] = c / c.rolling(50, min_periods=30).mean() - 1
    tr = (tbq / qv).replace([np.inf, -np.inf], np.nan)
    F["akis7"] = tr.rolling(7, min_periods=5).mean() - tr.rolling(60, min_periods=30).mean()
    F["vsp7"] = np.log(qv.rolling(7, min_periods=5).mean() / qv.rolling(60, min_periods=30).mean())
    adv = qv.rolling(30, min_periods=20).mean()
    F["adv"] = np.log(adv)
    F["age"] = np.log(age.clip(lower=1))
    U = (adv.where((age >= 60) & c.notna() & (adv >= 1e6)).rank(axis=1, ascending=False) <= TOP)
    return F, U


def ranked(F, U, btc):
    Fr = {k: v.where(U).rank(axis=1, pct=True) for k, v in F.items()}
    Fr["btc_r30"] = pd.DataFrame(np.repeat((btc / btc.shift(30) - 1).values[:, None], U.shape[1], 1), U.index, U.columns)
    Fr["btc_sma200"] = pd.DataFrame(np.repeat((btc / btc.rolling(200).mean() - 1).values[:, None], U.shape[1], 1),
                                    U.index, U.columns)
    return Fr


def load_model():
    for f in (MODEL_FILE, BUNDLED):
        if os.path.exists(f):
            try:
                m = joblib.load(f)
                _M.update(m)
                STATE["model_t"] = m.get("t", 0)
                return m
            except Exception as e:
                log.warning("portfoy modeli okunamadi %s: %s", f, e)
    return None


def ml_pick(ex, cache, say=lambda t: None):
    """Haftalik ML sepeti: [(sym, skor)] en iyi K + rejim. Model yoksa bos."""
    m = _M or load_model()
    if not m:
        return [], False, "ML modeli yok (ilk eğitim bekleniyor)"
    tk = ex.fetch_tickers()
    cand = sorted([(s, t.get("quoteVolume") or 0) for s, t in tk.items()
                   if s.endswith("/USDT") and ok_sym(s.replace("/", "")) and ":" not in s], key=lambda x: -x[1])[:120]
    syms = [s for s, _ in cand]
    with ThreadPoolExecutor(6) as pool:
        frames = dict(zip(syms, pool.map(lambda s: klines(ex, s, 160), syms)))
    frames = {s: d for s, d in frames.items() if len(d) >= 30}
    idx = sorted(set().union(*[d.index for d in frames.values()]))
    tab = lambda k: pd.DataFrame({s: d[k] for s, d in frames.items()}).reindex(idx)
    c, qv, tbq = tab("c"), tab("qv"), tab("tbq")
    day0 = pd.Series({s: first_day(ex, s, cache) for s in c.columns})
    age = pd.DataFrame({s: (c.index - pd.to_datetime(day0[s], unit="ms")).days + 1 for s in c.columns}, index=c.index)
    F, U = raw_feats(c, qv, tbq, age)
    btc = klines(ex, "BTC/USDT", 260)["c"]
    Fr = ranked(F, U, btc.reindex(c.index))
    last = c.index[-1]
    u = U.loc[last]
    cols = u.index[u.values]
    if len(cols) < 10:
        return [], False, "evren çok küçük"
    X = pd.DataFrame({k: Fr[k].loc[last, cols] for k in m["feats"]})
    sc = pd.Series(m["model"].predict(X[m["feats"]]), index=cols).sort_values(ascending=False)
    reg = bool(btc.iloc[-1] > btc.rolling(100).mean().iloc[-1])
    return [(s, round(float(v), 4)) for s, v in sc.head(K).items()], reg, f"evren {len(cols)} coin, model {time.strftime('%Y-%m-%d', time.gmtime(m.get('t', 0)))}"


# ---------------- hedef agirliklar ----------------
def targets(ex, st, ml_day, core_frac=None):
    """st: kalici durum (ML secimi, yas onbellegi). Donus: ({sym: agirlik}, bilgi)."""
    cf = C.PORTFOY_CORE if core_frac is None else core_frac
    w, info = {}, {"core": {}, "t": time.time()}
    for s in CORE_SYMS:
        cw, on, vol = core_weight(klines(ex, s, 260)["c"])
        w[s] = cf * 0.5 * cw
        info["core"][s] = {"w": round(w[s], 4), "trend": on, "vol": round(vol, 3)}
    if ml_day or "ml" not in st:
        try:
            picks, reg, note = ml_pick(ex, st.setdefault("age", {}))
            st["ml"] = {"picks": picks, "reg": reg, "note": note, "t": time.time()}
            if picks:  # karne: secim anindaki fiyatlar (7 gun sonra BTC ile karsilastirilir)
                px = {s: float(klines(ex, s, 2)["c"].iloc[-1]) for s, _ in picks}
                px["BTC/USDT"] = px.get("BTC/USDT") or float(klines(ex, "BTC/USDT", 2)["c"].iloc[-1])
                st["ml_hist"] = (st.get("ml_hist", []) + [{"t": time.time(), "px": px, "picks": [s for s, _ in picks]}])[-60:]
        except Exception as e:  # ML hatasi cekirdegi durdurmasin; onceki secim korunur
            log.warning("ML sepeti: %s", e)
            st.setdefault("ml", {"picks": [], "reg": False, "note": f"hata: {e}", "t": time.time()})
    ml = st["ml"]
    ic = (_M or {}).get("ic")
    ml["rest"] = ic is not None and ic < MIN_IC  # saglik kontrolu: tahmin gucu kaybolduysa sepet nakitte
    if ml["reg"] and not ml["rest"]:
        for s, _ in ml["picks"]:
            w[s] = w.get(s, 0.0) + (1 - cf) / K
    info["ml"] = ml
    return {s: round(v, 4) for s, v in w.items()}, info


# ---------------- egitim (90 gunde bir, arka planda) ----------------
U3 = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
F3 = "https://data.binance.vision/"


def _ls(sess, prefix, keys=False):
    out, marker = [], ""
    while True:
        p = {"prefix": prefix, "marker": marker}
        if not keys:
            p["delimiter"] = "/"
        r = sess.get(U3, params=p, timeout=30).text
        ks = [k for k in re.findall(r"<Key>([^<]+)</Key>" if keys else r"<Prefix>([^<]+)</Prefix>", r) if k != prefix]
        out += ks
        if "<IsTruncated>true" not in r or not ks:
            return out
        marker = ks[-1]


def _sync_one(sess, sym):
    """Delist edilenler dahil tek paritenin gunluk mumlari: eksik aylik zipler + son gunler (REST)."""
    fn = os.path.join(DATA, sym + ".npz")
    old = dict(np.load(fn)) if os.path.exists(fn) else None
    have = set(pd.to_datetime(old["t"], unit="ms").strftime("%Y-%m")) if old is not None else set()
    rows = []
    for k in _ls(sess, f"data/spot/monthly/klines/{sym}/1d/", True):
        if not k.endswith(".zip") or k[-11:-4] in have:
            continue
        z = zipfile.ZipFile(io.BytesIO(sess.get(F3 + k, timeout=30).content))
        for line in z.read(z.namelist()[0]).decode().splitlines():
            x = line.split(",")
            if x[0].isdigit():
                rows.append([int(x[0]), *(float(x[i]) for i in (1, 2, 3, 4, 7, 10))])
    a = np.array(rows, dtype=float).reshape(-1, 7)
    a[:, 0] = np.where(a[:, 0] > 1e14, a[:, 0] // 1000, a[:, 0])
    if old is not None:
        a = np.vstack([np.column_stack([old[k] for k in ("t", "o", "h", "l", "c", "qv", "tbq")]), a])
    if len(a):
        last = int(a[:, 0].max())
        k = sess.get("https://api.binance.com/api/v3/klines", params={"symbol": sym, "interval": "1d",
                                                                      "startTime": last + 1, "limit": 1000}, timeout=20).json()
        if isinstance(k, list) and len(k) > 1:
            a = np.vstack([a, [[x[0], *(float(x[i]) for i in (1, 2, 3, 4, 7, 10))] for x in k[:-1]]])
        a = np.array(sorted({r[0]: r for r in a.tolist()}.values()))
        np.savez(fn, **{k: a[:, i] for i, k in enumerate(("t", "o", "h", "l", "c", "qv", "tbq"))})


def sync_data(say):
    os.makedirs(DATA, exist_ok=True)
    sess = requests.Session()
    syms = [p.split("/")[-2] for p in _ls(sess, "data/spot/monthly/klines/")]
    syms = [s for s in syms if ok_sym(s)]
    say(f"Portföy ML: {len(syms)} paritenin (delist edilenler dahil) günlük verisi güncelleniyor...")

    def safe(s):
        for i in range(3):
            try:
                return _sync_one(sess, s)
            except Exception:
                time.sleep(2 + 3 * i)
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(safe, syms))


def train(say=lambda t: None):
    """Tum gecmisle (haftalik ornekler, son 8 gun haric) siralama modeli. Arastirmadaki model ile ayni."""
    from sklearn.ensemble import HistGradientBoostingRegressor
    fr = {}
    for f in os.listdir(DATA):
        z = np.load(os.path.join(DATA, f))
        if len(z["t"]) >= 30:
            fr[f[:-4]] = pd.DataFrame({k: z[k] for k in ("c", "qv", "tbq")}, index=pd.to_datetime(z["t"], unit="ms"))
    idx = pd.date_range("2017-08-17", max(d.index[-1] for d in fr.values()), freq="D")
    tab = lambda k: pd.DataFrame({s: d[k] for s, d in fr.items()}).reindex(idx)
    c, qv, tbq = tab("c"), tab("qv"), tab("tbq")
    age = c.notna().cumsum()
    F, U = raw_feats(c, qv, tbq, age)
    Fr = ranked(F, U, c["BTCUSDT"])
    y = (c.shift(-7) / c - 1).where(U).rank(axis=1, pct=True)
    days = c.index[(c.index.dayofweek == 0) & (c.index >= "2018-03-01") & (c.index < c.index[-1] - pd.Timedelta(days=8))]
    rows = []
    for d in days:
        cols = U.columns[U.loc[d].values]
        if len(cols) < 10:
            continue
        m = pd.DataFrame({k: Fr[k].loc[d, cols] for k in FEATS})
        m["y"] = y.loc[d, cols]
        m["d"] = d
        rows.append(m)
    Dd = pd.concat(rows).dropna(subset=["y"])
    mk = lambda: HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                               l2_regularization=1.0, random_state=0)
    # saglik kontrolu: son 26 haftayi gormeden egit, o haftalarda tahmin isabetini (sira korelasyonu) olc
    cut = Dd.d.max() - pd.Timedelta(weeks=26)
    old, new = Dd[Dd.d < cut - pd.Timedelta(days=8)], Dd[Dd.d >= cut].copy()
    new["p"] = mk().fit(old[FEATS], old.y).predict(new[FEATS])
    ic = float(new.groupby("d").apply(lambda g: g.p.corr(g.y, method="spearman")).mean())
    model = mk().fit(Dd[FEATS], Dd.y)
    out = {"model": model, "feats": FEATS, "t": time.time(), "n": len(Dd), "end": str(c.index[-1].date()),
           "ic": round(ic, 3)}
    joblib.dump(out, MODEL_FILE)
    _M.clear()
    _M.update(out)
    STATE["model_t"] = out["t"]
    say(f"Portföy ML modeli yeniden eğitildi ({out['end']} verisiyle). Sağlık kontrolü: son 6 ayda tahmin isabeti "
        f"{ic:+.3f} → " + ("sepet çalışmaya devam ediyor." if ic >= MIN_IC else
                            "tahmin gücü kayboldu, ML sepeti dinlenmeye alındı (o pay nakitte bekler)."))
    return out


def maybe_train(say):
    """Model yoksa veya 90 gunden eskiyse arka planda veri guncelle + egit (ilk sefer ~25 dk)."""
    m = _M or load_model()
    if STATE["training"] or (m and time.time() - m.get("t", 0) < RETRAIN_DAYS * 86400):
        return

    def run():
        STATE["training"] = True
        try:
            sync_data(say)
            train(say)
        except Exception as e:
            log.exception("portfoy egitimi")
            say(f"Portföy ML eğitimi başarısız (eski model kullanılıyor): {e}")
        finally:
            STATE["training"] = False
    threading.Thread(target=run, name="portfoy-egitim", daemon=True).start()


def ml_card(ex, st):
    """ML sepeti karnesi: her haftalik secim 7 gun sonra BTC ile karsilastirilir (gercek ileri test)."""
    for h in st.get("ml_hist", []):
        if "ret" not in h and time.time() - h["t"] >= 7 * 86400:
            try:
                p = lambda s: ex.fetch_ohlcv(s, "1d", since=int((h["t"] + 7 * 86400) * 1000), limit=1)[0][4]
                rets = [p(s) / h["px"][s] - 1 for s in h["picks"]]
                h["ret"], h["btc"] = round(sum(rets) / len(rets), 4), round(p("BTC/USDT") / h["px"]["BTC/USDT"] - 1, 4)
            except Exception:
                continue
    done = [h for h in st.get("ml_hist", []) if "ret" in h]
    return {"n": len(done), "gecti": sum(h["ret"] > h["btc"] for h in done),
            "fark_pct": round(sum(h["ret"] - h["btc"] for h in done) / len(done) * 100, 2) if done else None,
            "ic": (_M or {}).get("ic")}


def track(trades, curve, btc_now):
    """Sanal karne: strateji pozisyonlarinin kapanan islemleri + kasa egrisinin BTC'ye gore seyri."""
    tr = [t for t in trades if t.get("kind") == KIND]
    pnl = sum(t["pnl"] for t in tr)
    out = {"n": len(tr), "pnl": round(pnl, 2)}
    if curve:
        t0 = next((c for c in curve if c[2]), None)
        if t0 and btc_now:
            out["btc_pct"] = round((btc_now / t0[2] - 1) * 100, 2)
            out["eq_pct"] = round((curve[-1][1] / t0[1] - 1) * 100, 2)
    return out
