"""Daha hizli tepki testi.
A) ML sepeti: haftalik (mevcut) vs gunluk tahmin + histerezis (sepette kalmak icin ilk 10, girmek icin ilk 5) vs gunluk saf.
B) Cekirdek: gunluk kapanis (mevcut) vs saatlik kontrol (gun ici fiyat SMA'larin altina inince hemen azalt), 1s veri 2 yil."""
import os, sys, glob, pickle
__file__ = os.path.abspath(__file__)
exec(open(os.path.join(os.path.dirname(__file__), "ml.py"), encoding="utf-8").read().split("days = c.index[")[0])
# tum gunler icin ornek (tahmin icin); egitim yine haftalik orneklerle
days_all = c.index[c.index >= "2018-03-01"]
src = open(os.path.join(os.path.dirname(__file__), "ml.py"), encoding="utf-8").read()
exec("def frame(ds):" + src.split("def frame(ds):")[1].split("D = frame(days)")[0])
DA = frame(days_all)
feats = [k for k in DA.columns if k not in ("y", "d")]
DA["pred"] = np.nan
mon = DA.d.dt.dayofweek == 0
for yr in range(2020, c.index[-1].year + 1):
    start = pd.Timestamp(f"{yr}-01-01")
    trn = DA[mon & (DA.d < start - pd.Timedelta(days=8)) & DA.y.notna()]
    tst = DA.d.between(start, pd.Timestamp(f"{yr}-12-31"))
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=0).fit(trn[feats], trn.y)
    DA.loc[tst, "pred"] = m.predict(DA.loc[tst, feats])
print("tahmin hazir", flush=True)
S = DA.reset_index().pivot_table(index="d", columns="index", values="pred").reindex(c.index).reindex(columns=c.columns)
R = S.rank(axis=1, ascending=False)
reg = (b > b.rolling(100).mean()).fillna(False).values[:, None]
REB = pd.DataFrame(np.repeat((c.index.dayofweek == 0)[:, None], c.shape[1], 1), index=c.index, columns=c.columns)


def core():
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for s in (BTC, "ETHUSDT"):
        p = c[s]
        e = sum((p > p.rolling(n).mean()).astype(float) for n in (20, 50, 100, 200)) / 4
        W[s] = 0.5 * e * (0.4 / (ret[s].rolling(30).std() * np.sqrt(365))).clip(upper=1.0)
    return W


def hyster(enter=5, stay=10):
    held, out = set(), np.zeros(S.shape)
    Rv, regv, cols = R.values, reg[:, 0], list(S.columns)
    for i in range(len(S)):
        if not regv[i]:
            held = set()
            continue
        r = Rv[i]
        held = {j for j in held if r[j] <= stay}
        for j in np.argsort(np.where(np.isnan(r), 1e9, r)):
            if len(held) >= enter or np.isnan(r[j]) or r[j] > enter:
                break
            held.add(j)
        for j in held:
            out[i, j] = 1.0 / enter
    return pd.DataFrame(out, index=S.index, columns=S.columns)


Wc = core()
weekly = ((R <= 5) & S.notna() & reg).astype(float).div(5).where(REB, np.nan).ffill().fillna(0.0)
daily = ((R <= 5) & S.notna() & reg).astype(float) / 5
V = {"ML haftalik (mevcut)": weekly, "ML gunluk saf": daily, "ML gunluk histerezis 5/10": hyster(5, 10),
     "ML gunluk histerezis 5/15": hyster(5, 15)}
for name, Wm in V.items():
    for lab, W in (("sadece ML", Wm), ("70/30 portfoy", 0.7 * Wc + 0.3 * Wm)):
        eq, tu = PN.backtest(W, X)
        te, s6 = PN.stats(eq, PN.TRAIN_END, None), PN.stats(eq, c.index[-1] - pd.Timedelta(days=182), None)
        print(f"{name:<28} {lab:<14} TEST yillik {te['cagr']:6.1f}% dusus {te['mdd']:6.1f}% Sharpe {te['sharpe']:.2f} "
              f"| son 6 ay {s6['tot']:6.1f}% | devir/yil {tu[PN.TRAIN_END:].mean() * 365:.1f}", flush=True)

