"""Dususte zarari azaltma fikirleri (70/30 portfoy uzerine): hizli trend, dusus freni, sert ML rejimi, ML coinine trend filtresi.
Test 2022-26 + son 6 ay + 2022 yili. karisim.py ile ayni ML tahminleri."""
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
REB = pd.DataFrame(np.repeat((c.index.dayofweek == 0)[:, None], c.shape[1], 1), index=c.index, columns=c.columns)
S = D.reset_index().pivot_table(index="d", columns="index", values="pred").reindex(c.index).ffill(limit=6).reindex(columns=c.columns)
top5 = (S.rank(axis=1, ascending=False) <= 5) & S.notna()


def ml_w(reg):
    W = (top5 & reg).astype(float) / 5
    return W.where(REB, np.nan).ffill().fillna(0.0)


def ens(sym, smas=(20, 50, 100, 200), vt=0.4):
    p = c[sym]
    e = sum((p > p.rolling(n).mean()).astype(float) for n in smas) / len(smas)
    return e * (vt / (ret[sym].rolling(30).std() * np.sqrt(365))).clip(upper=1.0)


def core(smas=(20, 50, 100, 200), vt=0.4):
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    W[BTC], W["ETHUSDT"] = 0.5 * ens(BTC, smas, vt), 0.5 * ens("ETHUSDT", smas, vt)
    return W


reg = lambda n: (b > b.rolling(n).mean()).fillna(False).values[:, None]
base = 0.7 * core() + 0.3 * ml_w(reg(100))
V = {"1 mevcut (70/30)": base,
     "2 hizli trend (SMA10/20/50/100)": 0.7 * core((10, 20, 50, 100)) + 0.3 * ml_w(reg(100)),
     "3 daha dusuk oynaklik hedefi (%30)": 0.7 * core(vt=0.3) + 0.3 * ml_w(reg(100)),
     "4 ML sert rejim (BTC>SMA50)": 0.7 * core() + 0.3 * ml_w(reg(50)),
     "5 ML coini kendi SMA20 altinda -> sat": 0.7 * core() + 0.3 * ml_w(reg(100)).where(c > c.rolling(20).mean(), 0.0),
     "6 ML sepeti BTC trend oraniyla olcekli": 0.7 * core() + 0.3 * ml_w(True).mul(
         (sum((b > b.rolling(n).mean()).astype(float) for n in (20, 50, 100, 200)) / 4).values, axis=0)}
eq0, _ = PN.backtest(base, X)
dd = eq0 / eq0.rolling(60, min_periods=1).max() - 1
brake = pd.Series(np.where(dd.shift(1) < -0.10, 0.5, 1.0), index=c.index)
V["7 dusus freni (60g zirveden -%10 -> yarim)"] = base.mul(brake, axis=0)
Wb = pd.DataFrame(0.0, index=c.index, columns=c.columns); Wb[BTC] = 1.0
V["BTC al-tut"] = Wb
for name, W in V.items():
    eq, tu = PN.backtest(W, X)
    te, s6 = PN.stats(eq, PN.TRAIN_END, None), PN.stats(eq, c.index[-1] - pd.Timedelta(days=182), None)
    y22 = PN.stats(eq, "2022-01-01", "2022-12-31")
    print(f"{name:<44} TEST yillik {te['cagr']:6.1f}% en derin {te['mdd']:6.1f}% Sharpe {te['sharpe']:.2f} | 2022 {y22['tot']:6.1f}% "
          f"| son 6 ay {s6['tot']:6.1f}% (dusus {s6['mdd']:5.1f}%)")
