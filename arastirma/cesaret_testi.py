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
SEMBOL = {"BTC": "BTC-USD", "ETH": "ETH-USD", "ALTIN": "GC=F"}
MALIYET = {"BTC": 0.0015, "ETH": 0.0015, "ALTIN": 0.001}
KASA = 1000.0
VT = 0.40
ILK = pd.Timestamp("2017-01-01")
EGITIM_BAS, EGITIM_SON = pd.Timestamp("2018-01-01"), pd.Timestamp("2021-12-31")
TEST_BAS = pd.Timestamp("2022-01-01")
EKIM = pd.Timestamp("2026-10-01")
KASIM = pd.Timestamp("2026-11-01")
GUN = pd.Timedelta(days=1)
ADLAR = {"S0": "BTC al-tut",
         "S1": "Trend çekirdek (uygulama gibi): coin başına 0.5 × SMA20/50/100/200 üstünde kalma oranı × min(1, 0.40/30g oynaklık)",
         "S2": "S1 + boştaki para altına (altın > 200 günlük ortalaması ise, değilse USDT)",
         "S3": "Hızlı trend: S1 ama sadece SMA20/50 (daha erken geri girer)",
         "S4a": "Kartopu: SMA20 yukarı kırılımında uzun, 50 $ + her kazançta 25 $ (en çok 200 $), kayıpta 50 $",
         "S4b": "S4a sinyali, pay = max(50 $, kasanın %5'i)",
         "S5": "Kartopu dip alımı: SMA100 altında gün ≤ -%5 ise uzun, +%5 / -%8 / 7 gün çıkış, pay S4a gibi"}
STRATEJI = list(ADLAR)
CESUR = ["S0", "S3", "S4a", "S4b", "S5"]
TUR = {"S4a": ("S4", "kartopu"), "S4b": ("S4", "oran"), "S5": ("S5", "kartopu")}
ESIK = NormalDist().inv_cdf(1 - 0.05 / (2 * len(STRATEJI)))
DONEM_AD = {"egitim": "Eğitim", "test": "Test", "son_12_ay": "Son 12 ay", "ekim_kasim_2026": "Ekim-Kasım 2026"}


def indir(sembol, bugun):
    hata = None
    for host in ("query1", "query2", "query1"):
        try:
            r = requests.get(f"https://{host}.finance.yahoo.com/v8/finance/chart/{sembol}",
                             params={"period1": int(ILK.timestamp()), "period2": int(time.time()), "interval": "1d"},
                             headers={"User-Agent": UA}, timeout=30)
            r.raise_for_status()
            j = r.json()["chart"]["result"][0]
            if j["meta"].get("dataGranularity") != "1d":
                raise ValueError(f"mum {j['meta'].get('dataGranularity')}")
            t = pd.to_datetime(j["timestamp"], unit="s").normalize()
            c = pd.Series(j["indicators"]["quote"][0]["close"], index=t, dtype=float).dropna()
            c = c[~c.index.duplicated(keep="last")]
            return c[(c.index < bugun) & (c > 0)]
        except Exception as e:
            hata = e
            time.sleep(3)
    raise SystemExit(f"{sembol} indirilemedi: {hata}")


