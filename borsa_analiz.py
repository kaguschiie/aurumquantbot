"""Borsa analiz katmani: mali tablodan oranlar, fiyattan teknik gorunum, sektor ici karsilastirma, gerekceli puan.
Saf hesap (ag yok): girdi borsa_veri ciktilari. Eksik veride oran None kalir ve 'eksik' listesine yazilir; tahmin uretilmez.
Onemli sinir: BIST sirketleri enflasyon muhasebesi (TMS 29) uygular; farkli donemler farkli satin alma gucundedir. Son 12 ay (TTM)
ve buyume hesaplari bu yuzden YAKLASIKTIR (uyari olarak tasinir). Puan bir siralama aracidir, yatirim tavsiyesi degildir."""
import math
import time

import numpy as np
import pandas as pd

import teknik as T

# kalem kodlari (Is Yatirim). Sanayi/ticaret XI_29; banka UFRS_K; sigorta UFRS (sinirli)
K = {"XI_29": {"donen": "1A", "nakit": "1AA", "fin_yatirim": "1AB", "stok": "1AF", "varlik": "1BL", "kv_yuk": "2A", "kv_borc": "2AA",
               "uv_yuk": "2B", "uv_borc": "2BA", "ozkaynak": "2N", "ana_ozkaynak": "2O", "sermaye": "2OA", "satis": "3C", "brut_kar": "3D",
               "faaliyet_kari": "3DF", "net_kar": "3L", "ana_net_kar": "3Z", "amortisman": "4B", "isletme_nakit": "4C", "yatirim_harcamasi": "4CAI",
               "serbest_nakit": "4CB"},
     "UFRS_K": {"varlik": "1Z", "krediler": "1AF", "mevduat": "2A", "ozkaynak": "2O", "ana_ozkaynak": "2O", "net_faiz": "3C", "net_kar": "3Z",
                "ana_net_kar": "3Z"},
     "UFRS": {"varlik": "1Z", "net_kar": "3Z", "ana_net_kar": "3Z"}}
AKIS = {"satis", "brut_kar", "faaliyet_kari", "net_kar", "ana_net_kar", "amortisman", "isletme_nakit", "yatirim_harcamasi", "serbest_nakit",
        "net_faiz"}  # yilbasindan kumulatif gelen kalemler (bilanco kalemleri donem sonu degeridir)
TMS29 = "Enflasyon muhasebesi (TMS 29) nedeniyle dönemler farklı satın alma gücünde; son 12 ay ve büyüme rakamları yaklaşıktır."
# puan: (oran, yuksek iyi mi). Sektor ici yuzdelikten hesaplanir
BILESEN = {"deger": [("fk", False), ("pd_dd", False), ("fd_favok", False)],
           "kalite": [("roe", True), ("net_marj", True), ("net_borc_favok", False), ("cari_oran", True)],
           "buyume": [("satis_buyume", True), ("net_kar_buyume", True)],
           "momentum": [("getiri_12_1", True), ("sma200_fark", True), ("teknik", True)]}
AD = {"fk": "F/K", "pd_dd": "PD/DD", "fd_favok": "FD/FAVÖK", "fd_satis": "FD/Satış", "roe": "Özkaynak kârlılığı", "roa": "Aktif kârlılığı",
      "net_marj": "Net kâr marjı", "favok_marj": "FAVÖK marjı", "brut_marj": "Brüt marj", "net_borc_favok": "Net borç/FAVÖK", "cari_oran": "Cari oran",
      "borc_ozkaynak": "Finansal borç/özkaynak", "satis_buyume": "Satış büyümesi (yıllık)", "net_kar_buyume": "Net kâr büyümesi (yıllık)",
      "temettu_verimi": "Temettü verimi", "snak_verimi": "Serbest nakit akışı verimi", "getiri_12_1": "12 aylık getiri (son ay hariç)",
      "sma200_fark": "200 günlük ortalamaya uzaklık", "teknik": "Teknik özet (günlük)"}


def _f(x):
    return None if x is None or not isinstance(x, (int, float)) or not math.isfinite(x) else float(x)


