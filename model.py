"""Yerel ML karar katmani: egitim, walk-forward testi, cikis politikasi secimi, rejim bazli risk, backtest egrisi."""
import os
import time
from concurrent.futures import ThreadPoolExecutor

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier

import config as C
import data as D

FEATS = ["r1", "r3", "r6", "r12", "r24", "r72", "atr_pct", "vol24", "vol_ratio", "ema20", "ema50", "ema200",
         "ema50_slope", "rsi", "range_pos", "vol_z", "body", "low_wick", "sweep", "hour",
         "flow1", "flow6", "flow24", "trades_z", "vol_accel", "squeeze", "atr_ratio",
         "funding", "funding24", "premium", "premium_z", "oi1", "oi6", "oi24", "oi_z", "lsr", "lsr24",
         "btc_r1", "btc_r6", "btc_r24", "btc_ema50", "btc_ema200", "btc_flow24", "btc_funding", "breadth", "mkt_r24"]
CORE = ["r72", "atr_pct", "ema200", "vol_ratio"]  # bunlar bos ise satir kullanilmaz (isinma donemi)
BAR = pd.Timedelta(milliseconds=D.TF_MS)
POLICIES = {"akilli": ("ret_s", "held_s", "clean_s"), "sabit": ("ret", "held", "clean")}
REGIME_TR = {"trend": "Yükseliş trendi", "yatay": "Yatay", "dusus": "Sert düşüş"}


def ev(p, atr_pct):
    """Komisyon + kayma dahil yaklasik beklenen net getiri (siralama ve esik icin)."""
    win = C.TP_ATR * atr_pct - 2 * C.FEE
    loss = C.SL_ATR * atr_pct + 2 * C.FEE + C.SLIPPAGE
    return p * win - (1 - p) * loss


def regime(btc_ema50, btc_r24, breadth):
    """Piyasa rejimi: BTC trendi + genislik (coinlerin kacta kaci 50 EMA ustunde)."""
    crash = (np.asarray(btc_r24) < -0.04) | (np.asarray(breadth) < 0.2)
    trend = (np.asarray(btc_ema50) > 0) & (np.asarray(breadth) > 0.55)
    return np.where(crash, "dusus", np.where(trend, "trend", "yatay"))


def add_breadth(f):
    """Ayni saatteki tum coinlerden piyasa genisligi ve ortalama 24s getiri (kesitsel ozellik)."""
    g = f.groupby(level=0)
    f["breadth"] = g["ema50"].transform(lambda x: (x > 0).mean())
    f["mkt_r24"] = g["r24"].transform("mean")
    return f


def make_model():
    return HistGradientBoostingClassifier(max_iter=300, learning_rate=0.04, max_leaf_nodes=15,
                                          min_samples_leaf=400, l2_regularization=1.0, random_state=0)


def load_all(ex, symbols, say=lambda m: None):
    """Paralel, onbellekli veri yukleme. Hata veren veya kisa gecmisli sembol atlanir."""
    out, done = {}, 0

    def one(s):
        try:
            return s, D.load(ex, s)
        except Exception:
            return s, None
    with ThreadPoolExecutor(4) as pool:
        for s, df in pool.map(one, symbols):
            done += 1
            say(f"Veri {done}/{len(symbols)}: {s}")
            if df is not None and len(df) >= C.MIN_HISTORY_DAYS * 24:
                out[s] = df
    return out


def build_dataset(ex, say, symbols):
    dfs = load_all(ex, symbols, say)
    parts = []
    for s, df in dfs.items():
        f = D.features(df, dfs[C.BTC]).astype("float32")
        for k, v in D.labels(df).items():
            f[k] = v
        f["sym"] = s
        parts.append(f)
    d = add_breadth(pd.concat(parts))
    d = d.dropna(subset=CORE + ["y"]).sort_index()
    d["regime"] = regime(d.btc_ema50, d.btc_r24, d.breadth)
    return d


