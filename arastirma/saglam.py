"""Kazanan aile (BTC trend + oynaklik hedefleme) saglamlik testi: parametre komsulugu, 1 gun gecikme, 2x maliyet,
yeniden dengeleme esigi, yillik getiriler, ETH ekleme. Sadece BTC/ETH verisi."""
import os, sys, itertools, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import panel as PN

D = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d1")
px = {s: pd.Series(np.load(os.path.join(D, s + ".npz"))["c"], pd.to_datetime(np.load(os.path.join(D, s + ".npz"))["t"], unit="ms"))
      for s in ("BTCUSDT", "ETHUSDT")}
c = pd.DataFrame(px).asfreq("D")
r = c.pct_change(fill_method=None).fillna(0)


def sim(w, cost=0.0015, lag=0, band=0.0):
    """w: hedef agirlik serisi/df (gun t kapanisinda bilinir). lag: islem kac gun gecikir. band: |hedef-mevcut|<band ise islem yok."""
    w = w.shift(lag).fillna(0.0)
    W = w.values if w.ndim == 2 else w.values[:, None]
    R = r[w.columns].values if w.ndim == 2 else r[[w.name]].values
    eq = np.ones(len(W)); cur = np.zeros(W.shape[1]); turn = 0
    for i in range(1, len(W)):
        g = cur * (1 + R[i]); val = g.sum() + 1 - cur.sum(); eq[i] = eq[i - 1] * val
        cur = g / val
        tgt = W[i].copy()
        keep = np.abs(tgt - cur) < band
        tgt[keep & (tgt > 0)] = cur[keep & (tgt > 0)]   # kucuk ayar yapma (pozisyondan cikis her zaman yapilir)
        tr = np.abs(tgt - cur).sum(); turn += tr
        eq[i] *= 1 - tr * cost; cur = tgt
    return pd.Series(eq, c.index), turn / (len(W) / 365)


def strat(sym, n, vt, vwin=30):
    b = c[sym]
    on = (b > b.rolling(n).mean()).astype(float)
    if vt:
        v = r[sym].rolling(vwin).std() * np.sqrt(365)
        on = on * (vt / v).clip(upper=1.0)
    return on.rename(sym)


def line(name, eq, to):
    tr, te = PN.stats(eq, "2018-01-01", PN.TRAIN_END), PN.stats(eq, PN.TRAIN_END, None)
    return f"{name:<34} egitim CAGR {tr['cagr']:6.1f} DD {tr['mdd']:6.1f} Sh {tr['sharpe']:.2f} | test CAGR {te['cagr']:6.1f} " \
           f"DD {te['mdd']:6.1f} Sh {te['sharpe']:.2f} Calmar {te['calmar']:.2f} | devir/yil {to:.1f}"


bh, _ = sim(pd.Series(1.0, c.index, name="BTCUSDT"))
print(line("BTC al-tut", bh, 0))
print("\n-- parametre izgarasi (test Sharpe) --")
grid = {}
for n in (20, 30, 50, 75, 100, 150, 200):
    row = []
    for vt in (None, 0.3, 0.4, 0.5, 0.6, 0.8):
        eq, to = sim(strat("BTCUSDT", n, vt))
        s = PN.stats(eq, PN.TRAIN_END, None)["sharpe"]
        grid[(n, vt)] = s
        row.append(f"{s:5.2f}")
    print(f"SMA{n:<4}", " ".join(row), "   (vt: yok .3 .4 .5 .6 .8)")
print("izgaranin %si BTC al-tut test Sharpe'ini geciyor:", f"{np.mean([v > PN.stats(bh, PN.TRAIN_END, None)['sharpe'] for v in grid.values()]):.0%}")
base = strat("BTCUSDT", 50, 0.4)
print("\n-- secilen: BTC SMA50, hedef oynaklik %40 --")
for name, kw in (("normal", {}), ("1 gun gecikmeli islem", {"lag": 1}), ("2x maliyet", {"cost": 0.003}),
                 ("bant %10 (az islem)", {"band": 0.10}), ("bant %20", {"band": 0.20})):
    eq, to = sim(base, **kw)
    print(line(name, eq, to))
eq, _ = sim(base, band=0.10)
yr = eq["2018":].groupby(eq["2018":].index.year).apply(lambda g: round(100 * (g.iloc[-1] / g.iloc[0] - 1)))
yb = bh["2018":].groupby(bh["2018":].index.year).apply(lambda g: round(100 * (g.iloc[-1] / g.iloc[0] - 1)))
print("\nyillik % (strateji / BTC):", {y: (int(yr[y]), int(yb[y])) for y in yr.index})
print("piyasada kalma orani:", round((base > 0).mean(), 2), "ort agirlik:", round(base.mean(), 2))
for n in (50, 100):
    w = pd.concat([strat("BTCUSDT", n, 0.4) * 0.6, strat("ETHUSDT", n, 0.4) * 0.4], axis=1)
    eq, to = sim(w, band=0.10)
    print(line(f"BTC60/ETH40 SMA{n} vt.4", eq, to))
    w = pd.concat([strat("BTCUSDT", n, 0.4) * 0.5, strat("ETHUSDT", n, 0.4) * 0.5], axis=1)
    eq, to = sim(w, band=0.10)
    print(line(f"BTC50/ETH50 SMA{n} vt.4", eq, to))
# SMA'larin ortalamasi (tek parametreye bagimlilik yok)
ens = sum(strat("BTCUSDT", n, None) for n in (20, 50, 100, 200)) / 4
v = r["BTCUSDT"].rolling(30).std() * np.sqrt(365)
ens = (ens * (0.4 / v).clip(upper=1.0)).rename("BTCUSDT")
for name, kw in (("normal", {"band": 0.10}), ("1 gun gecikme", {"band": 0.10, "lag": 1}), ("2x maliyet", {"band": 0.10, "cost": 0.003})):
    eq, to = sim(ens, **kw)
    print(line(f"ENS SMA20/50/100/200 vt.4 {name}", eq, to))
eq, _ = sim(ens, band=0.10)
yr = eq["2018":].groupby(eq["2018":].index.year).apply(lambda g: round(100 * (g.iloc[-1] / g.iloc[0] - 1)))
print("ENS yillik % (strateji / BTC):", {y: (int(yr[y]), int(yb[y])) for y in yr.index})