def _bol(a, b, pozitif=True):
    a, b = _f(a), _f(b)
    if a is None or b is None or b == 0 or (pozitif and b < 0):
        return None
    return a / b


class Tablo:
    """Mali tablo erisimi: donem sonu degeri, son 12 ay (TTM), bir yil onceki TTM."""

    def __init__(self, t):
        self.grup, self.d, self.k = t["grup"], [tuple(x) for x in t["donemler"]], t["kalemler"]
        self.kod = K.get(self.grup, {})

    def _v(self, ad, donem):
        c = self.kod.get(ad)
        if not c or c not in self.k or donem not in self.d:
            return None
        return _f(self.k[c]["v"][self.d.index(donem)])

    def son(self, ad, geri=0):
        """Bilanco kalemi: en yeni donemden 'geri' ceyrek onceki deger."""
        return self._v(ad, self.d[geri]) if len(self.d) > geri else None

    def ttm(self, ad, geri_yil=0):
        """Akis kalemi son 12 ay: yil sonu ise dogrudan; degilse bu yil kumulatif + onceki yil sonu - onceki yil ayni donem."""
        if not self.d:
            return None
        y, p = self.d[0]
        y -= geri_yil
        if p == 12:
            return self._v(ad, (y, 12))
        a, b, c = self._v(ad, (y, p)), self._v(ad, (y - 1, 12)), self._v(ad, (y - 1, p))
        return None if None in (a, b, c) else a + b - c


def oranlar(tablo, ev, fiyat):
    """tablo/ev/fiyat: borsa_veri 'veri' alanlari (None olabilir). Donus: {oran: deger|None, 'eksik': [...], 'uyari': [...], 'donem': 'YYYY/A'}."""
    o, eksik, uyari = {}, [], []
    son = _f((fiyat or {}).get("son")) or _f((ev or {}).get("kapanis"))
    serm = _f((ev or {}).get("sermaye_mn"))
    pd_ = son * serm * 1e6 if son and serm else (_f((ev or {}).get("pd_mn")) or 0) * 1e6 or None
    pd_kaynak = _f((ev or {}).get("pd_mn"))
    if pd_ and pd_kaynak and abs(pd_ / (pd_kaynak * 1e6) - 1) > 0.08:
        uyari.append(f"Piyasa değeri iki kaynakta farklı (hesaplanan {pd_ / 1e9:.1f} mlr TL, kaynak {pd_kaynak / 1e3:.1f} mlr TL): sermaye artırımı/bölünme olabilir.")
    o["pd"] = pd_
    if not tablo:
        return {**o, "eksik": ["mali tablo"], "uyari": uyari, "donem": None, "grup": None}
    t = Tablo(tablo)
    o["grup"], o["donem"] = t.grup, f"{t.d[0][0]}/{t.d[0][1]}"
    yas = (time.time() - time.mktime((t.d[0][0], t.d[0][1], 28, 0, 0, 0, 0, 0, 0))) / 86400
    if yas > 200:
        uyari.append(f"Son mali tablo {o['donem']} dönemine ait (yaklaşık {yas / 30:.0f} ay önce): oranlar güncel olmayabilir.")
    kar, kar0 = t.ttm("ana_net_kar"), t.ttm("ana_net_kar", 1)
    oz = t.son("ana_ozkaynak") or t.son("ozkaynak")
    o.update(net_kar_ttm=kar, ozkaynak=oz, varlik=t.son("varlik"))
    o["fk"] = _bol(pd_, kar) if kar and kar > 0 else None
    o["pd_dd"] = _bol(pd_, oz)
    oz0 = t.son("ana_ozkaynak", 4) or t.son("ozkaynak", 4)
    o["roe"] = _bol(kar, (oz + oz0) / 2 if oz and oz0 else oz)
    o["roa"] = _bol(kar, t.son("varlik"))
    o["net_kar_buyume"] = _bol(kar - kar0, abs(kar0)) if kar is not None and kar0 not in (None, 0) else None
    if t.grup == "XI_29":
        satis, satis0 = t.ttm("satis"), t.ttm("satis", 1)
        fk_, am = t.ttm("faaliyet_kari"), t.ttm("amortisman")
        favok = fk_ + am if fk_ is not None and am is not None else None
        borc = (t.son("kv_borc") or 0) + (t.son("uv_borc") or 0)
        net_borc = borc - (t.son("nakit") or 0) - (t.son("fin_yatirim") or 0)
        fd = pd_ + net_borc if pd_ else None
        snak = t.ttm("serbest_nakit")
        o.update(satis_ttm=satis, favok_ttm=favok, net_borc=net_borc, fd=fd,
                 fd_favok=_bol(fd, favok) if favok and favok > 0 and fd and fd > 0 else None, fd_satis=_bol(fd, satis) if fd and fd > 0 else None,
                 brut_marj=_bol(t.ttm("brut_kar"), satis), favok_marj=_bol(favok, satis), net_marj=_bol(kar, satis),
                 net_borc_favok=_bol(net_borc, favok) if favok and favok > 0 else None, cari_oran=_bol(t.son("donen"), t.son("kv_yuk")),
                 borc_ozkaynak=_bol(borc, t.son("ozkaynak")), satis_buyume=_bol(satis - satis0, satis0) if satis is not None and satis0 else None,
                 snak_verimi=_bol(snak, pd_))
        uyari.append(TMS29)
    else:
        uyari.append("Banka/sigorta: sadece F/K, PD/DD, özkaynak ve aktif kârlılığı hesaplanır (FAVÖK, borç ve marj oranları bu şirketlere uygulanmaz).")
    tm = [a for ts, a in ((fiyat or {}).get("temettu") or []) if time.time() - ts < 365 * 86400]
    o["temettu_verimi"] = sum(tm) / son if tm and son else (0.0 if fiyat and son else None)
    for k in ("fk", "pd_dd", "roe") + (("fd_favok", "net_marj", "cari_oran", "satis_buyume") if t.grup == "XI_29" else ()):
        if o.get(k) is None:
            eksik.append(AD.get(k, k) + (" (zarar eden şirkette hesaplanmaz)" if k == "fk" and kar is not None and kar <= 0 else ""))
    return {**o, "eksik": eksik, "uyari": uyari}


