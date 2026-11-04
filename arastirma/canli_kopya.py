"""Canli yapinin kopyasi: 30 gunde bir egitim, ayni gun satirlari, sabit 100 tur (erken durdurma yok), canli bantlar %3/%1.
Eski test (yillik egitim, Pazartesi mumu, tek tohum) %26 gosteriyordu; canli bot Pazar mumunu kullaniyor ve %14 ediyordu.
Gunler arasi farkin cogu haftalik BTC rejim orneklemesinin sansi (rastgele sepette de ayni desen). Carsamba test goruldukten sonra secildi,
gercekci beklenti 7 gun ortalamasidir. Calistirma: python canli_kopya.py (indir_1d.py sonrasi, ~5 dk)."""
import os, sys, warnings
import numpy as np, pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor as HGB
warnings.filterwarnings("ignore")
H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, H)
import panel as PN

g = {"__file__": os.path.join(H, "ml.py"), "__name__": "ml"}
exec(open(g["__file__"], encoding="utf-8").read().split("D = frame(days)")[0], g)
P, X, c, ret, frame, b = g["P"], g["X"], g["c"], g["ret"], g["frame"], g["b"]
BTC, ETH, TE = "BTCUSDT", "ETHUSDT", PN.TRAIN_END
A = frame(c.index[c.index >= "2018-03-01"])
A["iy"] = A.index
fs = [k for k in A.columns if k not in ("y", "d", "iy")]
reg_d = (b > b.rolling(100).mean()).fillna(False)
Xa, ds = A[fs].values, A.d.values
GUN = "Pzt Sal Car Per Cum Cmt Paz".split()


def scores(day, it=100, es=False, seed=0):
    """day: egitim satirlarinin gunu. Tum gunler tahmin edilir."""
    rows = A[A.d.dt.dayofweek == day]
    cuts = list(pd.date_range("2020-01-01", c.index[-1], freq="30D")) + [c.index[-1] + pd.Timedelta(days=1)]
    pred = np.full(len(A), np.nan)
    kw = {"early_stopping": False} if not es else {}
    for a, z in zip(cuts[:-1], cuts[1:]):
        m = (ds >= np.datetime64(a)) & (ds < np.datetime64(z))
        if m.any():
            tr = rows[(rows.d < a - pd.Timedelta(days=8)) & rows.y.notna()]
            pred[m] = HGB(max_iter=it, learning_rate=0.05, max_leaf_nodes=15, min_samples_leaf=200, l2_regularization=1.0,
                          random_state=seed, **kw).fit(tr[fs].values, tr.y.values).predict(Xa[m])
    t = A[["d", "iy"]].copy()
    t["p"] = pred
    return t.dropna().pivot_table(index="d", columns="iy", values="p").reindex(index=c.index, columns=c.columns)


def gate(day, n=3):
    """Rejim sepet gunu okunur; hafta icinde n kapanis ust uste altindaysa cikis, geri donus sadece sepet gunu. n=None: sadece sepet gunu."""
    below = (~reg_d).astype(int).rolling(n or 1).sum() == (n or 1)
    cur, out = False, []
    for r, bl, x in zip(c.index.dayofweek == day, below.values, reg_d.values):
        if r:
            cur = bool(x)
        elif n and bl:
            cur = False
        out.append(cur)
    return np.array(out)[:, None]


def ml_w(sc, day, n=3):
    reb = np.repeat((c.index.dayofweek == day)[:, None], c.shape[1], 1)
    top = (sc.rank(axis=1, ascending=False) <= 5) & sc.notna()
    return top.astype(float).div(5).where(reb, np.nan).ffill().fillna(0.0) * gate(day, n)


def core():
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns)
    for s in (BTC, ETH):
        e = sum((c[s] > c[s].rolling(n).mean()).astype(float) for n in (20, 50, 100, 200)) / 4
        W[s] = 0.5 * e * (0.4 / (ret[s].rolling(30).std() * np.sqrt(365))).clip(upper=1.0)
    return W


def sim(W, day, cost=PN.COST, band=0.03, band_ml=0.01):
    W = W.reindex_like(c).fillna(0.0).where(X["alive"], 0.0)
    R, last, w = ret.values, X["last"].values, W.values
    reb = c.index.dayofweek == day
    n, m = w.shape
    eq, cur = np.ones(n), np.zeros(m)
    for i in range(1, n):
        v = cur * (1 + R[i])
        val = v.sum() + (1 - cur.sum())
        eq[i] = eq[i - 1] * val
        cur = v / val if val > 0 else np.zeros(m)
        tgt = w[i].copy()
        keep = (np.abs(tgt - cur) < (band_ml if reb[i] else band)) & ~((cur == 0) & (tgt >= 0.01)) & (tgt >= 0.005)
        tgt[keep] = cur[keep]
        eq[i] *= 1 - np.abs(tgt - cur).sum() * cost - (tgt * last[i]).sum() * PN.DELIST_HIT
        cur = tgt
    return pd.Series(eq, index=c.index)


def show(ad, W, day):
    e = sim(W, day)
    a, x, x2 = PN.stats(e, "2020-01-01", TE), PN.stats(e, TE, None), PN.stats(sim(W, day, cost=2 * PN.COST), TE, None)
    y = {int(k): round(100 * (v.iloc[-1] / v.iloc[0] - 1)) for k, v in e[TE:].groupby(e[TE:].index.year)}
    print(f"{ad:<24} egitim {a['cagr']:6.1f}/{a['mdd']:6.1f}/S{a['sharpe']:.2f} test {x['cagr']:5.1f}/{x['mdd']:6.1f}/S{x['sharpe']:.2f} 2x {x2['cagr']:5.1f} {y}", flush=True)


if __name__ == "__main__":
    C_ = core()
    old = scores(0, it=200, es=True)
    show("eski canli (Paz mumu)", 0.7 * C_ + 0.3 * ml_w(old, 6, None), 6)
    new = scores(2)
    show("canli (Car, 3 gun cikis)", 0.7 * C_ + 0.3 * ml_w(new, 2), 2)
    show("3 gun cikis olmadan", 0.7 * C_ + 0.3 * ml_w(new, 2, None), 2)
    show("sadece cekirdek", C_, 2)
    show("sadece ML (Car)", ml_w(new, 2), 2)
    W = pd.DataFrame(0.0, index=c.index, columns=c.columns); W[BTC] = 1.0
    show("BTC al-tut", W, 2)
    cg = []
    for d in range(7):
        sd = scores(d)
        cg.append(PN.stats(sim(0.7 * C_ + 0.3 * ml_w(sd, d), d), TE, None)["cagr"])
    print("kendi gun satiriyla, test yillik:", dict(zip(GUN, map(float, cg))), "ort", round(float(np.mean(cg)), 1), flush=True)
    U = PN.universe(X, top=50)
    rd = [np.mean([PN.stats(sim(ml_w(pd.DataFrame(np.random.default_rng(s).random(c.shape), c.index, c.columns).where(U), d), d), TE, None)["cagr"] for s in range(10)]) for d in range(7)]
    print("10 rastgele sepet, ortalama test yillik:", dict(zip(GUN, np.round(rd, 1).tolist())))
