import json
import os
import sys
import time
from statistics import NormalDist

import numpy as np
import pandas as pd
import requests

H = os.path.dirname(os.path.abspath(__file__))
CIKTI = os.path.join(H, "sonuclar")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
KASA = 1000.0
VT = 0.40
SPOT, VADELI, FONLAMA, BMAL = 0.0015, 0.0008, 0.0003, 0.003
KRIPTO_ILK, BIST_ILK = pd.Timestamp("2016-01-01"), pd.Timestamp("2008-01-01")
PENCERE = pd.Timestamp("2026-11-13")
ADLAR = {
    "C0": "Taban, uygulamanın trend çekirdeği: coin başına 0.5 × (SMA20/50/100/200 üstünde kalma oranı) × min(1, 0.40/30g yıllık oynaklık), kalan nakit",
    "C1": "Uzun/kısa çekirdek: C0 gibi; dört ortalamanın da altındaysa coin başına 0.5 × min(1, 0.40/oynaklık) KISA (vadeli, fonlama öder)",
    "C2": "C0 + kısa kol: kapanış < SMA50 ve SMA50 < SMA200 iken BTC ve ETH'de ayrı ayrı kasanın %25'i kadar kısa (1x vadeli); kapanış > SMA20 ise kapat; stop giriş +%12",
    "C3": "Izgara, sadece BTC, sadece yatayda (|kapanış/SMA50 - 1| < %5 ve 20g oynaklık 1 yıllık medyanının altında): referansın her -%2 basamağında kasanın %5'i alınır, her lot +%2'de satılır, en çok 5 lot, referans 7 günde bir yenilenir; yatay değilse kapalı (lotlar satılır)",
    "C3b": "C3 hep açık (yatay filtresi yok; lotlar hedefe kadar tutulur)",
    "C4": "Düşüşte dip alımı: BTC < SMA200 ve RSI(2) < 5 ise kasanın %20'si BTC; kapanış > SMA5 ya da 5 gün sonra çık, stop -%8",
    "B0": "XU100 al-tut",
    "B1": "Trend uzun/nakit: kapanış > SMA200 ise XU100, değilse nakit (faiz yok)",
    "B2": "Trend uzun/kısa (VİOP endeks vadelisi gibi, 1x): SMA200 üstünde uzun; SMA200 altında ve SMA50 < SMA200 ise kısa; diğer durumda boş",
    "B3": "Sadece kısa kol: kapanış < SMA50 < SMA200 iken XU100 kısa (1x), kapanış > SMA20 ise kapat",
}
STRATEJI = list(ADLAR)
ESIK = NormalDist().inv_cdf(1 - 0.05 / (2 * len(STRATEJI)))
KRIPTO_SATIR = ["C0", "C1", "C2", "C2k", "C3", "C3b", "C4", "C0+C3", "C0+C3b", "C0+C4", "C3L", "C3bL", "C1f0", "C2kf0"]
KRIPTO_TABLO = ["C0", "C1", "C2", "C2k", "C3", "C3b", "C4"]
KRIPTO_BIRLESIK = ["C0", "C1", "C2", "C0+C3", "C0+C3b", "C0+C4"]
BIST_SATIR = ["B0", "B1", "B2", "B3", "B0+B3"]
YERINE = {"C1", "C1f0", "B1", "B2"}
KOLU = {"C2": "C2k", "C2k": "C2k", "C3": "C3", "C0+C3": "C3", "C3b": "C3b", "C0+C3b": "C3b", "C4": "C4", "C0+C4": "C4",
        "C3L": "C3L", "C3bL": "C3bL", "C2kf0": "C2kf0", "B3": "B3", "B0+B3": "B3"}
HUKUM = {"kripto": [("C1", "C1", "C1"), ("C2", "C2k", "C2"), ("C3", "C3", "C0+C3"), ("C3b", "C3b", "C0+C3b"),
                    ("C4", "C4", "C0+C4")],
         "bist_tl": [("B1", "B1", "B1"), ("B2", "B2", "B2"), ("B3", "B3", "B0+B3")],
         "bist_usd": [("B1", "B1", "B1"), ("B2", "B2", "B2"), ("B3", "B3", "B0+B3")]}
TABAN = {"kripto": "C0", "bist_tl": "B0", "bist_usd": "B0"}
DONEM_AD = {"egitim": "Eğitim", "test": "Test", "son_12_ay": "Son 12 ay", "pencere": "13 Kasım 2026 kapanışından beri"}
PARA_AD = {"kripto": "KRİPTO (BTC/ETH, $)", "bist_tl": "BIST (XU100, TL)", "bist_usd": "BIST (XU100, dolar bazında)"}
PARA_KISA = {"kripto": "kripto", "bist_tl": "BIST TL", "bist_usd": "BIST $"}
BIRIM = {"kripto": "$", "bist_tl": "TL", "bist_usd": "$"}
SATIR_AD = {"C2k": "C2 kol", "C3L": "C3 lim", "C3bL": "C3b lim", "C1f0": "C1 fon0", "C2kf0": "C2k fon0"}


def indir(sembol, ilk, bugun):
    hata = None
    for host in ("query1", "query2", "query1"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sembol}",
                             params={"period1": int(ilk.timestamp()), "period2": int(time.time()), "interval": "1d"},
                             headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            j = r.json()["chart"]["result"][0]
            if j["meta"].get("dataGranularity") != "1d":
                raise ValueError(f"mum {j['meta'].get('dataGranularity')}")
            q = j["indicators"]["quote"][0]
            d = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close")},
                             index=pd.to_datetime(j["timestamp"], unit="s").normalize(), dtype=float)
            d = d.dropna(subset=["close"])
            d = d[~d.index.duplicated(keep="last")]
            return d[(d.index < bugun) & (d["close"] > 0)]
        except Exception as e:
            hata = e
            time.sleep(3)
    raise SystemExit(f"{sembol} indirilemedi: {hata}")


def olcek_duzelt(c):
    c = c.copy()
    r = c / c.shift(1)
    bulunan = []
    for d, v in r[(r < 0.05) | (r > 20)].items():
        k = 10.0 ** round(np.log10(v))
        c[c.index < d] = c[c.index < d] * k
        bulunan.append({"tarih": str(d.date()), "carpan": k})
    return c, bulunan


def kripto_veri(bugun):
    ham = {k: indir(f"{k}-USD", KRIPTO_ILK, bugun) for k in ("BTC", "ETH")}
    son = min(v.index[-1] for v in ham.values())
    takvim = pd.date_range(ham["BTC"].index[0], son, freq="D")
    P = pd.DataFrame({k: v["close"].reindex(takvim).ffill() for k, v in ham.items()})
    b = ham["BTC"].reindex(takvim)
    c = P["BTC"]
    o = b["open"].fillna(c)
    B = pd.DataFrame({"o": o, "h": pd.concat([b["high"].fillna(c), o, c], axis=1).max(axis=1),
                      "l": pd.concat([b["low"].fillna(c), o, c], axis=1).min(axis=1)})
    bilgi = {}
    for k, v in ham.items():
        g = takvim[takvim >= v.index[0]]
        bilgi[k] = {"sembol": f"{k}-USD", "ilk": str(v.index[0].date()), "son": str(v.index[-1].date()), "mum": len(v),
                    "eksik_gun": int(len(g) - v["close"].reindex(g).notna().sum())}
    return P, B, bilgi