def simulate(d, p, thr, policy="akilli"):
    """Sembol basina cakismayan islemler. Donus: islem tablosu (zaman, sembol, net getiri, rejim...)."""
    rc, hc, cc = POLICIES[policy]
    d = d.assign(ev=ev(p, d.atr_pct.values))
    rows = []
    for s, g in d[d.ev > thr].groupby("sym"):
        free_at = None
        for t, r, held, clean, a, reg in zip(g.index, g[rc], g[hc], g[cc], g.atr_pct, g.regime):
            if free_at is not None and t < free_at:
                continue
            rows.append((t, s, r - 2 * C.FEE - (0 if clean else C.SLIPPAGE), held, a, reg))
            free_at = t + BAR * (held + 1)
    return pd.DataFrame(rows, columns=["t", "sym", "net", "held", "atr_pct", "regime"]).sort_values("t")


def summary(r):
    r = np.asarray(r)
    if not len(r):
        return "islem yok"
    pf = r[r > 0].sum() / max(-r[r < 0].sum(), 1e-9)
    return (f"{len(r)} islem | kazanma %{(r > 0).mean() * 100:.0f} | ort %{r.mean() * 100:+.2f} | "
            f"toplam %{r.sum() * 100:+.1f} | profit factor {pf:.2f}")


def regime_mult(tr):
    """Rejim basina risk carpani: o rejimde gecmis testte kazandiran ise >0, kaybettiren ise 0 (islem yok)."""
    base = tr.net.mean()
    out = {}
    for reg in REGIME_TR:
        x = tr[tr.regime == reg].net
        if len(x) < 30:
            out[reg] = 0.5 if len(x) and x.mean() > 0 else 0.0
        else:
            out[reg] = float(np.clip(x.mean() / base, 0, 1.5)) if base > 0 and x.mean() > 0 else 0.0
    return out


