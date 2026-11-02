"""Cekirdek (BTC/ETH) saatlik tepki testi, 1s veri ~3 yil. Agirlik = 0.5 x (SMA20/50/100/200 ustunde kalma orani) x
min(1, %40 / 30g oynaklik). Varyantlar: gunluk kapanis (mevcut) / saatlik canli (gun icinde fiyat SMA'yi kesince) /
sadece satis saatlik (tehlikede hizli cik, alis gun kapanisinda). Maliyet %0.15 tek yon, bant 0.03."""
import os
import numpy as np, pandas as pd
G = os.environ.get("KQB_SAATLIK_VERI", "data")  # uygulamanin 1s mum onbellegi: {APP_DIR}/data/BTCUSDT_1h.pkl
COST, BAND, SMAS = 0.0015, 0.03, (20, 50, 100, 200)
px = {}
for s in ("BTC", "ETH"):
    d = pd.read_pickle(os.path.join(G, f"{s}USDT_1h.pkl"))
    px[s] = pd.Series(d["close"].values, pd.to_datetime(d.index, unit="ms"))
P = pd.DataFrame(px).dropna()
day = P.resample("1D").last()
dret = day.pct_change()


def weights(mode):
    W = pd.DataFrame(index=P.index, columns=P.columns, dtype=float)
    for s in P.columns:
        dc = day[s]
        vol = (dret[s].rolling(30).std() * np.sqrt(365)).shift(1)       # dunku kapanisa kadar
        vt = (0.4 / vol).clip(upper=1.0)
        # gunluk: dunku kapanisla hesaplanan agirlik bugun boyunca
        e_day = (sum((dc > dc.rolling(n).mean()).astype(float) for n in SMAS) / 4)
        w_day = (0.5 * e_day * (0.4 / (dret[s].rolling(30).std() * np.sqrt(365))).clip(upper=1.0)).shift(1)
        w_day_h = w_day.reindex(P.index, method="ffill")
        if mode == "gunluk":
            W[s] = w_day_h
            continue
        # canli: SMA_n = (son n-1 gun kapanisi + simdiki fiyat) / n
        h = P[s]
        dd = h.index.floor("D")
        e = 0
        for n in SMAS:
            prev_sum = dc.rolling(n - 1).sum().shift(1).reindex(dd).values  # dunden geriye n-1 kapanis
            sma_live = (prev_sum + h.values) / n
            e = e + (h.values > sma_live).astype(float)
        w_live = pd.Series(0.5 * e / 4 * vt.reindex(dd).values, index=h.index)
        W[s] = w_live if mode == "saatlik" else np.minimum(w_live, w_day_h)  # sadece azaltma saatlik
    return W.fillna(0.0)


def sim(W):
    r = P.pct_change().fillna(0.0).values
    w = W.values
    cur = np.zeros(2)
    eq = np.ones(len(P))
    turn = 0.0
    for i in range(1, len(P)):
        g = cur * (1 + r[i])
        val = g.sum() + 1 - cur.sum()
        eq[i] = eq[i - 1] * val
        cur = g / val
        tgt = w[i].copy()
        for j in range(2):
            if abs(tgt[j] - cur[j]) < BAND and not (tgt[j] == 0 and cur[j] > 0):
                tgt[j] = cur[j]
        tr = np.abs(tgt - cur).sum()
        turn += tr
        eq[i] *= 1 - tr * COST
        cur = tgt
    return pd.Series(eq, P.index), turn / (len(P) / 8760)


def st(e):
    yrs = (e.index[-1] - e.index[0]).days / 365
    dd = (e / e.cummax() - 1).min()
    dr = e.resample("1D").last().pct_change().dropna()
    return f"yillik {100 * (e.iloc[-1] ** (1 / yrs) - 1):6.1f}% dusus {100 * dd:6.1f}% Sharpe {dr.mean() / dr.std() * np.sqrt(365):.2f}"


start = P.index[0] + pd.Timedelta(days=210)
for mode in ("gunluk", "saatlik", "sadece_satis_saatlik"):
    eq, to = sim(weights(mode)[start:].reindex(P.index).fillna(0.0))
    e = eq[start:]
    last6 = e[e.index[-1] - pd.Timedelta(days=182):]
    print(f"{mode:<22} {st(e)} | son 6 ay {100 * (last6.iloc[-1] / last6.iloc[0] - 1):6.1f}% (dusus {100 * (last6 / last6.cummax() - 1).min():5.1f}%) | devir/yil {to:.1f}")
b = P["BTC"][start:]
print("BTC al-tut              ", st(b / b.iloc[0]))