def bist_veri(bugun):
    h100 = indir("XU100.IS", BIST_ILK, bugun)["close"]
    h30 = indir("XU030.IS", BIST_ILK, bugun)["close"]
    usd = indir("USDTRY=X", BIST_ILK, bugun)["close"]
    x100, s100 = olcek_duzelt(h100)
    x30, s30 = olcek_duzelt(h30)
    idx = x100.index.union(x30.index)
    a, b = x100.reindex(idx), x30.reindex(idx)
    ra, rb = a / a.shift(1), b / b.shift(1)
    dol = ra.isna() & rb.notna()
    dol.iloc[0] = False
    eksik = a.isna() & b.notna()
    if dol.any():
        k = ra.where(~dol, rb).fillna(1.0).cumprod()
        x = k * (x100.iloc[-1] / k.loc[x100.index[-1]])
    else:
        x = x100
    u = usd.reindex(x.index.union(usd.index)).ffill().reindex(x.index)
    if u.isna().any():
        raise SystemExit("USDTRY eksik")
    bilgi = {"XU100": {"sembol": "XU100.IS", "ilk": str(h100.index[0].date()), "son": str(h100.index[-1].date()), "mum": len(h100),
                       "olcek_duzeltme": s100},
             "XU030": {"sembol": "XU030.IS", "ilk": str(h30.index[0].date()), "son": str(h30.index[-1].date()), "mum": len(h30),
                       "olcek_duzeltme": s30, "xu100_eksik_gun_dolduruldu": [str(d.date()) for d in eksik[eksik].index]},
             "USDTRY": {"sembol": "USDTRY=X", "ilk": str(usd.index[0].date()), "son": str(usd.index[-1].date()), "mum": len(usd)}}
    return x, u, bilgi


def sma(c, n):
    return c.rolling(n).mean()


def oyn(c, n, yil):
    return c.pct_change(fill_method=None).rolling(n).std() * np.sqrt(yil)


def rsi(c, n):
    d = c.diff()
    yuk = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dus = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + yuk / dus)


def kripto_sinyal(P):
    G = {}
    for k in ("BTC", "ETH"):
        c = P[k]
        s = {n: sma(c, n) for n in (5, 20, 50, 100, 200)}
        on = sum((c > s[n]).astype(float) for n in (20, 50, 100, 200)) / 4
        tam = pd.concat([s[n] for n in (20, 50, 100, 200)], axis=1).notna().all(axis=1)
        alt = tam & (c < s[20]) & (c < s[50]) & (c < s[100]) & (c < s[200])
        olcek = (VT / oyn(c, 30, 365)).clip(upper=1.0)
        w0 = (0.5 * on * olcek).fillna(0.0)
        G[k] = {"w0": w0, "w1": w0.where(~alt, -0.5 * olcek).fillna(0.0), "s": s,
                "gir": (c < s[50]) & (s[50] < s[200]) & (c <= s[20]), "cik": c > s[20]}
    c, s = P["BTC"], G["BTC"]["s"]
    v20 = oyn(c, 20, 365)
    G["ust"] = c > s[200]
    G["yatay"] = ((c / s[50] - 1).abs() < 0.05) & (v20 < v20.rolling(365).median())
    G["dip_gir"] = (c < s[200]) & (rsi(c, 2) < 5)
    G["dip_cik"] = c > s[5]
    return G


def bist_sinyal(x):
    s20, s50, s200 = sma(x, 20), sma(x, 50), sma(x, 200)
    ust = x > s200
    v20 = oyn(x, 20, 252)
    return {"w0": pd.Series(1.0, index=x.index), "w1": ust.astype(float),
            "w2": pd.Series(np.where(ust, 1.0, np.where((x < s200) & (s50 < s200), -1.0, 0.0)), index=x.index),
            "gir": (x < s50) & (s50 < s200) & (x <= s20), "cik": x > s20, "ust": ust, "s50": s50, "s200": s200,
            "yatay": ((x / s50 - 1).abs() < 0.05) & (v20 < v20.rolling(250).median())}


def dizi(s, t):
    return s.reindex(t, fill_value=False).to_numpy(dtype=bool)


def bitir(x, cikis, neden, **ek):
    o = {"varlik": x["varlik"], "yon": x["yon"], "giris": x["giris"], "cikis": cikis, "pay": float(x["pay"]),
         "kar": float(x["kar"]), "yuzde": float(100 * x["kar"] / x["pay"]), "neden": neden}
    if "fiyat" in x:
        o["giris_fiyat"] = float(x["fiyat"])
    o.update(ek)
    return o


def maliyet(eski, yeni, f, mu, mk):
    if eski * yeni > 0:
        d = abs(yeni) - abs(eski)
        m = (mu if yeni > 0 else mk) * f
        return (-d * m, 0.0) if d < 0 else (0.0, d * m)
    return abs(eski) * f * (mu if eski > 0 else mk), abs(yeni) * f * (mu if yeni > 0 else mk)


def agirlik_sim(W, F, a, b, mu, mk, fon, gunluk):
    t = F.index
    i0, i1 = t.get_loc(a), t.get_loc(b)
    V = list(W.columns)
    w = W[V].shift(1).fillna(0.0).to_numpy()
    p = F[V].to_numpy()
    if np.isnan(p[i0 - 1:i1 + 1]).any():
        raise SystemExit(f"{a.date()}-{b.date()} fiyat eksik")
    n = len(V)
    q = np.zeros(n)
    E = KASA
    ozk, uz, ks, islemler = [KASA], [], [], []
    ep = [None] * n
    for i in range(i0, i1 + 1):
        d = q * (p[i] - p[i - 1]) - fon * np.maximum(-q, 0.0) * p[i - 1]
        E += d.sum()
        for j in range(n):
            if ep[j] is not None:
                ep[j]["kar"] += d[j]
        h = w[i] if i < i1 else np.zeros(n)
        tut = np.zeros(n, dtype=bool) if gunluk or i in (i0, i1) else w[i] == w[i - 1]
        Es = E
        for _ in range(4):
            y = np.where(tut, q, h * Es / p[i])
            Es = E - sum(sum(maliyet(q[j], y[j], p[i, j], mu, mk)) for j in range(n))
        y = np.where(tut, q, h * Es / p[i])
        E0 = E
        for j in range(n):
            if y[j] == q[j]:
                continue
            km, am = maliyet(q[j], y[j], p[i, j], mu, mk)
            E -= km + am
            if ep[j] is not None:
                ep[j]["kar"] -= km
                if y[j] == 0 or q[j] * y[j] < 0:
                    islemler.append(bitir(ep[j], t[i], "sinyal" if i < i1 else "dönem sonu (açık)"))
                    ep[j] = None
                else:
                    ep[j]["kar"] -= am
            if ep[j] is None and y[j] != 0:
                ep[j] = {"varlik": V[j], "yon": "uzun" if y[j] > 0 else "kısa", "giris": t[i], "pay": E0, "kar": -am}
        q = y
        ozk.append(E)
        uz.append(float((np.maximum(q, 0) * p[i]).sum() / E))
        ks.append(float((np.maximum(-q, 0) * p[i]).sum() / E))
    return {"ozk": pd.Series(ozk, index=t[i0 - 1:i1 + 1]), "islem": islemler, "uzun": np.array(uz), "kisa": np.array(ks)}


