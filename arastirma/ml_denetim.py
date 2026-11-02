"""ML siralama sonucunun denetimi: sizinti var mi, hangi ozellik calisiyor, tek faktorler ayni isi yapiyor mu,
gecikme/maliyet/yil saglamligi. ml.py'nin veri hazirligini aynen kullanir."""
import os, sys
__file__ = os.path.abspath(__file__)
src = open(os.path.join(os.path.dirname(__file__), "ml.py"), encoding="utf-8").read()
exec(src.split("pred = pd.Series")[0])          # veri + ozellikler + D (haftalik ornekler)
from sklearn.inspection import permutation_importance

D["pred"] = np.nan
imp = []
for yr in range(2020, c.index[-1].year + 1):
    start = pd.Timestamp(f"{yr}-01-01")
    trn = D[(D.d < start - pd.Timedelta(days=8)) & D.y.notna()]
    tst = D.d.between(start, pd.Timestamp(f"{yr}-12-31"))
    m = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200,
                                      l2_regularization=1.0, random_state=0).fit(trn[feats], trn.y)
    D.loc[tst, "pred"] = m.predict(D.loc[tst, feats])
    if yr >= 2023:
        t = D[tst & D.y.notna()]
        pi = permutation_importance(m, t[feats], t.y, n_repeats=3, random_state=0)
        imp.append(pd.Series(pi.importances_mean, feats))
print("ozellik onemi (2023+ test, permutasyon):")
print((pd.concat(imp, axis=1).mean(1) * 1000).sort_values(ascending=False).round(2).to_string())


def ic(col, sign=1):
    t = D[D.d >= "2022-01-01"].dropna(subset=["y"])
    return round(t.groupby("d").apply(lambda g: (sign * g[col]).corr(g.y, method="spearman")).mean(), 3)


print("\nTest IC (2022+): ML", ic("pred"), "| -r7 (haftalik donus)", ic("r7", -1), "| -r1", ic("r1", -1),
      "| adv (buyukluk)", ic("adv"), "| -vol30 (dusuk oynaklik)", ic("vol30", -1), "| age", ic("age"), "| r30", ic("r30"))

reg100 = (b > b.rolling(100).mean()).fillna(False).values[:, None]
REB = pd.DataFrame(np.repeat((c.index.dayofweek == 0)[:, None], c.shape[1], 1), index=c.index, columns=c.columns)


def port(score, K=5, lag=0, cost=PN.COST, name=""):
    S = D.reset_index().pivot_table(index="d", columns="index", values=score).reindex(c.index).ffill(limit=6)
    S = S.reindex(columns=c.columns)
    sig = (S.rank(axis=1, ascending=False) <= K) & S.notna() & reg100
    W = sig.astype(float) / K
    if lag:
        W = W.shift(lag).fillna(0.0)
        rb = REB.shift(lag, fill_value=False)
    else:
        rb = REB
    eq, tu = PN.backtest(W, X, cost=cost, rebal=rb)
    r = PN.report(name or score, eq, tu, start="2019-01-01")
    print(f"{r['name']:<28} test CAGR {r['test']['cagr']:6.1f} DD {r['test']['mdd']:6.1f} Sh {r['test']['sharpe']:.2f} "
          f"| yillar {r['years']}")
    return eq


print()
port("pred", 5, name="ML K5 sma100")
port("pred", 5, lag=1, name="ML K5 1 gun gecikme")
port("pred", 5, cost=0.003, name="ML K5 2x maliyet")
port("pred", 5, cost=0.005, name="ML K5 maliyet %0.5")
port("pred", 3, name="ML K3"); port("pred", 8, name="ML K8"); port("pred", 15, name="ML K15")
D["nr7"] = -D["r7"]; D["nvol"] = -D["vol30"]
port("nr7", 5, name="tek faktor -r7")
port("adv", 5, name="tek faktor buyukluk")
port("nvol", 5, name="tek faktor dusuk oynaklik")
D["mix"] = D["adv"] + D["nvol"]
port("mix", 5, name="buyukluk+dusuk oynaklik")
