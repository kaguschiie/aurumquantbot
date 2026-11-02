"""Gunluk panel + portfoy backtest motoru (ileriye bakissiz, maliyetli, delist edilen coinler dahil).
Agirlik w[t] gun t kapanisinda bilinen veriyle hesaplanir, t kapanisinda islenir, t+1 getirisini alir."""
import glob, os
import numpy as np, pandas as pd

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d1")
COST = 0.0015            # tek yon: %0.1 taker komisyon + %0.05 kayma
DELIST_HIT = 0.05        # verisi biten coinde zorunlu cikis cezasi
DAY = 86_400_000
TRAIN_END = pd.Timestamp("2022-01-01")


def load(min_days=30):
    fr = {}
    for f in glob.glob(os.path.join(D, "*.npz")):
        z = np.load(f)
        if len(z["t"]) < min_days:
            continue
        idx = pd.to_datetime(z["t"], unit="ms")
        fr[os.path.basename(f)[:-4]] = pd.DataFrame({k: z[k] for k in ("o", "h", "l", "c", "qv", "tbq")}, index=idx)
    t = pd.date_range("2017-08-17", max(df.index[-1] for df in fr.values()), freq="D")
    P = {k: pd.DataFrame({s: df[k] for s, df in fr.items()}).reindex(t) for k in ("o", "h", "l", "c", "qv", "tbq")}
    return P


def prep(P):
    c = P["c"]
    ret = c.pct_change(fill_method=None)
    alive = c.notna()
    # son gecerli gunden sonrasi: delist. Son gun getirisine ceza uygulanir
    last = alive & ~alive.shift(-1, fill_value=False) & (alive.index < alive.index[-1])[:, None]
    age = alive.cumsum()
    adv = P["qv"].rolling(30, min_periods=20).mean()
    return dict(c=c, ret=ret.fillna(0.0), alive=alive, last=last, age=age, adv=adv)


def universe(X, top=50, min_age=60, min_adv=1e6):
    a = X["adv"].where((X["age"] >= min_age) & X["alive"] & (X["adv"] >= min_adv))
    r = a.rank(axis=1, ascending=False)
    return r <= top


def backtest(W, X, cost=COST, rebal=None):
    """W: gun x sembol hedef agirlik (toplam <=1, kalan nakit). rebal: True olan gunlerde agirlik guncellenir."""
    W = W.reindex_like(X["c"]).fillna(0.0)
    if rebal is not None:
        W = W.where(rebal, np.nan).ffill().fillna(0.0)
    W = W.where(X["alive"], 0.0)
    R = X["ret"].values
    last = X["last"].values
    w = W.values
    n, m = w.shape
    eq = np.ones(n)
    cur = np.zeros(m)
    turn = np.zeros(n)
    for i in range(1, n):
        g = cur * (1 + R[i])                      # dunku pozisyon bugunun getirisini alir
        val = g.sum() + (1 - cur.sum())
        eq[i] = eq[i - 1] * val
        cur = g / val if val > 0 else np.zeros(m)
        tgt = w[i]
        tr = np.abs(tgt - cur).sum()
        turn[i] = tr
        eq[i] *= 1 - tr * cost - (tgt * last[i]).sum() * DELIST_HIT  # son gunu olan coini tasimak: delist cezasi
        cur = tgt.copy()
    return pd.Series(eq, index=X["c"].index), pd.Series(turn, index=X["c"].index)


def stats(eq, a=None, b=None):
    e = eq[a:b] if (a is not None or b is not None) else eq
    e = e / e.iloc[0]
    r = e.pct_change().dropna()
    yrs = len(r) / 365
    cagr = e.iloc[-1] ** (1 / yrs) - 1 if yrs > 0 else 0
    dd = (e / e.cummax() - 1).min()
    sh = r.mean() / r.std() * np.sqrt(365) if r.std() > 0 else 0
    return dict(cagr=round(100 * cagr, 1), mdd=round(100 * dd, 1), sharpe=round(sh, 2),
                calmar=round(cagr / -dd, 2) if dd < 0 else 0, tot=round(100 * (e.iloc[-1] - 1), 1))


def report(name, eq, turn=None, start="2018-01-01"):
    tr, te = stats(eq, start, TRAIN_END), stats(eq, TRAIN_END, None)
    yr = {y: round(100 * (g.iloc[-1] / g.iloc[0] - 1), 0) for y, g in eq[start:].groupby(eq[start:].index.year)}
    to = round(turn[start:].mean() * 365, 1) if turn is not None else 0
    return dict(name=name, train=tr, test=te, years=yr, turnover_y=to)