def kisa_kol(F, sinyal, a, b, pay, mal, fon, stop):
    t = F.index
    i0, i1 = t.get_loc(a), t.get_loc(b)
    V = list(sinyal)
    p = {k: F[k].to_numpy() for k in V}
    gir = {k: dizi(sinyal[k][0], t) for k in V}
    cik = {k: dizi(sinyal[k][1], t) for k in V}
    E = KASA
    acik, plan, islemler, ozk, ks = {}, {}, [], [KASA], []
    for i in range(i0 - 1, i1 + 1):
        if i >= i0:
            for k, x in acik.items():
                d = x["q"] * (p[k][i] - p[k][i - 1]) - fon * abs(x["q"]) * p[k][i - 1]
                E += d
                x["kar"] += d
            kapanacak = {k: v for k, v in plan.items() if v != "ac"}
            if i == i1:
                kapanacak = {k: kapanacak.get(k, "dönem sonu (açık)") for k in acik}
            for k, neden in kapanacak.items():
                x = acik.pop(k)
                m = abs(x["q"]) * p[k][i] * mal
                E -= m
                x["kar"] -= m
                islemler.append(bitir(x, t[i], neden, cikis_fiyat=float(p[k][i])))
            if i < i1:
                for k in [k for k, v in plan.items() if v == "ac"]:
                    tutar = pay * E
                    m = tutar * mal
                    E -= m
                    acik[k] = {"varlik": k, "yon": "kısa", "giris": t[i], "fiyat": p[k][i], "pay": tutar,
                               "q": -tutar / p[k][i], "kar": -m}
        plan = {}
        if i < i1:
            for k in V:
                if k in acik:
                    if cik[k][i]:
                        plan[k] = "SMA20 üstü"
                    elif stop is not None and p[k][i] >= (1 + stop) * acik[k]["fiyat"]:
                        plan[k] = f"stop +%{100 * stop:.0f}"
                elif gir[k][i]:
                    plan[k] = "ac"
        if i >= i0:
            ozk.append(E)
            ks.append(sum(abs(x["q"]) * p[k][i] for k, x in acik.items()) / E)
    return {"ozk": pd.Series(ozk, index=t[i0 - 1:i1 + 1]), "islem": islemler, "uzun": np.zeros(len(ks)), "kisa": np.array(ks)}


def izgara(C, B, yatay, a, b, filtre, limit):
    t = C.index
    i0, i1 = t.get_loc(a), t.get_loc(b)
    c, o, h, l = C.to_numpy(), B["o"].to_numpy(), B["h"].to_numpy(), B["l"].to_numpy()
    y = dizi(yatay, t)
    E = KASA
    lotlar, islemler, ozk, uz = [], [], [KASA], []
    ref, ref_i, ref_no = None, 0, 0
    hepsi, al, sat = False, [], set()
    for i in range(i0 - 1, i1 + 1):
        if i >= i0:
            Ep = E
            kalan = []
            for x in lotlar:
                if hepsi:
                    s, neden = c[i], "yatay bitti"
                elif limit and h[i] >= x["hedef"]:
                    s, neden = max(x["hedef"], o[i]), "hedef +%2"
                elif not limit and id(x) in sat:
                    s, neden = c[i], "hedef +%2"
                else:
                    s, neden = None, None
                if s is None:
                    d = x["q"] * (c[i] - c[i - 1])
                    x["kar"] += d
                    E += d
                    kalan.append(x)
                else:
                    d = x["q"] * (s - c[i - 1]) - x["q"] * s * SPOT
                    x["kar"] += d
                    E += d
                    islemler.append(bitir(x, t[i], neden, cikis_fiyat=float(s)))
            for k, sv in al:
                f = (min(sv, o[i]) if l[i] <= sv else None) if limit else c[i]
                if f is None:
                    continue
                q = 0.05 * Ep / f
                d = q * (c[i] - f) - q * f * SPOT
                E += d
                kalan.append({"varlik": "BTC", "yon": "uzun", "giris": t[i], "fiyat": float(f), "pay": q * f, "q": q,
                              "hedef": 1.02 * f, "k": k, "ref_no": ref_no, "kar": d})
            lotlar = kalan
        hepsi, al, sat = False, [], set()
        if i == i1:
            for x in lotlar:
                m = x["q"] * c[i] * SPOT
                x["kar"] -= m
                E -= m
                islemler.append(bitir(x, t[i], "dönem sonu (açık)", cikis_fiyat=float(c[i])))
            lotlar = []
        elif filtre and not y[i]:
            hepsi = bool(lotlar)
            ref = None
        else:
            if ref is None or i - ref_i >= 7:
                ref, ref_i, ref_no = c[i], i, ref_no + 1
            if not limit:
                sat = {id(x) for x in lotlar if c[i] >= x["hedef"]}
            dolu = {x["k"] for x in lotlar if x["ref_no"] == ref_no}
            bos = 5 - len(lotlar) + len(sat)
            for k in range(1, 6):
                sv = ref * (1 - 0.02 * k)
                if bos > 0 and k not in dolu and (limit or c[i] <= sv):
                    al.append((k, sv))
                    bos -= 1
        if i >= i0:
            ozk.append(E)
            uz.append(sum(x["q"] for x in lotlar) * c[i] / E)
    return {"ozk": pd.Series(ozk, index=t[i0 - 1:i1 + 1]), "islem": islemler, "uzun": np.array(uz), "kisa": np.zeros(len(uz))}


def dip_al(C, gir, cik, a, b):
    t = C.index
    i0, i1 = t.get_loc(a), t.get_loc(b)
    c, g, k_ = C.to_numpy(), dizi(gir, t), dizi(cik, t)
    E = KASA
    x, giris, cikis = None, False, None
    islemler, ozk, uz = [], [KASA], []
    for i in range(i0 - 1, i1 + 1):
        if i >= i0:
            if x is not None:
                d = x["q"] * (c[i] - c[i - 1])
                x["kar"] += d
                E += d
                if cikis or i - x["i"] >= 5 or i == i1:
                    m = x["q"] * c[i] * SPOT
                    x["kar"] -= m
                    E -= m
                    neden = cikis or ("5 gün" if i - x["i"] >= 5 else "dönem sonu (açık)")
                    islemler.append(bitir(x, t[i], neden, cikis_fiyat=float(c[i])))
                    x = None
            if giris and i < i1:
                tutar = 0.2 * E
                m = tutar * SPOT
                E -= m
                x = {"varlik": "BTC", "yon": "uzun", "giris": t[i], "i": i, "fiyat": float(c[i]), "pay": tutar,
                     "q": tutar / c[i], "kar": -m}
        giris, cikis = False, None
        if i < i1:
            if x is not None:
                if c[i] <= 0.92 * x["fiyat"]:
                    cikis = "stop -%8"
                elif k_[i]:
                    cikis = "SMA5 üstü"
            elif g[i]:
                giris = True
        if i >= i0:
            ozk.append(E)
            uz.append(0.0 if x is None else x["q"] * c[i] / E)
    return {"ozk": pd.Series(ozk, index=t[i0 - 1:i1 + 1]), "islem": islemler, "uzun": np.array(uz), "kisa": np.zeros(len(uz))}


def katman(taban, kol):
    r = taban["ozk"].pct_change().fillna(0.0) + kol["ozk"].pct_change().fillna(0.0)
    return {"ozk": KASA * (1 + r).cumprod(), "islem": kol["islem"], "uzun": taban["uzun"] + kol["uzun"],
            "kisa": taban["kisa"] + kol["kisa"]}


def kripto_kos(P, B, G, a, b):
    W0 = pd.DataFrame({k: G[k]["w0"] for k in ("BTC", "ETH")})
    W1 = pd.DataFrame({k: G[k]["w1"] for k in ("BTC", "ETH")})
    S = {k: (G[k]["gir"], G[k]["cik"]) for k in ("BTC", "ETH")}
    K = {"C0": agirlik_sim(W0, P, a, b, SPOT, VADELI, FONLAMA, True),
         "C1": agirlik_sim(W1, P, a, b, SPOT, VADELI, FONLAMA, True),
         "C1f0": agirlik_sim(W1, P, a, b, SPOT, VADELI, 0.0, True),
         "C2k": kisa_kol(P, S, a, b, 0.25, VADELI, FONLAMA, 0.12),
         "C2kf0": kisa_kol(P, S, a, b, 0.25, VADELI, 0.0, 0.12),
         "C3": izgara(P["BTC"], B, G["yatay"], a, b, True, False),
         "C3b": izgara(P["BTC"], B, G["yatay"], a, b, False, False),
         "C3L": izgara(P["BTC"], B, G["yatay"], a, b, True, True),
         "C3bL": izgara(P["BTC"], B, G["yatay"], a, b, False, True),
         "C4": dip_al(P["BTC"], G["dip_gir"], G["dip_cik"], a, b)}
    K["C2"] = katman(K["C0"], K["C2k"])
    for s in ("C3", "C3b", "C4"):
        K[f"C0+{s}"] = katman(K["C0"], K[s])
    return K