def teknik(mumlar):
    """mumlar: [[t, o, h, l, c, adjc, v], ...] gunluk. Donus: gorunum sozlugu ya da None (yetersiz veri)."""
    if not mumlar or len(mumlar) < 60:
        return None
    df = pd.DataFrame(mumlar, columns=["t", "open", "high", "low", "close", "adj", "volume"]).astype(float)
    c, adj = df.close, df.adj
    r = T.rating(df.iloc[-250:].reset_index(drop=True))
    g = lambda n: float(adj.iloc[-1] / adj.iloc[-1 - n] - 1) if len(adj) > n else None
    sma = lambda n: float(c.iloc[-n:].mean()) if len(c) >= n else None
    son = float(c.iloc[-1])
    yil = df.iloc[-250:]
    out = {"rec": r["rec"], "etiket": r["label"], "rsi": r["rsi"], "adx": r["adx"], "atr_pct": r["atr_pct"], "son": son,
           "sma50": sma(50), "sma200": sma(200), "sma200_fark": son / sma(200) - 1 if sma(200) else None,
           "getiri_1a": g(21), "getiri_3a": g(63), "getiri_12a": g(250),
           "getiri_12_1": float(adj.iloc[-22] / adj.iloc[-251] - 1) if len(adj) > 251 else None,
           "zirve_52h": float(yil.high.max()), "dip_52h": float(yil.low.min()), "zirveden": son / float(yil.high.max()) - 1,
           "oynaklik": float(adj.pct_change().iloc[-60:].std() * math.sqrt(250)), "hacim_tl": float((df.close * df.volume).iloc[-20:].mean())}
    out["trend"] = ("yükseliş" if out["sma50"] and out["sma200"] and son > out["sma50"] > out["sma200"] else
                    "düşüş" if out["sma50"] and out["sma200"] and son < out["sma50"] < out["sma200"] else "yatay/kararsız")
    out["destek"], out["direnc"] = seviyeler(df.iloc[-180:], son)
    return out