def veri():
    bugun = pd.Timestamp(int(time.time() // 86400 * 86400), unit="s")
    ham = {k: indir(s, bugun) for k, s in SEMBOL.items()}
    son = min(ham["BTC"].index[-1], ham["ETH"].index[-1])
    takvim = pd.date_range(ILK, son, freq="D")
    bilgi = {k: {"sembol": SEMBOL[k], "ilk": str(v.index[0].date()), "son": str(v.index[-1].date()), "mum": len(v)}
             for k, v in ham.items()}
    for k in ("BTC", "ETH"):
        g = takvim[takvim >= ham[k].index[0]]
        bilgi[k]["eksik_gun"] = int(len(g) - ham[k].reindex(g).notna().sum())
    P = pd.DataFrame({k: ham[k].reindex(takvim).ffill() for k in ("BTC", "ETH")})
    P["ALTIN"] = ham["ALTIN"].reindex(takvim).ffill()
    a = ham["ALTIN"]
    ust = (a > a.rolling(200).mean()).astype(float).reindex(takvim).ffill().fillna(0.0)
    return P, ust, bilgi


def trend(c, pencere):
    on = sum((c > c.rolling(n).mean()).astype(float) for n in pencere) / len(pencere)
    vol = c.pct_change(fill_method=None).rolling(30).std() * np.sqrt(365)
    return (0.5 * on * (VT / vol).clip(upper=1.0)).fillna(0.0)


def agirliklar(P, ust):
    s1 = pd.DataFrame({k: trend(P[k], (20, 50, 100, 200)) for k in ("BTC", "ETH")})
    s2 = s1.copy()
    s2["ALTIN"] = (1.0 - s1["BTC"] - s1["ETH"]) * ust
    return {"S0": pd.DataFrame({"BTC": 1.0}, index=P.index), "S1": s1, "S2": s2,
            "S3": pd.DataFrame({k: trend(P[k], (20, 50)) for k in ("BTC", "ETH")})}


def islem_sinyalleri(P):
    S = {}
    for k in ("BTC", "ETH"):
        c = P[k]
        s20, s100 = c.rolling(20).mean(), c.rolling(100).mean()
        S[k] = {"c": c.values, "s20": s20.values,
                "S4": ((c > s20) & (c.shift(1) <= s20.shift(1))).values,
                "S5": ((c < s100) & (c / c.shift(1) - 1 <= -0.05)).values}
    return S


def agirlik_sim(W, P, a, b, g):
    t = P.index
    i0, i1 = t.get_loc(a), t.get_loc(b)
    V = list(W.columns)
    w = W.shift(g).fillna(0.0).values
    F = P[V].values
    if np.isnan(F[i0 - 1:i1 + 1]).any():
        raise SystemExit(f"{a.date()}-{b.date()} fiyat eksik")
    m = np.array([MALIYET[v] for v in V])
    adet, kar, basE = np.zeros(len(V)), np.zeros(len(V)), np.zeros(len(V))
    basi = [None] * len(V)
    nakit, ozk, yat, islemler = KASA, [KASA], [], []
    for i in range(i0, i1 + 1):
        p = F[i]
        kar += adet * (p - F[i - 1])
        deger = adet * p
        E = nakit + deger.sum()
        h = w[i] if i < i1 else np.zeros(len(V))
        Es = E
        for _ in range(4):
            Es = E - (np.abs(h * Es - deger) * m).sum()
        yeni = h * Es
        mal = np.abs(yeni - deger) * m
        for j, v in enumerate(V):
            if basi[j] is None and yeni[j] > 0:
                basi[j], basE[j], kar[j] = i, E, 0.0
            if basi[j] is not None:
                kar[j] -= mal[j]
                if yeni[j] == 0:
                    islemler.append({"varlik": v, "giris": t[basi[j]], "cikis": t[i], "pay": float(basE[j]),
                                     "kar": float(kar[j]), "yuzde": float(100 * kar[j] / basE[j]),
                                     "neden": "sinyal" if i < i1 else "dönem sonu (açık)"})
                    basi[j] = None
        nakit = Es - yeni.sum()
        adet = np.where(yeni > 0, yeni / p, 0.0)
        ozk.append(Es)
        yat.append(yeni.sum() / Es)
    return pd.Series(ozk, index=t[i0 - 1:i1 + 1]), islemler, yat


def islem_sim(kural, boyut, S, t, a, b, g):
    i0, i1 = t.get_loc(a), t.get_loc(b)
    d = {"nakit": KASA, "seri": 0}
    acik, bekle, islemler, ozk, yat = {}, {}, [], [KASA], []

    def deger(i):
        return d["nakit"] + sum(x["adet"] * S[k]["c"][i] for k, x in acik.items())

    def ac(k, i):
        pay = min(200.0, 50.0 + 25.0 * d["seri"]) if boyut == "kartopu" else max(50.0, 0.05 * deger(i))
        pay = min(pay, d["nakit"])
        if pay >= 1:
            p = S[k]["c"][i]
            d["nakit"] -= pay
            acik[k] = {"i": i, "fiyat": p, "adet": pay * (1 - MALIYET[k]) / p, "pay": pay, "plan": None, "neden": None}

    def kapat(k, i, neden):
        x = acik.pop(k)
        p = S[k]["c"][i]
        gelir = x["adet"] * p * (1 - MALIYET[k])
        d["nakit"] += gelir
        kar = gelir - x["pay"]
        d["seri"] = d["seri"] + 1 if kar > 0 else 0
        islemler.append({"varlik": k, "giris": t[x["i"]], "cikis": t[i], "giris_fiyat": float(x["fiyat"]),
                         "cikis_fiyat": float(p), "pay": float(x["pay"]), "kar": float(kar),
                         "yuzde": float(100 * kar / x["pay"]), "neden": neden})

    for i in range(i0, i1 + 1):
        for k in list(acik):
            x = acik[k]
            if x["plan"] is not None and x["plan"] <= i:
                kapat(k, i, x["neden"])
            elif kural == "S5" and i - x["i"] >= 7:
                kapat(k, i, "7 gün")
        for k in [k for k, j in bekle.items() if j <= i]:
            del bekle[k]
            ac(k, i)
        for k, x in list(acik.items()):
            if x["plan"] is None:
                c = S[k]["c"][i]
                if kural == "S4":
                    neden = "stop -%5" if c <= 0.95 * x["fiyat"] else "SMA20 altı" if c < S[k]["s20"][i] else None
                else:
                    neden = "stop -%8" if c <= 0.92 * x["fiyat"] else "hedef +%5" if c >= 1.05 * x["fiyat"] else None
                if neden and g == 0:
                    kapat(k, i, neden)
                elif neden:
                    x["plan"], x["neden"] = i + g, neden
        if i < i1:
            for k in ("BTC", "ETH"):
                if k not in acik and k not in bekle and S[k][kural][i]:
                    if g == 0:
                        ac(k, i)
                    else:
                        bekle[k] = i + g
        else:
            for k in list(acik):
                kapat(k, i, "dönem sonu (açık)")
        e = deger(i)
        ozk.append(e)
        yat.append((e - d["nakit"]) / e)
    return pd.Series(ozk, index=t[i0 - 1:i1 + 1]), islemler, yat


def calistir(s, W, S, P, a, b, g):
    if s in W:
        return agirlik_sim(W[s], P, a, b, g)
    return islem_sim(*TUR[s], S, P.index, a, b, g)


def tstat(x, en_az=2):
    x = np.asarray(x, dtype=float)
    if len(x) < max(2, en_az):
        return None
    s = x.std(ddof=1)
    return float(x.mean() / s * np.sqrt(len(x))) if s > 0 else None


def olc(ozk, islemler, yat, ref):
    r = ozk.pct_change().dropna()
    n = len(r)
    k = ozk.iloc[-1] / ozk.iloc[0]
    o = {"bas": str(ozk.index[1].date()), "son": str(ozk.index[-1].date()), "gun": n, "son_kasa": float(ozk.iloc[-1]),
         "toplam": float(100 * (k - 1)), "yillik": float(100 * (k ** (365 / n) - 1)) if n >= 365 else None,
         "dusus": float(100 * (ozk / ozk.cummax() - 1).min()), "ort_yatirim": float(100 * np.mean(yat)),
         "islem": len(islemler), "kazanma": None, "ort_kar": None, "ort_yuzde": None, "t_kar": None, "t_yuzde": None,
         "t_fark": None, "fark_yillik": None}
    if islemler:
        kar = np.array([x["kar"] for x in islemler])
        yz = np.array([x["yuzde"] for x in islemler])
        o.update(kazanma=float(100 * (kar > 0).mean()), ort_kar=float(kar.mean()), ort_yuzde=float(yz.mean()),
                 t_kar=tstat(kar, 10), t_yuzde=tstat(yz, 10))
    if ref is not None:
        f = (r - ref).values
        o.update(t_fark=tstat(f, 30), fark_yillik=float(100 * f.mean() * 365))
    return o


def kartopu_tani(islemler):
    kz = [x["kar"] > 0 for x in islemler]
    kk = [b for a, b in zip(kz, kz[1:]) if a]
    kyk = [b for a, b in zip(kz, kz[1:]) if not a]
    return {"islem": len(islemler),
            "kazanctan_sonra_kazanma": float(100 * np.mean(kk)) if kk else None,
            "kayiptan_sonra_kazanma": float(100 * np.mean(kyk)) if kyk else None,
            "kartopu_kar": float(sum(x["kar"] for x in islemler)),
            "sabit50_kar": float(sum(0.5 * x["yuzde"] for x in islemler)),
            "medyan_yuzde": float(np.median([x["yuzde"] for x in islemler])) if islemler else None}


def nakit_tarihi(w, son):
    acik = w.loc[:son].sum(axis=1) > 0
    if not acik.any():
        return None
    s = acik[acik].index.max()
    return s + GUN if s < son else None


def sayi(x, n=1, w=8):
    return f"{'-':>{w}}" if x is None else f"{x:>{w}.{n}f}"


def isaret(x, n=1):
    return "-" if x is None else f"{x:+.{n}f}"


def oran(x):
    return "-" if x is None else f"%{x:.0f}"


def tarih(x):
    return "-" if x is None else str(x.date())


def tablo(Rd):
    L = [f"{'':<5}{'Toplam%':>9}{'Yıllık%':>9}{'Düşüş%':>9}{'Yatırım%':>10}{'İşlem':>7}{'Kazanma%':>10}"
         f"{'Ort.$':>9}{'Ort.%':>8}{'t($)':>7}{'t(%)':>7}{'t(S1fark)':>10}"]
    for s in STRATEJI:
        o = Rd[s]
        L.append(f"{s:<5}{sayi(o['toplam'], 1, 9)}{sayi(o['yillik'], 1, 9)}{sayi(o['dusus'], 1, 9)}"
                 f"{sayi(o['ort_yatirim'], 1, 10)}{o['islem']:>7}{sayi(o['kazanma'], 0, 10)}{sayi(o['ort_kar'], 2, 9)}"
                 f"{sayi(o['ort_yuzde'], 2, 8)}{sayi(o['t_kar'], 2, 7)}{sayi(o['t_yuzde'], 2, 7)}{sayi(o['t_fark'], 2, 10)}")
    return L


def islem_js(x):
    return {k: (str(v.date()) if isinstance(v, pd.Timestamp) else round(v, 4) if isinstance(v, float) else v)
            for k, v in x.items()}


def yuvarla(o):
    if isinstance(o, dict):
        return {k: yuvarla(v) for k, v in o.items()}
    if isinstance(o, list):
        return [yuvarla(v) for v in o]
    if isinstance(o, float):
        return round(o, 3)
    return o


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    P, ust, bilgi = veri()
    son = P.index[-1]
    D = {"egitim": (EGITIM_BAS, EGITIM_SON), "test": (TEST_BAS, son),
         "son_12_ay": (son - pd.Timedelta(days=364), son), "ekim_kasim_2026": (EKIM, son)}
    W = agirliklar(P, ust)
    S = islem_sinyalleri(P)
    R, K = {}, {}
    for g in (1, 0):
        for ad, (a, b) in D.items():
            if g == 0 and ad not in ("egitim", "test"):
                continue
            k = {s: calistir(s, W, S, P, a, b, g) for s in STRATEJI}
            ref = k["S1"][0].pct_change().dropna()
            R[(g, ad)] = {s: olc(*k[s], None if s == "S1" else ref) for s in STRATEJI}
            K[(g, ad)] = k

    def gecer(s, g):
        e, t = R[(g, "egitim")], R[(g, "test")]
        return e[s]["yillik"] > e["S1"]["yillik"] and t[s]["yillik"] > t["S1"]["yillik"]

    def kanitli(s, g):
        e, t = R[(g, "egitim")][s]["t_fark"], R[(g, "test")][s]["t_fark"]
        return gecer(s, g) and e is not None and t is not None and e >= ESIK and t >= ESIK

    tani = {g: {s: {ad: kartopu_tani(K[(g, ad)][s][1]) for ad in ("egitim", "test")} for s in ("S4a", "S5")} for g in (1, 0)}
    kt = K[(1, "test")]
    ekim_son = KASIM - GUN
    kasim = {s: {"yuzde": float(100 * (kt[s][0][son] / kt[s][0][ekim_son] - 1)),
                 "tutar": float(kt[s][0][son] - kt[s][0][ekim_son])} for s in STRATEJI}
    kasim_islem = {s: [x for x in kt[s][1] if x["cikis"] >= KASIM] for s in ("S4a", "S4b", "S5")}
    n1, n0 = nakit_tarihi(W["S1"].shift(1), son), nakit_tarihi(W["S1"], son)
    btc_kasim = float(100 * (P.at[son, "BTC"] / P.at[ekim_son, "BTC"] - 1))
    eth_kasim = float(100 * (P.at[son, "ETH"] / P.at[ekim_son, "ETH"] - 1))
    btc_n1 = float(100 * (P.at[son, "BTC"] / P.at[n1, "BTC"] - 1)) if n1 is not None else None
    btc_n0 = float(100 * (P.at[son, "BTC"] / P.at[n0, "BTC"] - 1)) if n0 is not None else None
    s1_gun = [{"tarih": str(d.date()), "btc": float(P.at[d, "BTC"]), "eth": float(P.at[d, "ETH"]),
               "s1_btc_sinyal": float(W["S1"].at[d, "BTC"]), "s1_eth_sinyal": float(W["S1"].at[d, "ETH"]),
               "s1_btc_elde": float(W["S1"]["BTC"].shift(1).at[d]), "s1_eth_elde": float(W["S1"]["ETH"].shift(1).at[d])}
              for d in P.loc[EKIM:son].index]

    E, T = R[(1, "egitim")], R[(1, "test")]
    E0, T0 = R[(0, "egitim")], R[(0, "test")]
    L = [f"CESARET TESTİ: önceden kayıtlı geçmiş test (çalıştırma {time.strftime('%Y-%m-%d')}, son kapanış {son.date()})",
         "Soru: bot daha cesur olsaydı (al-tut, hızlı trend, kartopu pay) uygulamanın trend çekirdeğinden (S1) iyi olur muydu?",
         "",
         "Veri    : Yahoo günlük kapanış: " + ", ".join(f"{v['sembol']} {v['ilk']}..{v['son']}" for v in bilgi.values())
         + f"; eksik kripto günü BTC {bilgi['BTC']['eksik_gun']}, ETH {bilgi['ETH']['eksik_gun']}.",
         "          range=max günlük değil aylık/haftalık mum döndürüyor; aynı uç period1/period2 ile çekildi. Bugünün bitmemiş mumu atıldı.",
         "Kurallar: sinyal kapanışta, o kapanışa kadarki veriyle; işlem BİR SONRAKİ kapanışta (gecikme 1). Maliyet tek yön kripto %0.15,",
         "          altın %0.10. Boş para USDT (getirisiz). Her dönem 1000 $ nakitle başlar; dönem sonunda açık pozisyon maliyetle kapatılır.",
         "          Stoplar kapanışla ölçülür (gün içi fitil yok). Altın GC=F sürekli vadeli (spot altına yakın), hafta sonu son fiyat.",
         "          ETH Yahoo'da 2017-11-09'dan: SMA200 2018-05 sonuna kadar yok, yoksa 'altında' sayılır (S1/S2 ETH payı o dönem düşük).",
         f"Çoklu test: {len(STRATEJI)} önceden kayıtlı strateji (S4 iki pay kuralı) → Bonferroni eşiği |t| ≥ {ESIK:.2f} (çift yönlü %5/{len(STRATEJI)}).",
         "Karar kuralı (önceden): varyant S1'i 'geçer' = maliyet sonrası yıllık getiri hem eğitimde hem testte S1'den yüksek;",
         f"          'kanıtlı geçer' = ayrıca S1'e göre günlük fark t ≥ {ESIK:.2f} iki dönemde de. Sinyalin pozitif beklentisi =",
         f"          işlem başı net ortalama iki dönemde de > 0 (kanıtlı: t ≥ {ESIK:.2f}).",
         "İşlem   : S4/S5 gerçek işlem; % = paya göre. S0-S3'te işlem = varlığın sıfırdan pozisyona girip sıfıra döndüğü dönem; % = dönem",
         "          başındaki kasaya göre. Yatırım% = kasanın ortalama ne kadarının coinde/altında olduğu. t(S1fark) = S1'e göre günlük getiri farkı.",
         "          İşlem t'si sadece 10+ işlemde gösterilir. Her dönem ayrı koşudur (Kasım bölümü test koşusunun içinden).",
         ""]
    L += [f"{s:<5}{ADLAR[s]}" for s in STRATEJI]
    for ad, (a, b) in D.items():
        L += ["", f"=== {DONEM_AD[ad]} {a.date()} → {b.date()} ({R[(1, ad)]['S1']['gun']} gün), gecikme 1 ==="] + tablo(R[(1, ad)])
        if ad == "ekim_kasim_2026":
            L.append("  (yıllık getiri 1 yıldan kısa dönemde gösterilmez)")
    L += ["", "=== S1'i geçiyor mu? (maliyet sonrası yıllık getiri, gecikme 1) ==="]
    for s in [x for x in STRATEJI if x != "S1"]:
        L.append(f"{s:<5}eğitim {E[s]['yillik']:7.1f}% vs S1 {E['S1']['yillik']:6.1f}% (t {isaret(E[s]['t_fark'], 2)}) | "
                 f"test {T[s]['yillik']:6.1f}% vs S1 {T['S1']['yillik']:5.1f}% (t {isaret(T[s]['t_fark'], 2)}) | "
                 f"düşüş eğitim {E[s]['dusus']:6.1f}% / test {T[s]['dusus']:6.1f}% (S1 {E['S1']['dusus']:.1f}% / {T['S1']['dusus']:.1f}%) → "
                 + ("KANITLI GEÇER" if kanitli(s, 1) else "geçer (kanıtsız)" if gecer(s, 1) else "geçemez"))
    L += ["", "=== Sinyalin beklentisi (paydan bağımsız, işlem başı net %, gecikme 1) ==="]
    for s, ad_ in (("S4a", "S4 SMA20 kırılımı (S4a ve S4b aynı işlemler)"), ("S5", "S5 düşüşte dip alımı")):
        L.append(ad_)
        for p in ("egitim", "test", "son_12_ay", "ekim_kasim_2026"):
            o = R[(1, p)][s]
            L.append(f"  {DONEM_AD[p]:<17} işlem {o['islem']:>3}  ortalama {isaret(o['ort_yuzde'], 2):>6}%  kazanma {sayi(o['kazanma'], 0, 3)}%  "
                     f"t {isaret(o['t_yuzde'], 2)}")
    L += ["", "=== Kartopu payın etkisi (aynı işlemler, sabit 50 $ ile karşılaştırma, gecikme 1) ==="]
    for s in ("S4a", "S5"):
        for p in ("egitim", "test"):
            x = tani[1][s][p]
            L.append(f"{s:<5}{DONEM_AD[p]:<7} işlem {x['islem']:>3} | kartopu {x['kartopu_kar']:+8.2f} $ vs sabit 50 $ {x['sabit50_kar']:+8.2f} $ | "
                     f"kazançtan sonra kazanma {sayi(x['kazanctan_sonra_kazanma'], 0, 3)}%, kayıptan sonra {sayi(x['kayiptan_sonra_kazanma'], 0, 3)}%")
    L += ["", f"=== Kasım 2026 (2026-11-01 → {son.date()}, test koşusunun içinden, gecikme 1) ===",
          f"BTC {btc_kasim:+.1f}%, ETH {eth_kasim:+.1f}%.",
          f"S1 tamamen nakitte: gecikme 1 ile {n1.date() if n1 is not None else '-'} kapanışından beri (BTC o günden {isaret(btc_n1)}%), "
          f"aynı kapanışta işlemle (uygulama) {n0.date() if n0 is not None else '-'} kapanışından beri (BTC {isaret(btc_n0)}%)."]
    L.append("Kasım getirisi: " + ", ".join(f"{s} {kasim[s]['yuzde']:+.1f}% ({kasim[s]['tutar']:+.1f} $)" for s in STRATEJI))
    for s in ("S4a", "S4b", "S5"):
        xs = kasim_islem[s]
        L.append(f"{s} kasımda kapanan/açık işlem: {len(xs)}" + ("" if xs else " (sinyal yok)"))
        for x in xs:
            L.append(f"   {x['varlik']} giriş {x['giris'].date()} {x['giris_fiyat']:.0f} → çıkış {x['cikis'].date()} {x['cikis_fiyat']:.0f} "
                     f"pay {x['pay']:.0f} $ kâr {x['kar']:+.2f} $ ({x['yuzde']:+.1f}%) {x['neden']}")
    L += ["", "=== Duyarlılık: sinyalle AYNI kapanışta işlem (uygulamanın 00:05 UTC işlemi gibi, gecikme 0) ==="]
    for s in STRATEJI:
        e, t = E0[s], T0[s]
        L.append(f"{s:<5}eğitim yıllık {e['yillik']:7.1f}% düşüş {e['dusus']:6.1f}% | test yıllık {t['yillik']:6.1f}% düşüş {t['dusus']:6.1f}% | "
                 f"işlem başı {isaret(e['ort_yuzde'], 2)}% / {isaret(t['ort_yuzde'], 2)}% (t {isaret(e['t_yuzde'], 2)} / {isaret(t['t_yuzde'], 2)})"
                 + ("" if s == "S1" else f" | S1'e göre t {isaret(e['t_fark'], 2)} / {isaret(t['t_fark'], 2)} → "
                    + ("KANITLI GEÇER" if kanitli(s, 0) else "geçer (kanıtsız)" if gecer(s, 0) else "geçemez")))

    gecen1 = [s for s in CESUR if gecer(s, 1)]
    kanit1 = [s for s in CESUR if kanitli(s, 1)]
    gecen0 = [s for s in CESUR if gecer(s, 0)]

    def fark(s):
        return (f"{s} (eğitim {E[s]['yillik'] - E['S1']['yillik']:+.1f}, test {T[s]['yillik'] - T['S1']['yillik']:+.1f} puan/yıl; "
                f"t {isaret(E[s]['t_fark'], 2)} / {isaret(T[s]['t_fark'], 2)}, eşik {ESIK:.2f}; test düşüşü {T[s]['dusus']:.1f}% "
                f"vs S1 {T['S1']['dusus']:.1f}%; aynı kapanışta işlemde {'yine geçer' if gecer(s, 0) else 'geçemez'})")

    def beklenti(s, g):
        e, t = R[(g, "egitim")][s], R[(g, "test")][s]
        poz = (e["ort_yuzde"] or 0) > 0 and (t["ort_yuzde"] or 0) > 0
        kan = poz and (e["t_yuzde"] or 0) >= ESIK and (t["t_yuzde"] or 0) >= ESIK
        return poz, kan, e, t

    def esik_notu(t):
        return "eşiği geçer" if t is not None and t >= ESIK else "eşik altı"

    def kartopu_farki(x, p):
        return x[p]["kartopu_kar"] - x[p]["sabit50_kar"]

    M = ["", "=== Sonuç ==="]
    if kanit1:
        M.append("Önce sonuç: S1'i kanıtlı geçen cesur varyant var: " + "; ".join(fark(s) for s in kanit1) + ".")
    elif gecen1:
        M.append("Önce sonuç: S1'i kanıtlı geçen cesur varyant yok. Kâğıt üzerinde iki dönemde de geçen: "
                 + "; ".join(fark(s) for s in gecen1) + ". Fark şans düzeyinde.")
    else:
        M.append("Önce sonuç: daha cesur hiçbir varyant (S0 al-tut, S3 hızlı trend, S4a/S4b/S5 kartopu) S1'i hem eğitimde hem testte "
                 "maliyet sonrası geçemedi.")
    M.append(f"Al-tut (S0): yıllık eğitim {E['S0']['yillik']:.1f}% / test {T['S0']['yillik']:.1f}%, en derin düşüş {E['S0']['dusus']:.1f}% / "
             f"{T['S0']['dusus']:.1f}%; S1: {E['S1']['yillik']:.1f}% / {T['S1']['yillik']:.1f}%, düşüş {E['S1']['dusus']:.1f}% / {T['S1']['dusus']:.1f}%.")
    M.append(f"Kartopu varyantları kasanın ortalama %{T['S4a']['ort_yatirim']:.0f} (S4a), %{T['S4b']['ort_yatirim']:.0f} (S4b), "
             f"%{T['S5']['ort_yatirim']:.0f} (S5) kadarını kullanır, S1 %{T['S1']['ort_yatirim']:.0f} (test): 1000 $ kasada 50-200 $'lık işlem "
             f"S1'den cesur değil, daha ürkektir. Yıllık getiri eğitim {E['S4a']['yillik']:.1f} / {E['S4b']['yillik']:.1f} / {E['S5']['yillik']:.1f}%, "
             f"test {T['S4a']['yillik']:.1f} / {T['S4b']['yillik']:.1f} / {T['S5']['yillik']:.1f}%.")
    M.append(f"S2 (boş para altında; cesaret değil, boştaki parayı değerlendirme): eğitim {E['S2']['yillik']:.1f}%, test {T['S2']['yillik']:.1f}% "
             f"(test düşüşü {T['S2']['dusus']:.1f}%), S1'e göre t {isaret(E['S2']['t_fark'], 2)} / {isaret(T['S2']['t_fark'], 2)} → "
             + ("kanıtlı geçer" if kanitli("S2", 1) else "geçer ama kanıtsız" if gecer("S2", 1) else "geçemez")
             + f"; aynı kapanışta işlemde {'geçer' if gecer('S2', 0) else 'geçemez'}.")
    for s, ad_ in (("S4a", "S4 sinyali (SMA20 kırılımı)"), ("S5", "S5 sinyali (düşüşte dip alımı)")):
        poz, kan, e, t = beklenti(s, 1)
        y = R[(1, "son_12_ay")][s]
        M.append(f"{ad_}: pozitif beklenti " + ("var ve kanıtlı" if kan else "var ama kanıtlı değil" if poz else "YOK")
                 + f". İşlem başı net ortalama eğitim {isaret(e['ort_yuzde'], 2)}% (n {e['islem']}, t {isaret(e['t_yuzde'], 2)}, {esik_notu(e['t_yuzde'])}), "
                 f"test {isaret(t['ort_yuzde'], 2)}% (n {t['islem']}, t {isaret(t['t_yuzde'], 2)}, {esik_notu(t['t_yuzde'])}), "
                 f"son 12 ay {isaret(y['ort_yuzde'], 2)}% (n {y['islem']}).")
    x4, x5 = tani[1]["S4a"], tani[1]["S5"]
    M.append("Kartopu pay (kazandıkça büyüyen) ancak sinyalin beklentisi pozitifse ve kazançlar art arda geliyorsa işe yarar. Aynı işlemlerde "
             f"sabit 50 $'a göre: S4a eğitim {kartopu_farki(x4, 'egitim'):+.0f} $ (kazançtan sonra kazanma {oran(x4['egitim']['kazanctan_sonra_kazanma'])}, "
             f"kayıptan sonra {oran(x4['egitim']['kayiptan_sonra_kazanma'])}), test {kartopu_farki(x4, 'test'):+.0f} $; "
             f"S5 eğitim {kartopu_farki(x5, 'egitim'):+.0f} $, test {kartopu_farki(x5, 'test'):+.0f} $. S5'te kazanç +%5 ile sınırlı, kayıp -%8 ve "
             "üstü: seri büyüdükçe pay büyür, seriyi bitiren büyük kayıp en büyük payla gelir.")
    k5 = kasim_islem["S5"]
    M.append(f"Kasım 2026: BTC {btc_kasim:+.1f}%. S1 {tarih(n0)} kapanışından beri (gecikme 1 ile {tarih(n1)}) nakitte, kasım "
             f"{kasim['S1']['yuzde']:+.1f}%; o günden beri BTC {isaret(btc_n0)}% / {isaret(btc_n1)}%. Al-tut {kasim['S0']['yuzde']:+.1f}%. "
             f"S4a {len(kasim_islem['S4a'])} işlem ({kasim['S4a']['tutar']:+.1f} $). S5 {len(k5)} işlem, {sum(x['kar'] <= 0 for x in k5)} tanesi zararda "
             f"({kasim['S5']['tutar']:+.1f} $" + (f", işlem başı {np.mean([x['yuzde'] for x in k5]):+.1f}%" if k5 else "") + ").")
    M.append(f"Aynı kapanışta işlem (uygulama gibi) varsayımında: S1 yıllık eğitim {E0['S1']['yillik']:.1f}% / test {T0['S1']['yillik']:.1f}%; "
             "S1'i iki dönemde geçen cesur varyant: " + (", ".join(gecen0) if gecen0 else "yok")
             + "; S4 sinyali " + ("pozitif" if beklenti("S4a", 0)[0] else "pozitif değil")
             + f" (t {isaret(E0['S4a']['t_yuzde'], 2)} / {isaret(T0['S4a']['t_yuzde'], 2)}), S5 "
             + ("pozitif" if beklenti("S5", 0)[0] else "pozitif değil")
             + f" (t {isaret(E0['S5']['t_yuzde'], 2)} / {isaret(T0['S5']['t_yuzde'], 2)}).")
    M.append("Bu sayılar geçmiş testtir; tek varlık çifti (BTC/ETH) ve tek geçmiş yol, gerçek para sonucu değildir.")
    L += M
    metin = "\n".join(L) + "\n"
    os.makedirs(CIKTI, exist_ok=True)
    with open(os.path.join(CIKTI, "cesaret_testi.txt"), "w", encoding="utf-8") as fh:
        fh.write(metin)
    js = {"calistirma": time.strftime("%Y-%m-%d %H:%M"), "son_kapanis": str(son.date()), "veri": bilgi,
          "ayar": {"maliyet_tek_yon": MALIYET, "kasa": KASA, "hedef_oynaklik": VT, "gecikme_ana": 1,
                   "bonferroni_strateji": len(STRATEJI), "bonferroni_esik_t": ESIK},
          "stratejiler": ADLAR,
          "donemler": {k: [str(a.date()), str(b.date())] for k, (a, b) in D.items()},
          "sonuc": {f"gecikme_{g}": {ad: R[(g, ad)] for (g2, ad) in R if g2 == g} for g in (1, 0)},
          "s1_gecer": {f"gecikme_{g}": {s: {"gecer": gecer(s, g), "kanitli": kanitli(s, g)} for s in STRATEJI if s != "S1"}
                       for g in (1, 0)},
          "kartopu_tani": {f"gecikme_{g}": tani[g] for g in (1, 0)},
          "kasim_2026": {"btc_yuzde": btc_kasim, "eth_yuzde": eth_kasim,
                         "s1_nakit_tarihi_gecikme1": str(n1.date()) if n1 is not None else None,
                         "s1_nakit_tarihi_gecikme0": str(n0.date()) if n0 is not None else None,
                         "btc_nakitten_beri_gecikme1": btc_n1, "btc_nakitten_beri_gecikme0": btc_n0,
                         "strateji": kasim, "islemler": {s: [islem_js(x) for x in v] for s, v in kasim_islem.items()},
                         "s1_gunluk": s1_gun},
          "islemler_test": {s: [islem_js(x) for x in K[(1, "test")][s][1]] for s in ("S4a", "S4b", "S5")},
          "sonuc_metni": M[2:]}
    with open(os.path.join(CIKTI, "cesaret_testi.json"), "w", encoding="utf-8") as fh:
        json.dump(yuvarla(js), fh, ensure_ascii=False, indent=1)
    print(metin)


if __name__ == "__main__":
    main()