def bist_kos(F, G, a, b):
    K = {s: agirlik_sim(pd.DataFrame({"XU100": G[w]}), F, a, b, BMAL, BMAL, 0.0, False)
         for s, w in (("B0", "w0"), ("B1", "w1"), ("B2", "w2"))}
    K["B3"] = kisa_kol(F, {"XU100": (G["gir"], G["cik"])}, a, b, 1.0, BMAL, 0.0, None)
    K["B0+B3"] = katman(K["B0"], K["B3"])
    return K


def getiri(k):
    return k["ozk"].pct_change().dropna()


def fark_seri(s, K, taban):
    if s in YERINE:
        return getiri(K[s]) - getiri(K[taban])
    if s in KOLU:
        return getiri(K[KOLU[s]])
    return None


def tstat(x, en_az=30):
    x = np.asarray(x, dtype=float)
    if len(x) < en_az:
        return None
    s = x.std(ddof=1)
    return float(x.mean() / s * np.sqrt(len(x))) if s > 0 else None


def bayrak(s, idx):
    return s.astype(float).shift(1).reindex(idx).fillna(0.0) > 0.5


def zincir(r, m):
    return float(100 * ((1 + r[m]).prod() - 1))


def olc(k, fark, ust, yatay):
    ozk, islemler = k["ozk"], k["islem"]
    r = ozk.pct_change().dropna()
    dE = ozk.diff().dropna()
    gun = (ozk.index[-1] - ozk.index[0]).days
    kat = ozk.iloc[-1] / ozk.iloc[0]
    ay = ozk.iloc[1:].resample("ME").last()
    onceki = ay.shift(1)
    onceki.iloc[0] = ozk.iloc[0]
    ar = ay / onceki - 1
    u, y = bayrak(ust, r.index), bayrak(yatay, r.index)
    kul = k["uzun"] + k["kisa"]
    o = {"bas": str(r.index[0].date()), "son": str(r.index[-1].date()), "gun": len(r), "son_kasa": float(ozk.iloc[-1]),
         "toplam": float(100 * (kat - 1)), "yillik": float(100 * (kat ** (365.25 / gun) - 1)) if gun >= 365 else None,
         "dusus": float(100 * (ozk / ozk.cummax() - 1).min()), "ay_arti": float(100 * (ar > 0).mean()), "ay": len(ar),
         "islem": len(islemler), "kazanma": None, "ort_kar": None, "ort_yuzde": None,
         "t_fark": tstat(fark) if fark is not None else None,
         "g_ust": zincir(r, u), "g_alt": zincir(r, ~u), "g_yatay": zincir(r, y),
         "kar_ust": float(dE[u].sum()), "kar_alt": float(dE[~u].sum()), "kar_yatay": float(dE[y].sum()),
         "gun_ust": int(u.sum()), "gun_alt": int((~u).sum()), "gun_yatay": int(y.sum()),
         "ort_kullanim": float(100 * kul.mean()), "en_cok_kullanim": float(100 * kul.max()),
         "kullanim_100_ustu_gun": int((kul > 1.0 + 1e-9).sum())}
    if islemler:
        kar = np.array([x["kar"] for x in islemler])
        o.update(kazanma=float(100 * (kar > 0).mean()), ort_kar=float(kar.mean()),
                 ort_yuzde=float(np.mean([x["yuzde"] for x in islemler])))
    return o


def kisa_ozet(islemler):
    k = [x for x in islemler if x["yon"] == "kısa"]
    if not k:
        return {"islem": 0, "kazanma": None, "ort_yuzde": None, "kar": 0.0}
    return {"islem": len(k), "kazanma": float(100 * np.mean([x["kar"] > 0 for x in k])),
            "ort_yuzde": float(np.mean([x["yuzde"] for x in k])), "kar": float(sum(x["kar"] for x in k))}


def donemler(t, eb, es, tb):
    son = t[-1]

    def ar(bas, bit):
        return t[t >= bas][0], t[t <= bit][-1]

    return {"egitim": ar(eb, es), "test": ar(tb, son), "son_12_ay": ar(son - pd.Timedelta(days=364), son),
            "pencere": ar(PENCERE, son)}


def hukum(R, pazar, ad, kendi, birlesik):
    tb = TABAN[pazar]
    E, T = R[pazar]["egitim"], R[pazar]["test"]

    def ikili(f):
        return [f(E), f(T)]

    o = {"strateji": ad, "kendi": kendi, "birlesik": birlesik,
         "g_alt": ikili(lambda D: D[kendi]["g_alt"]), "g_yatay": ikili(lambda D: D[kendi]["g_yatay"]),
         "g_ust": ikili(lambda D: D[kendi]["g_ust"]),
         "fark_alt": ikili(lambda D: D[birlesik]["g_alt"] - D[tb]["g_alt"]),
         "fark_yatay": ikili(lambda D: D[birlesik]["g_yatay"] - D[tb]["g_yatay"]),
         "fark_ust": ikili(lambda D: D[birlesik]["g_ust"] - D[tb]["g_ust"]),
         "t": ikili(lambda D: D[birlesik]["t_fark"]),
         "yillik": ikili(lambda D: D[birlesik]["yillik"]), "taban_yillik": ikili(lambda D: D[tb]["yillik"]),
         "dusus": ikili(lambda D: D[birlesik]["dusus"]), "taban_dusus": ikili(lambda D: D[tb]["dusus"])}
    o["dususte_kazanir"] = all(x > 0 for x in o["g_alt"])
    o["yatayda_kazanir"] = all(x > 0 for x in o["g_yatay"])
    o["dususte_tabandan_iyi"] = all(x > 0 for x in o["fark_alt"])
    o["yatayda_tabandan_iyi"] = all(x > 0 for x in o["fark_yatay"])
    o["kanitli"] = all(x is not None and x >= ESIK for x in o["t"])
    o["getiri_iyi"] = all(a > b for a, b in zip(o["yillik"], o["taban_yillik"]))
    o["dusus_iyi"] = all(a > b for a, b in zip(o["dusus"], o["taban_dusus"]))
    return o


def sayi(x, n=1, w=8):
    return f"{'-':>{w}}" if x is None else f"{x:>{w}.{n}f}"


def isaret(x, n=1):
    return "-" if x is None else f"{x:+.{n}f}"


def ad_(s):
    return SATIR_AD.get(s, s)


def et(v, n=1, b="%"):
    return f"eğitim {isaret(v[0], n)}{b} / test {isaret(v[1], n)}{b}"


def yon(v):
    if v[0] > 0 and v[1] > 0:
        return "iki dönemde de kazandı"
    if v[0] <= 0 and v[1] <= 0:
        return "iki dönemde de kaybetti"
    return "eğitimde kazandı, testte kaybetti" if v[0] > 0 else "eğitimde kaybetti, testte kazandı"