def equity_curve(tr, mult):
    """Gecmis test kasa egrisi: MAX_POSITIONS ve risk boyutlandirmasi ile portfoy simulasyonu."""
    eq, active, pts, peak, mdd = 1.0, [], [], 1.0, 0.0
    for t, net, held, a, reg in zip(tr.t, tr.net, tr.held, tr.atr_pct, tr.regime):
        active = [x for x in active if x > t]
        m = mult.get(reg, 1.0)
        if len(active) >= C.MAX_POSITIONS or m <= 0:
            continue
        size = min(C.RISK_PCT / 100 / (C.SL_ATR * a), C.MAX_POS_PCT / 100) * m
        eq *= 1 + size * net
        active.append(t + BAR * held)
        peak = max(peak, eq)
        mdd = max(mdd, 1 - eq / peak)
        pts.append([int(t.timestamp()), round((eq - 1) * 100, 2)])
    step = max(1, len(pts) // 600)
    return {"points": pts[::step] + pts[-1:], "return_pct": round((eq - 1) * 100, 1), "max_dd_pct": round(mdd * 100, 1),
            "trades": len(pts)}


def review(d):
    """Oz-degerlendirme: canlida yazilan tahminler vs gerceklesen sonuclar.
    Donus: (tahmin sayisi, ort. tahmin edilen TP olasiligi, gerceklesen TP orani) veya None."""
    if not os.path.exists(C.JOURNAL_FILE):
        return None
    j = pd.read_csv(C.JOURNAL_FILE, parse_dates=["bar"])
    real = d.reset_index(names="bar")[["bar", "sym", "y"]]
    m = j.merge(real, on=["bar", "sym"], how="inner")
    if len(m) < 50:
        return None
    return len(m), m.p.mean(), m.y.mean()


def train(ex, say, d=None, symbols=None):
    """Walk-forward: son %60 veri 6 doneme bolunur, her donem sadece gecmisle egitilen modelle tahmin edilir.
    Her cikis politikasi icin EV esigi aranir; ancak donemlerin cogunda tutarli net kar varsa kabul edilir.
    En iyi politika, rejim carpanlari ve backtest egrisi modele kaydedilir; avantaj yoksa bot sadece izler."""
    symbols = symbols or D.universe(ex)
    say(f"Parite evreni: {len(symbols)} coin (24s hacme gore)")
    d = build_dataset(ex, say, symbols) if d is None else d
    X, y = d[FEATS], d.y
    say(f"Egitim: {d.sym.nunique()} coin, {len(d)} ornek, {d.index[0]:%Y-%m-%d} -> {d.index[-1]:%Y-%m-%d}")
    rv = review(d)
    if rv:
        say(f"Oz-degerlendirme: {rv[0]} canli tahmin | beklenen TP %{rv[1] * 100:.0f} | gerceklesen %{rv[2] * 100:.0f}")
    ts = d.index.unique().sort_values()
    edges = [ts[min(int(len(ts) * q), len(ts) - 1)] for q in np.linspace(0.4, 1.0, 7)]
    edges[-1] += BAR
    folds = list(zip(edges[:-1], edges[1:]))
    # maliyet sonrasi hedef: botun akilli cikisiyla komisyon (iki yon) + kayma dusulunce net kar mi?
    yn = (d.ret_s - 2 * C.FEE - C.SLIPPAGE > 0).astype(int)
    p, pn = np.full(len(d), np.nan), np.full(len(d), np.nan)
    for i, (a, b) in enumerate(folds):
        say(f"Gecmis test donemi {i + 1}/6 egitiliyor")
        tr, te = d.index < a - BAR * C.MAX_BARS, (d.index >= a) & (d.index < b)  # bosluk: etiket sizintisi yok
        p[te] = make_model().fit(X[tr], y[tr]).predict_proba(X[te])[:, 1]
        pn[te] = make_model().fit(X[tr], yn[tr]).predict_proba(X[te])[:, 1]
    ok = ~np.isnan(p)
    o, po = d[ok], p[ok]

    best = None  # (toplam, esik, politika, islemler, rapor)
    for pol in POLICIES:
        for thr in np.arange(0, 0.0101, 0.0005):
            tr = simulate(o, po, thr, pol)
            if len(tr) < 50:
                continue
            r = tr.net.values
            fs = [tr[(tr.t >= a) & (tr.t < b)].net.sum() for a, b in folds]
            pos_folds = sum(x > 0.01 for x in fs)  # en az %1 net kar eden donem
            pf = r[r > 0].sum() / max(-r[r < 0].sum(), 1e-9)
            robust = r.sum() - max(fs) > 0  # tek bir sansli doneme dayanmiyor
            if pf >= 1.1 and pos_folds >= 4 and robust and (best is None or r.sum() > best[0]):
                best = (r.sum(), float(thr), pol, tr,
                        f"{pol} cikis, EV esigi %{thr * 100:.2f} | {summary(r)} | karli donem {pos_folds}/6")
    if best:
        _, thr, pol, tr, report = best
    else:
        thr, pol = None, "akilli"
        tr = simulate(o, po, 0.005, pol)
        report = f"Tutarli avantaj yok (orn. esik %0.5: {summary(tr.net)}) -> islem acilmayacak, sadece izleme."
    mult = regime_mult(tr) if len(tr) else {k: 0.0 for k in REGIME_TR}
    curve = equity_curve(tr, mult if best else {k: 1.0 for k in REGIME_TR})
    say(f"Gorulmemis veri testi: {report}")
    say("Rejim risk carpanlari: " + ", ".join(f"{REGIME_TR[k]} x{v:.2f}" for k, v in mult.items()))

    ok_n, ynv, pnv = ~np.isnan(pn), yn.values, pn
    hi = ok_n & (pnv >= 0.55)
    net = {"n": int(ok_n.sum()), "pred": round(float(pnv[ok_n].mean()), 3), "real": round(float(ynv[ok_n].mean()), 3),
           "brier": round(float(((pnv[ok_n] - ynv[ok_n]) ** 2).mean()), 4), "hi_n": int(hi.sum()),
           "hi_real": round(float(ynv[hi].mean()), 3) if hi.any() else None}
    say(f"Maliyet sonrasi net model (gorulmemis veri): ort tahmin %{net['pred'] * 100:.0f}, gerceklesen "
        f"%{net['real'] * 100:.0f}; tahmin >= %55 olanlarda gerceklesen "
        + (f"%{net['hi_real'] * 100:.0f} ({net['hi_n']} ornek)" if net["hi_real"] is not None else "-"))
    say("Canli model tum veriyle egitiliyor")
    final = make_model().fit(X, y)
    bundle = {"model": final, "net_model": make_model().fit(X, yn), "net_report": net, "thr": thr, "policy": pol,
              "regime_mult": mult, "trained": time.time(), "report": report, "backtest": curve,
              "symbols": sorted(d.sym.unique().tolist(), key=lambda s: s != C.BTC)}
    joblib.dump(bundle, C.MODEL_FILE)
    return bundle