def seviyeler(df, son, pencere=5, n=3):
    """Son ~9 ayin tepe/dip donus noktalarindan fiyata en yakin n destek ve n direnc (%1,5 icindekiler birlestirilir)."""
    h, l = df.high.values, df.low.values
    tepe = [h[i] for i in range(pencere, len(h) - pencere) if h[i] == h[i - pencere:i + pencere + 1].max()]
    dip = [l[i] for i in range(pencere, len(l) - pencere) if l[i] == l[i - pencere:i + pencere + 1].min()]

    def sade(xs):
        out = []
        for x in xs:
            if not out or abs(x / out[-1] - 1) > 0.015:
                out.append(float(x))
        return out
    return (sade(sorted((x for x in dip + tepe if x < son * 0.995), reverse=True))[:n],
            sade(sorted(x for x in dip + tepe if x > son * 1.005))[:n])


def yuzdelikler(tum):
    """tum: {kod: {'sektor', 'oran': {...}}}. Her oran icin sektor ici yuzdelik (0-1; en az 5 sirket), yoksa tum piyasa."""
    out = {k: {} for k in tum}
    oranlar_ = {x for g in BILESEN.values() for x, _ in g}
    for o in oranlar_:
        grup = {}
        for k, v in tum.items():
            x = _f(v["oran"].get(o))
            if x is not None:
                grup.setdefault(v["sektor"], []).append((k, x))
        hepsi = sorted(x for g in grup.values() for _, x in g)
        for sek, liste in grup.items():
            taban = sorted(x for _, x in liste) if len(liste) >= 5 else hepsi
            for k, x in liste:
                out[k][o] = {"y": float(np.searchsorted(taban, x, side="right") - 0.5) / len(taban), "n": len(taban),
                             "kapsam": "sektör" if len(liste) >= 5 else "tüm piyasa"}
    return out


def puan(oran, yuzde):
    """Sektor ici yuzdeliklerden 0-100 puan. En az 3 bilesende veri yoksa puan yok. Donus: {puan, bilesen, arti, eksi, guven}."""
    bil, arti, eksi = {}, [], []
    for ad, liste in BILESEN.items():
        ps = []
        for o, yuksek_iyi in liste:
            y = (yuzde.get(o) or {}).get("y")
            if y is None:
                continue
            p = y if yuksek_iyi else 1 - y
            ps.append(p)
            v, kapsam = oran.get(o), yuzde[o]["kapsam"]
            metin = f"{AD[o]} {bicim(o, v)}: {kapsam} içinde {'en iyi' if p >= 0.5 else 'en zayıf'} %{(1 - p if p >= 0.5 else p) * 100:.0f}'lik dilimde"
            if p >= 0.75:
                arti.append(metin)
            elif p <= 0.25:
                eksi.append(metin)
        if ps:
            bil[ad] = round(100 * sum(ps) / len(ps))
    if oran.get("net_kar_ttm") is not None and oran["net_kar_ttm"] <= 0:
        eksi.append("Son 12 ayda zarar etti (F/K hesaplanmaz)")
    if len(bil) < 3:
        return {"puan": None, "bilesen": bil, "arti": arti, "eksi": eksi, "guven": "yetersiz veri"}
    n = sum((yuzde.get(o) or {}).get("y") is not None for g in BILESEN.values() for o, _ in g)
    return {"puan": round(sum(bil.values()) / len(bil)), "bilesen": bil, "arti": arti[:6], "eksi": eksi[:6],
            "guven": "yüksek" if n >= 10 and len(bil) == 4 else "orta" if n >= 7 else "düşük"}


def bicim(o, v):
    if v is None:
        return "-"
    if o in ("fk", "pd_dd", "fd_favok", "fd_satis", "net_borc_favok", "cari_oran", "borc_ozkaynak"):
        return f"{v:.1f}"
    if o == "teknik":
        return f"{v:+.2f}"
    return f"%{v * 100:.1f}"