def tablo(D, satirlar, birim):
    L = [f"{'':<9}{'Yıllık%':>8}{'Toplam%':>9}{'Düşüş%':>8}{'Ay+%':>6}{'Kull%':>6}{'İşlem':>6}{'Kaz%':>5}"
         f"{'Ort.' + birim:>8}{'Ort.%':>7}{'t(fark)':>8} | {'Rejim%: üst':>12}{'alt':>8}{'yatay':>8}"]
    for s in satirlar:
        o = D[s]
        L.append(f"{ad_(s):<9}{sayi(o['yillik'], 1, 8)}{sayi(o['toplam'], 1, 9)}{sayi(o['dusus'], 1, 8)}{sayi(o['ay_arti'], 0, 6)}"
                 f"{sayi(o['ort_kullanim'], 0, 6)}{o['islem']:>6}{sayi(o['kazanma'], 0, 5)}{sayi(o['ort_kar'], 2, 8)}"
                 f"{sayi(o['ort_yuzde'], 2, 7)}{sayi(o['t_fark'], 2, 8)} | {o['g_ust']:>12.1f}{o['g_alt']:>8.1f}{o['g_yatay']:>8.1f}")
    return L


def birlesik_tablo(Rp, satirlar):
    L = [f"{'':<9}{'Eğitim yıl%':>12}{'düşüş%':>8}{'Test yıl%':>11}{'düşüş%':>8}{'12 ay%':>8}{'düşüş%':>8}"
         f"{'13 Kas%':>9}{'En çok kul%':>12}{'100+ gün':>9}"]
    for s in satirlar:
        e, t, y, p = (Rp[d][s] for d in ("egitim", "test", "son_12_ay", "pencere"))
        L.append(f"{ad_(s):<9}{sayi(e['yillik'], 1, 12)}{sayi(e['dusus'], 1, 8)}{sayi(t['yillik'], 1, 11)}{sayi(t['dusus'], 1, 8)}"
                 f"{sayi(y['toplam'], 1, 8)}{sayi(y['dusus'], 1, 8)}{sayi(p['toplam'], 1, 9)}"
                 f"{sayi(max(e['en_cok_kullanim'], t['en_cok_kullanim']), 0, 12)}"
                 f"{e['kullanim_100_ustu_gun'] + t['kullanim_100_ustu_gun']:>9}")
    return L


def hukum_satiri(h):
    etiket = []
    if h["dususte_kazanir"]:
        etiket.append("DÜŞÜŞTE KAZANIR")
    if h["yatayda_kazanir"]:
        etiket.append("YATAYDA KAZANIR")
    if h["dususte_tabandan_iyi"]:
        etiket.append("düşüşte tabandan iyi")
    if h["yatayda_tabandan_iyi"]:
        etiket.append("yatayda tabandan iyi")
    if h["getiri_iyi"] and h["dusus_iyi"]:
        etiket.append("birleşim getiri ve düşüşü iyileştirir")
    elif h["getiri_iyi"]:
        etiket.append("birleşim getiriyi iyileştirir")
    elif h["dusus_iyi"]:
        etiket.append("birleşim düşüşü iyileştirir")
    etiket.append("KANITLI" if h["kanitli"] else "kanıtsız")
    return [f"{h['strateji']:<4} kendi rejim getirisi: SMA200 altı {et(h['g_alt'])}, yatay {et(h['g_yatay'])}, üstü {et(h['g_ust'])}",
            f"     tabana göre fark (puan): altı {et(h['fark_alt'], 1, '')}, yatay {et(h['fark_yatay'], 1, '')}, "
            f"üstü {et(h['fark_ust'], 1, '')}; t {et(h['t'], 2, '')} (eşik {ESIK:.2f})",
            f"     tabanla birlikte yıllık {et(h['yillik'])} (taban {et(h['taban_yillik'])}), "
            f"en derin düşüş {et(h['dusus'])} (taban {et(h['taban_dusus'])}) → " + ", ".join(etiket)]


def islem_js(x):
    return {k: (str(v.date()) if isinstance(v, pd.Timestamp) else round(v, 4) if isinstance(v, float) else v) for k, v in x.items()}


def yuvarla(o):
    if isinstance(o, dict):
        return {k: yuvarla(v) for k, v in o.items()}
    if isinstance(o, list):
        return [yuvarla(v) for v in o]
    if isinstance(o, float):
        return round(o, 3)
    return o


def aralik(FZ, ad, s, alan):
    v = [o[alan] for o in FZ[ad][s]]
    return f"{min(v):+.1f}..{max(v):+.1f}%"


def faz_satiri(FZ, s):
    eksi = sum(FZ["egitim"][s][k]["g_yatay"] < 0 and FZ["test"][s][k]["g_yatay"] < 0 for k in range(7))
    return (f"{ad_(s)} referans günü (dönem başı 0-6 gün kaydırıldı, referans başka günde yenilenir): yıllık eğitim "
            f"{aralik(FZ, 'egitim', s, 'yillik')}, test {aralik(FZ, 'test', s, 'yillik')}; yatay günler eğitim "
            f"{aralik(FZ, 'egitim', s, 'g_yatay')}, test {aralik(FZ, 'test', s, 'g_yatay')} (7 denemenin {eksi}'inde iki dönemde de eksi)")


