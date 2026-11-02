"""Dengelemede 24 saatlik zarar limiti: kasa bir gunde %X dustuyse o gun alim yok (satis serbest). 70/30 portfoy, koruma.py ile ayni ML."""
import os
__file__ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "koruma.py")
exec(open(__file__, encoding="utf-8").read().split("V = {")[0])


def sim(W, limit=None, cost=PN.COST):
    W = W.reindex_like(X["c"]).fillna(0.0).where(X["alive"], 0.0)
    R, last, w = X["ret"].values, X["last"].values, W.values
    n, m = w.shape
    eq, cur, blocked = np.ones(n), np.zeros(m), np.zeros(n, bool)
    for i in range(1, n):
        g = cur * (1 + R[i])
        val = g.sum() + (1 - cur.sum())
        eq[i] = eq[i - 1] * val
        cur = g / val if val > 0 else np.zeros(m)
        tgt = w[i]
        if limit is not None and val - 1 <= -limit:
            blocked[i] = True
            tgt = np.minimum(tgt, cur)
        eq[i] *= 1 - np.abs(tgt - cur).sum() * cost - (tgt * last[i]).sum() * PN.DELIST_HIT
        cur = tgt.copy()
    return pd.Series(eq, index=c.index), pd.Series(blocked, index=c.index)


for name, lim in (("limit yok", None), ("%3", 0.03), ("%5", 0.05)):
    eq, bl = sim(base, lim)
    te, tr = PN.stats(eq, PN.TRAIN_END, None), PN.stats(eq, "2018-03-01", PN.TRAIN_END)
    yrs = {y: round(100 * (g.iloc[-1] / g.iloc[0] - 1)) for y, g in eq[PN.TRAIN_END:].groupby(eq[PN.TRAIN_END:].index.year)}
    print(f"{name:<10} TEST yillik {te['cagr']:6.1f}% dusus {te['mdd']:6.1f}% Sharpe {te['sharpe']:.2f} | EGITIM yillik {tr['cagr']:6.1f}% "
          f"dusus {tr['mdd']:6.1f}% | test gunu engel {int(bl[PN.TRAIN_END:].sum())}/{len(bl[PN.TRAIN_END:])} | {yrs}", flush=True)
