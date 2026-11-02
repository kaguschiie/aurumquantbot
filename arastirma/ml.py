"""Hibrit ML'in iyilestirilmis hali: saatlik al/alma siniflandirmasi yerine haftalik KESITSEL SIRALAMA (Qlib yaklasimi).
Ozellikler gunluk; hedef: gelecek 7 gun getirisinin evren icindeki sirasi. Yururlukte ileri (walk-forward): her yil basinda
sadece o tarihe kadarki veriyle (7 gun bosluk) yeniden egitim. Portfoy: tahmini en yuksek K coin, BTC rejim filtresi."""
import os, sys, time, json
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel as PN

OUT = os.path.dirname(os.path.abspath(__file__))
t0 = time.time()
P = PN.load()
STOCK = ("BNCB", "CRCLB", "INTCB", "KORUB", "MSTRB", "MUB", "NBISB", "NVDAB", "SKHYB", "SNDKB", "SOXLB", "SPCXB")
for k in P:
    P[k] = P[k][[s for s in P[k].columns if not s.startswith(STOCK)]]
X = PN.prep(P)
c, ret = X["c"], X["ret"]
BTC = "BTCUSDT"
TOP = 50
U = PN.universe(X, top=TOP)
F = {}
for L in (1, 3, 7, 14, 30, 60, 90):
    F[f"r{L}"] = c / c.shift(L) - 1
F["vol30"] = ret.rolling(30, min_periods=20).std()
F["vol7"] = ret.rolling(7, min_periods=5).std()
F["dhi90"] = c / c.rolling(90, min_periods=30).max() - 1
F["sma50"] = c / c.rolling(50, min_periods=30).mean() - 1
tr_ = (P["tbq"] / P["qv"]).replace([np.inf, -np.inf], np.nan)
F["akis7"] = tr_.rolling(7, min_periods=5).mean() - tr_.rolling(60, min_periods=30).mean()
F["vsp7"] = np.log(P["qv"].rolling(7, min_periods=5).mean() / P["qv"].rolling(60, min_periods=30).mean())
F["adv"] = np.log(X["adv"])
F["age"] = np.log(X["age"].clip(lower=1))
b = c[BTC]
mkt = {"btc_r30": b / b.shift(30) - 1, "btc_sma200": b / b.rolling(200).mean() - 1}
fwd = c.shift(-7) / c - 1
# kesitsel normalizasyon: her gun evrendeki yuzdelik sira (rejimden bagimsiz)
Fr = {k: v.where(U).rank(axis=1, pct=True) for k, v in F.items()}
y = fwd.where(U).rank(axis=1, pct=True)
days = c.index[(c.index.dayofweek == 0) & (c.index >= "2018-03-01")]


def frame(ds):
    rows = []
    for d in ds:
        u = U.loc[d]
        cols = u.index[u.values]
        if len(cols) < 10:
            continue
        m = pd.DataFrame({k: v.loc[d, cols] for k, v in Fr.items()})
        for k, v in mkt.items():
            m[k] = v.loc[d]
        m["y"] = y.loc[d, cols]
        m["d"] = d
        rows.append(m)
    return pd.concat(rows)


D = frame(days)
feats = [k for k in D.columns if k not in ("y", "d")]
print("ornek", len(D), "ozellik", len(feats), f"{time.time() - t0:.0f}s", flush=True)
pred = pd.Series(np.nan, index=D.index.astype(str) + "|" + D["d"].astype(str))
D["pred"] = np.nan
ic = {}
for yr in range(2019, c.index[-1].year + 1):
    start = pd.Timestamp(f"{yr}-01-01")
    trn = D[(D.d < start - pd.Timedelta(days=8)) & D.y.notna()]
    tst = D.d.between(start, pd.Timestamp(f"{yr}-12-31"))
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=0)
    m.fit(trn[feats], trn.y)
    D.loc[tst, "pred"] = m.predict(D.loc[tst, feats])
    t = D[tst & D.y.notna()]
    ic[yr] = round(t.groupby("d").apply(lambda g: g.pred.corr(g.y, method="spearman")).mean(), 3)
    print(yr, "egitim", len(trn), "IC", ic[yr], flush=True)

# momentum tek basina ayni IC olcusu (ML deger katiyor mu?)
ic_mom = {yr: round(D[D.d.dt.year == yr].groupby("d").apply(lambda g: g.r30.corr(g.y, method="spearman")).mean(), 3)
          for yr in ic}
print("IC ML ", ic, "\nIC r30", ic_mom)
PR = D.reset_index().pivot_table(index="d", columns="index", values="pred").reindex(c.index).ffill(limit=6)
PR = PR.reindex(columns=c.columns)
res = []
reg = (b > b.rolling(200).mean()).fillna(False).values[:, None]
reg100 = (b > b.rolling(100).mean()).fillna(False).values[:, None]
REB = pd.DataFrame(np.repeat((c.index.dayofweek == 0)[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
for K in (3, 5, 10):
    for rg_name, rg in (("yok", True), ("sma100", reg100), ("sma200", reg)):
        sig = (PR.rank(axis=1, ascending=False) <= K) & PR.notna() & rg
        eq, tu = PN.backtest(sig.astype(float) / K, X, rebal=REB)
        r = PN.report(f"ML_K{K}_rg{rg_name}", eq, tu, start="2019-01-01")
        res.append(r)
        print(r["name"], "egitim(2019-21)", r["train"], "| test", r["test"], "| devir", r["turnover_y"], flush=True)
W = pd.DataFrame(0.0, index=c.index, columns=c.columns); W[BTC] = 1.0
eq, tu = PN.backtest(W, X)
r = PN.report("BH_BTC", eq, tu, start="2019-01-01"); res.append(r)
print("BTC", r["train"], r["test"])
json.dump({"ic_ml": ic, "ic_mom": ic_mom, "res": res}, open(os.path.join(OUT, "ml_sonuc.json"), "w"), default=str, indent=1)
print(f"sure {time.time() - t0:.0f}s")
