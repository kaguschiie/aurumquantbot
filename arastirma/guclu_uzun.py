"""GUCLU AL 4s ve 1g - UZUN TUTMA: onceki test 4/12/24 SAATTE cikiyordu. Burada sinyal kendi zaman diliminde tutulur
(gunler/haftalar) ve botun gercek teknik ozet fonksiyonu (teknik.rating_series) kullanilir.
Olay: rec 0.5'i yukari keser (taze GUCLU AL), likit coin. Giris kapanis + maliyet.
Kiyas: AYNI ZAMANDA tum likit coinlerin ortalama getirisi (piyasa) -> fazla getiri; gun kumelemeli t; medyan ve %1-99 kirpilmis ort."""
import glob, os, sys, time, json
import numpy as np, pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import teknik as TK

S = os.path.dirname(os.path.abspath(__file__))
COST = 0.0015


def load_d1():
    out = {}
    for f in glob.glob(os.path.join(S, "d1", "*.npz")):
        z = np.load(f)
        if len(z["t"]) < 250:
            continue
        df = pd.DataFrame({"open": z["o"], "high": z["h"], "low": z["l"], "close": z["c"], "qv": z["qv"]},
                          index=pd.to_datetime(z["t"], unit="ms"))
        df["volume"] = df.qv / df.close.replace(0, np.nan)
        out[os.path.basename(f)[:-4]] = df
    return out


def load_4h():
    out = {}
    for f in glob.glob(os.path.join(S, "veri4h", "*.npz")):  # 4 saatlik veri depoda ve indir_1d.py'de yok
        z = np.load(f)
        if not {"o", "h", "l", "c", "v", "qv"} <= set(z.files):
            continue
        out[os.path.basename(f)[:-4]] = pd.DataFrame(
            {"open": z["o"], "high": z["h"], "low": z["l"], "close": z["c"], "volume": z["v"], "qv": z["qv"]},
            index=pd.to_datetime(z["t"], unit="ms"))
    return out


def trim(a):
    lo, hi = np.nanpercentile(a, [1, 99])
    return np.clip(a, lo, hi).mean()


def study(fr, bpd, name, split, holds_d=(1, 3, 7, 14, 30), min_qv=3e6):
    btc = fr["BTCUSDT"].close.resample("1D").last()
    reg = btc > btc.rolling(200).mean()
    C = pd.DataFrame({s: d.close for s, d in fr.items()})
    QV = pd.DataFrame({s: d.qv for s, d in fr.items()}).reindex(C.index)
    liq = QV.rolling(bpd, min_periods=bpd).sum() >= min_qv
    fwd = {h: (C.shift(-h * bpd) * (1 - COST) / (C * (1 + COST)) - 1) for h in holds_d}
    mkt = {h: f.where(liq).mean(1) for h, f in fwd.items()}   # ayni anda tum likit coinlerin ortalamasi
    rows = []
    for sym, df in fr.items():
        if len(df) < 300 or sym not in C:
            continue
        rec = TK.rating_series(df)["rec"].reindex(C.index).values
        c = C[sym].values
        n = len(c)
        sma = pd.Series(c).rolling(20).mean().values
        lq = liq[sym].values
        ev = np.zeros(n, bool)
        ev[1:] = (rec[1:] > 0.5) & (rec[:-1] <= 0.5) & lq[1:]
        for i in np.flatnonzero(ev):
            r = {"sym": sym, "t": C.index[i]}
            for h in holds_d:
                r[f"h{h}"] = fwd[h][sym].iat[i]
                r[f"x{h}"] = r[f"h{h}"] - mkt[h].iat[i]
            e = c[i] * (1 + COST)
            jj = np.flatnonzero(rec[i + 1:i + 1 + 60 * bpd] < 0.1)
            j = i + 1 + jj[0] if len(jj) else min(i + 60 * bpd, n - 1)
            r["ozet_cikis"] = c[j] * (1 - COST) / e - 1 if j > i else np.nan
            r["ozet_g"] = (j - i) / bpd
            jj = np.flatnonzero(c[i + 1:i + 1 + 90 * bpd] < sma[i + 1:i + 1 + 90 * bpd])
            j = i + 1 + jj[0] if len(jj) else min(i + 90 * bpd, n - 1)
            r["trend_cikis"] = c[j] * (1 - COST) / e - 1 if j > i else np.nan
            rows.append(r)
    D = pd.DataFrame(rows)
    D["reg"] = reg.reindex(D.t.dt.floor("D")).ffill().fillna(False).values
    D["per"] = np.where(D.t < split, "egitim", "test")
    out = {}
    print(f"\n######## {name}: {len(D)} taze GUCLU AL olayi ########")
    for per in ("egitim", "test"):
        for flt, m in (("hepsi", True), ("BTC>SMA200", D.reg)):
            E = D[(D.per == per) & m]
            print(f"[{per} | {flt}] n={len(E)}")
            for h in holds_d:
                a, x = E[f"h{h}"].dropna(), E[f"x{h}"].dropna()
                g = E.dropna(subset=[f"x{h}"]).groupby(E.t.dt.floor("D"))[f"x{h}"].mean()
                t = g.mean() / (g.std() / np.sqrt(len(g))) if len(g) > 2 else 0
                out[f"{per}|{flt}|h{h}"] = dict(n=len(a), ort_kirp=round(100 * trim(a), 2), medyan=round(100 * a.median(), 2),
                                                kazanma=round((a > 0).mean(), 2), fazla_kirp=round(100 * trim(x), 2),
                                                fazla_medyan=round(100 * x.median(), 2), t=round(t, 2))
                o = out[f"{per}|{flt}|h{h}"]
                print(f"   {h:>2}g tut: net ort {o['ort_kirp']:+.2f}% med {o['medyan']:+.2f}% kaz {o['kazanma']:.0%} | "
                      f"piyasaya gore fazla ort {o['fazla_kirp']:+.2f}% med {o['fazla_medyan']:+.2f}% t={t:.1f}")
            for k in ("ozet_cikis", "trend_cikis"):
                a = E[k].dropna()
                out[f"{per}|{flt}|{k}"] = dict(n=len(a), ort_kirp=round(100 * trim(a), 2), medyan=round(100 * a.median(), 2),
                                               kazanma=round((a > 0).mean(), 2))
                print(f"   {k}: net ort {100 * trim(a):+.2f}% med {100 * a.median():+.2f}% kaz {(a > 0).mean():.0%}")
    print("ozet cikis ort. tutma (gun):", round(D.ozet_g.mean(), 1))
    return out


t0 = time.time()
res = {}
if "4s" not in sys.argv:
    res["1g"] = study(load_d1(), 1, "GUNLUK GUCLU AL (2018-2026, delist dahil)", pd.Timestamp("2022-01-01"))
res["4s"] = study(load_4h(), 6, "4 SAATLIK GUCLU AL (2022-08 ->, likit coinler)", pd.Timestamp("2024-01-01"),
                  holds_d=(1, 3, 7, 14))
json.dump(res, open(os.path.join(S, "guclu_uzun.json"), "w"), indent=1)
print(f"sure {time.time() - t0:.0f}s")
