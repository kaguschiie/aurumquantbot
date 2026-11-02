"""Strateji ailelerini tara: egitim 2018-2021 ile parametre sec, test 2022+ ile dogrula. BTC al-tut ile karsilastir."""
import itertools, json, os, sys, time
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel as PN

OUT = os.path.dirname(os.path.abspath(__file__))
STOCK = ("BNCB", "CRCLB", "INTCB", "KORUB", "MSTRB", "MUB", "NBISB", "NVDAB", "SKHYB", "SNDKB", "SOXLB", "SPCXB")
t0 = time.time()
P = PN.load()
for k in P:
    P[k] = P[k][[s for s in P[k].columns if not s.startswith(STOCK)]]
X = PN.prep(P)
c, ret = X["c"], X["ret"]
print("panel", c.shape, f"{time.time() - t0:.0f}s", flush=True)
BTC = "BTCUSDT"
week = pd.Series(c.index.dayofweek == 0, index=c.index)
REB = pd.DataFrame(np.repeat(week.values[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
vol = ret.rolling(30, min_periods=20).std() * np.sqrt(365)
res = []


def run(name, W, rebal=None, **meta):
    eq, tu = PN.backtest(W, X, rebal=rebal)
    r = PN.report(name, eq, tu)
    r.update(meta)
    res.append(r)
    return eq


def regime(n):
    b = c[BTC]
    return (b > b.rolling(n).mean()).fillna(False)


# --- 0) kiyaslar ---
W = pd.DataFrame(0.0, index=c.index, columns=c.columns); W[BTC] = 1.0
btc_eq = run("BH_BTC", W, fam="kiyas")
U10 = PN.universe(X, top=10)
run("BH_TOP10_esit_haftalik", U10.astype(float).div(U10.sum(1), axis=0), REB, fam="kiyas")

# --- 1) BTC trend + vol hedefleme ---
for n, vt in itertools.product((50, 100, 150, 200), (None, 0.4, 0.6)):
    g = regime(n).astype(float)
    if vt:
        g = g * (vt / vol[BTC]).clip(upper=1.0)
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns); W[BTC] = g
    run(f"BTC_SMA{n}_vt{vt}", W, fam="btc_trend", n=n, vt=vt)
for n in (100, 200):  # BTC+ETH yari yari, her biri kendi trendinde
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for s in (BTC, "ETHUSDT"):
        W[s] = 0.5 * (c[s] > c[s].rolling(n).mean()).astype(float)
    run(f"BTCETH_SMA{n}", W, fam="btc_trend", n=n)

# --- 2) cok-coin zaman serisi trendi (ters oynaklik agirlik, BTC rejim filtresi) ---
for top, n, rg, vt in itertools.product((10, 20, 30), (50, 100), (0, 100, 200), (0.5, 0.8)):
    U = PN.universe(X, top=top)
    sig = (c > c.rolling(n).mean()) & U
    if rg:
        sig = sig & regime(rg).values[:, None]
    iv = (1 / vol).where(sig)
    W = iv.div(iv.sum(1), axis=0).fillna(0.0)
    W = W.mul(np.minimum(sig.sum(1) / max(5, top // 3), 1.0), axis=0)  # az coin trenddeyse nakit fazla
    scale = (vt / (W * vol).sum(1).replace(0, np.nan)).clip(upper=1.0).fillna(0.0)  # korelasyon~1 varsayimi (temkinli)
    run(f"TS_top{top}_sma{n}_rg{rg}_vt{vt}", W.mul(scale, axis=0), REB,
        fam="ts_trend", top=top, n=n, rg=rg, vt=vt)

# --- 3) kesitsel momentum (haftalik, BTC rejim filtresi) ---
for top, L, K, rg in itertools.product((30, 50, 100), (14, 30, 60, 90), (3, 5, 10), (0, 100, 200)):
    U = PN.universe(X, top=top)
    mom = (c / c.shift(L) - 1).where(U)
    rk = mom.rank(axis=1, ascending=False)
    sig = (rk <= K) & (mom > 0)
    if rg:
        sig = sig & regime(rg).values[:, None]
    W = sig.astype(float) / K
    run(f"XS_top{top}_L{L}_K{K}_rg{rg}", W, REB, fam="xs_mom", top=top, L=L, K=K, rg=rg)

# --- 4) balina akisi (gunluk): agresif alis orani 7g - 30g (birikim) + hacim patlamasi ---
tr_ = (P["tbq"] / P["qv"]).replace([np.inf, -np.inf], np.nan)
acc = tr_.rolling(7, min_periods=5).mean() - tr_.rolling(60, min_periods=30).mean()
vsp = P["qv"].rolling(7, min_periods=5).mean() / P["qv"].rolling(60, min_periods=30).mean()
for top, K, rg, mode in itertools.product((30, 50, 100), (5, 10), (0, 200), ("akis", "akis+mom", "hacim+mom")):
    U = PN.universe(X, top=top)
    mom30 = c / c.shift(30) - 1
    if mode == "akis":
        sc = acc
    elif mode == "akis+mom":
        sc = acc.rank(axis=1, pct=True) + mom30.rank(axis=1, pct=True)
    else:
        sc = vsp.rank(axis=1, pct=True) + mom30.rank(axis=1, pct=True)
    sc = sc.where(U)
    sig = (sc.rank(axis=1, ascending=False) <= K) & (mom30 > 0)
    if rg:
        sig = sig & regime(rg).values[:, None]
    run(f"WH_{mode}_top{top}_K{K}_rg{rg}", sig.astype(float) / K, REB, fam="balina", top=top, K=K, rg=rg, mode=mode)

# --- 5) gunluk Donchian-20 (botta calisan kol) portfoy olarak ---
for top, N, X_ in ((30, 20, 10), (30, 55, 20), (10, 20, 10)):
    U = PN.universe(X, top=top)
    hi = c.rolling(N).max().shift(1); lo = c.rolling(X_).min().shift(1)
    pos = pd.DataFrame(np.nan, index=c.index, columns=c.columns)
    pos[(c > hi) & U & regime(100).values[:, None]] = 1.0
    pos[c < lo] = 0.0
    pos = pos.ffill().fillna(0.0)
    W = pos.div(pos.sum(1).clip(lower=10), axis=0)
    run(f"DON{N}_{X_}_top{top}", W, fam="donchian", top=top)

df = pd.json_normalize(res)
df.to_csv(os.path.join(OUT, "sonuc.csv"), index=False)
b = df[df.name == "BH_BTC"].iloc[0]
print("BTC al-tut  egitim", res[0]["train"], "\n            test  ", res[0]["test"], "\n yillar", res[0]["years"])
cols = ["name", "train.cagr", "train.mdd", "train.sharpe", "test.cagr", "test.mdd", "test.sharpe", "test.calmar", "turnover_y"]
for fam, g in df.groupby("fam"):
    g = g.sort_values("train.sharpe", ascending=False)
    print(f"\n== {fam} ({len(g)} varyant) egitim Sharpe'a gore ilk 5 + test")
    print(g[cols].head(5).to_string(index=False))
    print(f"   ailenin test Sharpe medyani {g['test.sharpe'].median():.2f}, BTC'yi (test Sharpe {b['test.sharpe']}) gecen oran "
          f"{(g['test.sharpe'] > b['test.sharpe']).mean():.0%}")
print(f"sure {time.time() - t0:.0f}s")
