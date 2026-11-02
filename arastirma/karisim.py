"""Cekirdek (BTC/ETH trend, SMA20/50/100/200 ortalamasi, hedef oynaklik %40) + ML alt sepeti (K5, BTC>SMA100) karisimi."""
import os, sys
__file__ = os.path.abspath(__file__)
exec(open(os.path.join(os.path.dirname(__file__), "ml.py"), encoding="utf-8").read().split("pred = pd.Series")[0])
D["pred"] = np.nan
for yr in range(2020, c.index[-1].year + 1):
    start = pd.Timestamp(f"{yr}-01-01")
    trn = D[(D.d < start - pd.Timedelta(days=8)) & D.y.notna()]
    tst = D.d.between(start, pd.Timestamp(f"{yr}-12-31"))
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=0).fit(trn[feats], trn.y)
    D.loc[tst, "pred"] = m.predict(D.loc[tst, feats])
reg100 = (b > b.rolling(100).mean()).fillna(False).values[:, None]
REB = pd.DataFrame(np.repeat((c.index.dayofweek == 0)[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
S = D.reset_index().pivot_table(index="d", columns="index", values="pred").reindex(c.index).ffill(limit=6).reindex(columns=c.columns)
Wml = ((S.rank(axis=1, ascending=False) <= 5) & S.notna() & reg100).astype(float) / 5
Wml = Wml.where(REB, np.nan).ffill().fillna(0.0)


def ens(sym):
    p = c[sym]
    e = sum((p > p.rolling(n).mean()).astype(float) for n in (20, 50, 100, 200)) / 4
    v = ret[sym].rolling(30).std() * np.sqrt(365)
    return e * (0.4 / v).clip(upper=1.0)


Wc = pd.DataFrame(0.0, index=c.index, columns=c.columns)
Wc[BTC], Wc["ETHUSDT"] = 0.5 * ens(BTC), 0.5 * ens("ETHUSDT")
Wb = pd.DataFrame(0.0, index=c.index, columns=c.columns); Wb[BTC] = 1.0
start = "2020-01-01"
for name, W in [("BTC al-tut", Wb), ("Cekirdek BTC/ETH", Wc), ("ML K5", Wml)] + \
               [(f"Karisim cekirdek {int(a * 100)} / ML {int(100 - a * 100)}", a * Wc + (1 - a) * Wml) for a in (0.8, 0.7, 0.6, 0.5)]:
    eq, tu = PN.backtest(W, X)
    tr, te = PN.stats(eq, start, PN.TRAIN_END), PN.stats(eq, PN.TRAIN_END, None)
    yr = {y: int(round(100 * (g.iloc[-1] / g.iloc[0] - 1))) for y, g in eq[start:].groupby(eq[start:].index.year)}
    print(f"{name:<30} 2020-21 CAGR {tr['cagr']:6.1f} DD {tr['mdd']:6.1f} | TEST 2022+ CAGR {te['cagr']:6.1f} DD {te['mdd']:6.1f} "
          f"Sh {te['sharpe']:.2f} Calmar {te['calmar']:.2f} | {yr}")