def sonuc_metni(R, HK, KISA, PIY, kes, tl_dolar, FZ):
    def hk(p, s):
        return next(h for h in HK[p] if h["strateji"] == s)

    def ET(p, s, alan):
        return [R[p]["egitim"][s][alan], R[p]["test"][s][alan]]

    def oran(v):
        return f"eğitim %{v[0]:.0f} / test %{v[1]:.0f}"

    tum = [(p, h) for p in HUKUM for h in HK[p]]
    dk = [f"{h['strateji']} ({PARA_KISA[p]})" for p, h in tum if h["dususte_kazanir"]]
    yk = [(p, h) for p, h in tum if h["yatayda_kazanir"]]
    kn = [f"{h['strateji']} ({PARA_KISA[p]})" for p, h in tum if h["kanitli"]]
    tmax = max((x, h["strateji"], p) for p, h in tum for x in h["t"] if x is not None)
    iyi = [f"{h['strateji']} ({PARA_KISA[p]})" for p, h in tum if h["getiri_iyi"] and h["dusus_iyi"]]
    c1, c2, c3, c3b, c4 = (hk("kripto", s) for s in ("C1", "C2", "C3", "C3b", "C4"))
    b1t, b2t, b3t = (hk("bist_tl", s) for s in ("B1", "B2", "B3"))
    b1u, b2u, b3u = (hk("bist_usd", s) for s in ("B1", "B2", "B3"))
    s12, pk = R["kripto"]["son_12_ay"], R["kripto"]["pencere"]
    b12, pb = R["bist_tl"]["son_12_ay"], R["bist_tl"]["pencere"]
    M = ["", "=== Sonuç ==="]
    M.append("Önce sonuç: " + ("düşen piyasada (200 günlük ortalamanın altında) hem eğitimde hem testte maliyet sonrası kazandıran: "
                               + ", ".join(dk) if dk else
                               "hayır. Önceden kayıtlı stratejilerin hiçbiri düşen piyasada (BTC ya da XU100 200 günlük ortalamanın "
                               "altındayken) hem eğitimde hem testte maliyet sonrası para kazandırmadı")
             + "; " + ("tabana göre kanıt eşiğini iki dönemde geçen: " + ", ".join(kn) if kn else
                       f"hiçbiri tabana göre kanıt eşiğini (t ≥ {ESIK:.2f}) geçmedi, en yüksek t {tmax[0]:+.2f} ({tmax[1]}, {PARA_KISA[tmax[2]]})") + ".")
    M.append("Yatay günlerde iki dönemde de kazandıran: " + ("; ".join(
        f"{h['strateji']} ({PARA_KISA[p]}, yatay günlerin getirisi {et(h['g_yatay'], 2)}, "
        f"{'kanıtlı' if h['kanitli'] else 'kanıtsız: t ' + et(h['t'], 2, '')})" for p, h in yk) if yk else "yok") + ".")
    f1, f2 = ET("kripto", "C1f0", "g_alt"), ET("kripto", "C2kf0", "g_alt")
    M.append(f"Kripto açığa satış: düşüş günlerinde C1 (çekirdeğe kısa bacak) {et(c1['g_alt'])} (tabana göre {et(c1['fark_alt'], 1, ' puan')}), "
             f"C2 kısa kolu {et(c2['g_alt'])}: C1'in kısa bacağı {yon(c1['fark_alt'])}, C2 kolu {yon(c2['g_alt'])}. Yıllık getiri C1 {et(c1['yillik'])}, "
             f"C2 {et(c2['yillik'])}, taban {et(c1['taban_yillik'])}; en derin düşüş C1 {et(c1['dusus'])}, C2 {et(c2['dusus'])}, "
             f"taban {et(c1['taban_dusus'])}. Fonlama sıfır olsaydı düşüş günlerinde C1 {et(f1)}, C2 kolu {et(f2)}: "
             + ("karar değişmezdi." if not (all(x > 0 for x in f1) or all(x > 0 for x in f2)) else "en az biri iki dönemde de artıya geçerdi (karar fonlama varsayımına bağlı)."))
    M.append(f"Şu anki düşüş: son 12 ayda BTC {PIY[('kripto', 'son_12_ay')]['BTC']:+.1f}%; C0 {s12['C0']['toplam']:+.1f}%, C1 {s12['C1']['toplam']:+.1f}%, "
             f"C2 {s12['C2']['toplam']:+.1f}%. 13 Kasım kapanışından beri BTC {PIY[('kripto', 'pencere')]['BTC']:+.1f}%: C0 {pk['C0']['toplam']:+.1f}% "
             f"(kasanın %{pk['C0']['ort_kullanim']:.0f}'i pozisyonda), C1 {pk['C1']['toplam']:+.1f}%, C2 {pk['C2']['toplam']:+.1f}% ({kes}). "
             "Tek bir düşüş dönemi kanıt değildir"
             + (": aynı C1 kuralı testin tamamında tabanın gerisinde kaldı." if c1["yillik"][1] < c1["taban_yillik"][1] else "."))
    kul3 = max(ET("kripto", "C3", "ort_kullanim"))
    M.append(f"Izgara (yatay piyasa botu): filtreli C3 {'çok az çalışıyor' if kul3 < 2 else 'çalışıyor'} (eğitim "
             f"{R['kripto']['egitim']['C3']['islem']} / test {R['kripto']['test']['C3']['islem']} işlem, kasanın ortalama en çok %{kul3:.1f}'i), "
             f"yıllık {et(ET('kripto', 'C3', 'yillik'))}, yatay günlerde {et(c3['g_yatay'], 2)} ({yon(c3['g_yatay'])}). Hep açık C3b yıllık "
             f"{et(ET('kripto', 'C3b', 'yillik'))}, yatay günlerde {et(c3b['g_yatay'], 2)} ({yon(c3b['g_yatay'])}"
             + ("; kazancı yatay günlerden değil, diğer günlerden" if all(x <= 0 for x in c3b["g_yatay"]) else "")
             + f"); tabana eklenince en derin düşüş {et(c3b['dusus'])} (taban {et(c3b['taban_dusus'])}). Limit emirle (gün içi dolum, duyarlılık): "
             f"C3 {et(ET('kripto', 'C3L', 'yillik'))}, C3b {et(ET('kripto', 'C3bL', 'yillik'))} yıllık, yatay günlerde C3 lim "
             f"{et(ET('kripto', 'C3L', 'g_yatay'), 2)}, C3b lim {et(ET('kripto', 'C3bL', 'g_yatay'), 2)}. {faz_satiri(FZ, 'C3b')}.")
    M.append(f"Düşüşte dip alımı (C4): yıllık {et(ET('kripto', 'C4', 'yillik'))}, düşüş günlerinde {et(c4['g_alt'], 2)} ({yon(c4['g_alt'])}), "
             f"işlem başı {et(ET('kripto', 'C4', 'ort_yuzde'), 2)}, kazanma {oran(ET('kripto', 'C4', 'kazanma'))}.")
    M.append(f"BIST (TL): düşüş günlerinde B2 (uzun/kısa) {et(b2t['g_alt'])} ({yon(b2t['g_alt'])}), B3 (sadece kısa) {et(b3t['g_alt'])} "
             f"({yon(b3t['g_alt'])}); endeks enflasyonla yükseldiği için TL'de kısa pozisyon baştan yenik başlar. B1 (ortalamanın altında nakit) "
             f"yıllık {et(b1t['yillik'])} vs al-tut {et(b1t['taban_yillik'])}, en derin düşüş {et(b1t['dusus'])} vs {et(b1t['taban_dusus'])}"
             + (": ne getiriyi ne düşüşü iyileştirdi." if not (b1t["getiri_iyi"] or b1t["dusus_iyi"]) else "."))
    M.append(f"BIST (dolar bazında): düşüş günlerinde B2 {et(b2u['g_alt'])} ({yon(b2u['g_alt'])}), B3 {et(b3u['g_alt'])} ({yon(b3u['g_alt'])}); "
             f"al-tut yıllık {et(b1u['taban_yillik'])}. Boş TL nakit ayrıca dolar karşısında eğitimde %{100 * (1 - 1 / (1 + tl_dolar['egitim'] / 100)):.0f}, "
             f"testte %{100 * (1 - 1 / (1 + tl_dolar['test'] / 100)):.0f} değer kaybetti.")
    M.append(f"BIST şu an: son 12 ayda XU100 {PIY[('bist_tl', 'son_12_ay')]['XU100']:+.1f}% (TL); B1 {b12['B1']['toplam']:+.1f}%, B2 {b12['B2']['toplam']:+.1f}%, "
             f"B3 {b12['B3']['toplam']:+.1f}%. 13 Kasım'dan beri XU100 {PIY[('bist_tl', 'pencere')]['XU100']:+.1f}%: B1 {pb['B1']['toplam']:+.1f}%, "
             f"B2 {pb['B2']['toplam']:+.1f}%, B3 {pb['B3']['toplam']:+.1f}%.")
    M.append("Tabana eklemek: hem yıllık getiriyi hem en derin düşüşü iki dönemde de iyileştiren birleşim " + (", ".join(iyi) if iyi else "yok") + ".")
    if not dk and not kn:
        M.append(f"Özet: düşüşte para kazandırmanın kanıtlı bir yolu bulunamadı. Taban (C0) düşüş günlerinde {et(ET('kripto', 'C0', 'g_alt'))} "
                 f"({yon(ET('kripto', 'C0', 'g_alt'))}). Düşüş günlerinde kripto açığa satış C1 kısa bacağı {yon(c1['fark_alt'])}, "
                 f"C2 kolu {yon(c2['g_alt'])}; dip alımı (C4) {yon(c4['g_alt'])}; BIST kısa (B3) TL'de {yon(b3t['g_alt'])}, dolarda "
                 f"{yon(b3u['g_alt'])}. Yatay günlerde ızgara C3 {yon(c3['g_yatay'])}, C3b {yon(c3b['g_yatay'])}. Son 12 ayda kısa pozisyonun "
                 f"iyi görünmesi (C1 {s12['C1']['toplam']:+.1f}%, B2 TL {b12['B2']['toplam']:+.1f}%) bu tek düşüşe aittir; aynı kurallar testin "
                 f"tamamında tabanı geçemedi (C1 t {isaret(c1['t'][1], 2)}, B2 TL t {isaret(b2t['t'][1], 2)}).")
    M.append("Sınırlar: işlemler kapanışta (gün içi stop yok; ızgarada limit emir sadece duyarlılık), kısa pozisyon fonlama öder varsayımı "
             "(gerçekte çoğu zaman kısa fonlama alır; sıfır fonlama yukarıda), VİOP vadeli primi ve faiz dahil değil, XU100 temettüsüz, "
             "tek geçmiş yol. Gerçek para sonucu değildir.")
    return M


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    bugun = pd.Timestamp(int(time.time() // 86400 * 86400), unit="s")
    P, B, bilgi_k = kripto_veri(bugun)
    X, U, bilgi_b = bist_veri(bugun)
    GK, GB = kripto_sinyal(P), bist_sinyal(X)
    DK = donemler(P.index, pd.Timestamp("2018-01-01"), pd.Timestamp("2021-12-31"), pd.Timestamp("2022-01-01"))
    DB = donemler(X.index, pd.Timestamp("2010-01-01"), pd.Timestamp("2019-12-31"), pd.Timestamp("2020-01-01"))
    D = {"kripto": DK, "bist_tl": DB, "bist_usd": DB}
    FB = {"bist_tl": pd.DataFrame({"XU100": X}), "bist_usd": pd.DataFrame({"XU100": X / U})}
    R, K = {}, {}
    for ad, (a, b) in DK.items():
        k = kripto_kos(P, B, GK, a, b)
        K[("kripto", ad)] = k
        R.setdefault("kripto", {})[ad] = {s: olc(k[s], fark_seri(s, k, "C0"), GK["ust"], GK["yatay"]) for s in KRIPTO_SATIR}
    for para, F in FB.items():
        for ad, (a, b) in DB.items():
            k = bist_kos(F, GB, a, b)
            K[(para, ad)] = k
            R.setdefault(para, {})[ad] = {s: olc(k[s], fark_seri(s, k, "B0"), GB["ust"], GB["yatay"]) for s in BIST_SATIR}
    HK = {p: [hukum(R, p, *x) for x in HUKUM[p]] for p in HUKUM}
    KISA = {p: {ad: {s: kisa_ozet(K[(p, ad)][s]["islem"]) for s in (("C1", "C2k") if p == "kripto" else ("B2", "B3"))}
                for ad in ("egitim", "test")} for p in HUKUM}
    PIY = {}
    for p, Dp in D.items():
        for ad, (a, b) in Dp.items():
            if p == "kripto":
                PIY[(p, ad)] = {k: float(100 * (P.at[b, k] / P.at[a, k] - 1)) for k in ("BTC", "ETH")}
            else:
                F = FB[p]["XU100"]
                PIY[(p, ad)] = {"XU100": float(100 * (F.at[b] / F.at[a] - 1))}
        PIY[(p, "rejim")] = {ad: {"ust": R[p][ad][TABAN[p]]["gun_ust"], "alt": R[p][ad][TABAN[p]]["gun_alt"],
                                  "yatay": R[p][ad][TABAN[p]]["gun_yatay"]} for ad in Dp}
    sk, sb = P.index[-1], X.index[-1]
    tl_dolar = {ad: float(100 * (U.at[b] / U.at[a] - 1)) for ad, (a, b) in DB.items()}
    ort = {k: (float(GK[k]["s"][50].iloc[-1]), float(GK[k]["s"][200].iloc[-1])) for k in ("BTC", "ETH")}
    ort["XU100"] = (float(GB["s50"].iloc[-1]), float(GB["s200"].iloc[-1]))
    kes = (("C2 girmedi: " if R["kripto"]["pencere"]["C2k"]["islem"] == 0 else f"C2 {R['kripto']['pencere']['C2k']['islem']} işlem; ")
           + ", ".join(f"{k} 50 günlük ortalama {v[0]:.0f} {'>' if v[0] > v[1] else '<'} 200 günlük {v[1]:.0f}"
                       for k, v in ort.items() if k != "XU100"))
    FZ = {ad: {s: [olc(izgara(P["BTC"], B, GK["yatay"], a + pd.Timedelta(days=k), b, False, s == "C3bL"), None, GK["ust"], GK["yatay"])
                   for k in range(7)] for s in ("C3b", "C3bL")} for ad, (a, b) in DK.items() if ad in ("egitim", "test")}
    dolan = bilgi_b["XU030"]["xu100_eksik_gun_dolduruldu"]

    L = [f"DÜŞÜŞ TESTİ: önceden kayıtlı geçmiş test (çalıştırma {time.strftime('%Y-%m-%d')}, son kapanış kripto {sk.date()}, BIST {sb.date()})",
         "Soru: botlarımız piyasa düşerken ya da yatay giderken para kazanabilir mi?",
         "",
         "Veri    : Yahoo günlük kapanış (period1/period2; range=max günlük değil aylık mum veriyor), bugünün bitmemiş mumu atıldı.",
         "          " + ", ".join(f"{v['sembol']} {v['ilk']}..{v['son']} ({v['mum']} mum, eksik gün {v['eksik_gun']})" for v in bilgi_k.values()) + ".",
         "          " + ", ".join(f"{v['sembol']} {v['ilk']}..{v['son']} ({v['mum']} mum)" for v in bilgi_b.values()) + ".",
         f"          XU100 2020 sıfır atma (÷100) sıçraması: "
         + (f"düzeltildi {bilgi_b['XU100']['olcek_duzeltme']}" if bilgi_b['XU100']['olcek_duzeltme'] else "Yahoo serisinde yok (eski değerler zaten ÷100)")
         + f"; XU100'de kapanışı boş olup XU030'da olan {len(dolan)} işlem günü XU030 getirisiyle dolduruldu"
         + (f" ({', '.join(dolan)})" if dolan else "") + ". USDTRY BIST günlerine son bilinen değerle hizalandı.",
         "Kurallar: sinyal kapanışta, o kapanışa kadarki veriyle; işlem BİR SONRAKİ kapanışta (stop ve hedefler de kapanışla ölçülür, ertesi",
         "          kapanışta uygulanır; sadece C4'ün '5 gün' çıkışı o kapanışta). Maliyet tek yön: kripto spot %0.15, vadeli %0.08 + fonlama",
         "          günde %0.03 (8 saatte %0.01, yılda ~%11; kısa pozisyon öder varsayımı, temkinli); BIST %0.30 (VİOP dahil, fonlama/baz yok).",
         "          Boş para getirisiz (USDT / TL; faiz yok). Her dönem 1000 nakitle ayrı koşu: dönemden önceki kapanışın sinyali dönemin ilk",
         "          kapanışında işlenir; dönem sonunda açık pozisyon maliyetle kapanır. C0 ve C1 her gün hedef ağırlığa dengelenir (uygulama gibi);",
         "          B0-B2 sadece hedef değişince işlem yapar (sabit adet); C2/C3/C4/B3 tek tek işlemdir.",
         "          Giriş, kapatma koşulu (kapanış > SMA20) zaten doğruysa açılmaz (C2, B3; aksi halde ertesi gün kapanan boş işlem olurdu).",
         "          C1'de kısa için dört ortalamanın da hesaplanmış olması gerekir (ETH SMA200 2018-05 sonundan itibaren).",
         "          BIST dolar görünümü: aynı sinyaller (TL endeksinden), fiyat XU100/USDTRY, boş para dolar. TL'de kısa pozisyon enflasyon",
         "          yüzünden baştan yenik başlar; gerçek VİOP kısası vadeli primini (≈ TL faizi) de kazanırdı, faiz olduğu için dahil edilmedi.",
         "          XU100 fiyat endeksi (temettüsüz).",
         "Rejim   : her günün getirisi bir önceki kapanıştaki duruma göre ayrılır. Kripto: BTC 200 günlük ortalamanın üstü/altı; BIST: XU100 (TL)",
         "          üstü/altı. 'Yatay' ek ayrım (C3'ün tanımı): |kapanış/SMA50 - 1| < %5 ve 20 günlük oynaklık 1 yıllık medyanının altında (BTC / XU100).",
         "          Rejim% = sadece o günlerin günlük getirileri zincirlenince (kasa büyüklüğünden bağımsız). Tutarlar JSON'da (kar_ust/alt/yatay).",
         f"Çoklu test: {len(STRATEJI)} önceden kayıtlı strateji (C0-C4, C3b, B0-B3) → Bonferroni eşiği |t| ≥ {ESIK:.2f} (çift yönlü %5/{len(STRATEJI)}).",
         "          t(fark) = tabana göre günlük fark: C1/B1/B2 tabanın yerine (strateji - taban); kollar (C2, C3, C3b, C4, B3) tabanın üstüne",
         "          eklenir, fark = kolun kendi günlük getirisi (taban + kol birleşiminin tabandan farkı). En az 30 gün.",
         "Karar (önceden): 'düşüşte kazanır' = SMA200 altı günlerde kendi getirisi hem eğitimde hem testte > 0 (maliyet sonrası; kollarda kolun",
         "          kendisi); 'yatayda kazanır' aynı, yatay günlerde; 'kanıtlı' = ayrıca t(fark) ≥ eşik iki dönemde de. Diğer rejimdeki kayıp ve",
         "          tabanla birleşimin yıllık getiri / en derin düşüşe etkisi yanında verilir.",
         "Tablo   : Kull% = kasanın ortalama ne kadarı pozisyonda (uzun + kısa). İşlem = gerçek işlem; C0/C1/B0-B2'de pozisyonun sıfırdan",
         "          açılıp sıfıra (ya da ters yöne) döndüğü dönem, % = başlangıç kasasına göre; diğerlerinde % = işlem tutarına göre.",
         "          Ay+% = kârlı ay oranı. C2 kol = C2'nin kısa kolu tek başına (C2 ve C2 kol işlemleri aynıdır).",
         ""]
    L += [f"{s:<4} {ADLAR[s]}" for s in STRATEJI]
    for p in ("kripto", "bist_tl", "bist_usd"):
        L += ["", f"################ {PARA_AD[p]} ################"]
        for ad, (a, b) in D[p].items():
            pi = PIY[(p, ad)]
            rj = PIY[(p, "rejim")][ad]
            L += ["", f"=== {DONEM_AD[ad]} {a.date()} → {b.date()} ({R[p][ad][TABAN[p]]['gun']} gün; "
                  + ", ".join(f"{k} {v:+.1f}%" for k, v in pi.items())
                  + f"; gün: SMA200 üstü {rj['ust']}, altı {rj['alt']}, yatay {rj['yatay']}) ==="]
            L += tablo(R[p][ad], KRIPTO_TABLO if p == "kripto" else BIST_SATIR[:4], BIRIM[p])
            if ad == "pencere":
                L.append("  (1 yıldan kısa dönemde yıllık, 30 günden kısa dönemde t gösterilmez)")
                if p == "kripto":
                    L.append(f"  {kes}; C0 kasanın %{R[p][ad]['C0']['ort_kullanim']:.0f}'i ile pozisyonda.")
        if p == "kripto":
            L += ["", "=== Tabana eklemek (kripto): taban C0 ile birleşim, dört dönem (12 ay ve 13 Kas = toplam %) ==="]
            L += birlesik_tablo(R[p], KRIPTO_BIRLESIK)
            L.append("  En çok kul% = eğitim/testte kasanın en çok kullanılan oranı; 100+ gün = %100'ü aştığı gün (spot uzunda borç gerekir).")
            L += ["", "=== Duyarlılık (karara girmez): ızgara limit emirle (gün içi en düşük/en yüksekle dolum) ve kısa fonlama sıfır ==="]
            L += tablo(R[p]["egitim"], [], "$")
            for ad in ("egitim", "test"):
                L += [f"{DONEM_AD[ad]}:"] + tablo(R[p][ad], ["C3", "C3L", "C3b", "C3bL", "C1", "C1f0", "C2k", "C2kf0"], "$")[1:]
            L += [faz_satiri(FZ, s) for s in ("C3b", "C3bL")]
        else:
            L += ["", f"=== Tabana eklemek ({PARA_AD[p]}) ==="]
            L += birlesik_tablo(R[p], BIST_SATIR)
            L.append("  Kısa VİOP'ta teminat tutarın altındadır: %100 üstü kullanım kısa pozisyondan gelir ve borç gerektirmez.")
    L += ["", "=== Düşen / yatay piyasada para kazanıyor mu? (maliyet sonrası) ==="]
    for p in ("kripto", "bist_tl", "bist_usd"):
        L.append(f"--- {PARA_AD[p]} ---")
        for h in HK[p]:
            L += hukum_satiri(h)
        for ad in ("egitim", "test"):
            L.append(f"     {DONEM_AD[ad]} kısa işlemleri: " + "; ".join(
                f"{ad_(s)} {v['islem']} işlem, kazanma {sayi(v['kazanma'], 0, 0)}%, ort {isaret(v['ort_yuzde'], 2)}%, toplam {v['kar']:+.0f} {BIRIM[p]}"
                for s, v in KISA[p][ad].items()))
    L += ["", "USDTRY artışı (boş TL nakit tutan biri için dolar bazında ek kayıp): "
          + ", ".join(f"{DONEM_AD[ad]} {v:+.1f}%" for ad, v in tl_dolar.items()) + "."]
    M = sonuc_metni(R, HK, KISA, PIY, kes, tl_dolar, FZ)
    L += M
    metin = "\n".join(L) + "\n"
    os.makedirs(CIKTI, exist_ok=True)
    with open(os.path.join(CIKTI, "dusus_testi.txt"), "w", encoding="utf-8") as fh:
        fh.write(metin)
    js = {"calistirma": time.strftime("%Y-%m-%d %H:%M"), "son_kapanis": {"kripto": str(sk.date()), "bist": str(sb.date())},
          "veri": {**bilgi_k, **bilgi_b},
          "ayar": {"kasa": KASA, "hedef_oynaklik": VT, "maliyet_tek_yon": {"kripto_spot": SPOT, "kripto_vadeli": VADELI, "bist": BMAL},
                   "fonlama_gunluk": FONLAMA, "gecikme": 1, "bonferroni_strateji": len(STRATEJI), "bonferroni_esik_t": ESIK},
          "stratejiler": ADLAR,
          "donemler": {p: {k: [str(a.date()), str(b.date())] for k, (a, b) in Dp.items()} for p, Dp in D.items()},
          "piyasa": {f"{p}|{ad}": v for (p, ad), v in PIY.items()},
          "ortalamalar_son": {k: {"sma50": v[0], "sma200": v[1]} for k, v in ort.items()},
          "tl_dolar_kaybi": tl_dolar,
          "sonuc": R, "hukum": HK, "kisa_islemler": KISA,
          "izgara_referans_gunu": {ad: {s: [{k: o[k] for k in ("yillik", "dusus", "islem", "g_ust", "g_alt", "g_yatay")} for o in v]
                                        for s, v in d.items()} for ad, d in FZ.items()},
          "islemler_pencere": {p: {s: [islem_js(x) for x in K[(p, "pencere")][s]["islem"]]
                                   for s in (KRIPTO_TABLO if p == "kripto" else BIST_SATIR[:4])} for p in HUKUM},
          "islemler_test": {p: {s: [islem_js(x) for x in K[(p, "test")][s]["islem"]]
                                for s in (("C1", "C2k", "C4") if p == "kripto" else ("B2", "B3"))} for p in HUKUM},
          "sonuc_metni": M[2:]}
    with open(os.path.join(CIKTI, "dusus_testi.json"), "w", encoding="utf-8") as fh:
        json.dump(yuvarla(js), fh, ensure_ascii=False, indent=1)
    print(metin)


if __name__ == "__main__":
    main()
