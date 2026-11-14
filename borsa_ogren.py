"""Borsa ogrenme defteri: her alim nedeni olculebilir bir HIPOTEZDIR ("bu olay turu N gunde BIST100'u maliyetten fazla gecer").
Tamamen yerel, ucretsiz. Kriptodan alinan ders: kaybeden sinyalde stop/cikis/filtreyle oynamak (37 deneme) yerine once TESHIS.
  ornek     : olay -> giris yayindan sonraki ilk acilis (09:30 oncesi yayin ayni gun), 1/5/20 gunluk BIST100'e gore fark + 20 gunluk yol
  olc/karar : gune gore toplanir (ayni gunun olaylari tek gozlem: piyasa hareketi sisirmesin), egitim ilk 2/3 / test son 1/3.
              Kanit esigi denenen hipotez sayisiyla yukselir (cok deneyen sansla bulur). Alim iddiasi maliyeti (gidis-donus) gecmeli.
  teshis    : kaybedenler ayrilir: giris (hic lehe gitmedi -> sinyal avantajsiz, cikisla UGRASMA), cikis (once lehe gitti, geri verdi),
              maliyet (brut arti, net eksi), ortam (kotu rejimde kayip orani belirgin yuksek).
  kristal   : kazanan ve kaybedenleri en cok ayiran kosul (rejim, seans, onceki momentum, likidite, oynaklik): yeni hipotez adayi.
              Her yeni hipotez deneme sayisina eklenir. Hipotez basina en cok DENEME_HAKKI alt deneme; sonra emekli."""
import bisect
import collections
import datetime
import math
import statistics

import config as C

TR = datetime.timezone(datetime.timedelta(hours=3))
MALIYET = 2 * (C.BORSA_KOMISYON + C.BORSA_KAYMA)   # gidis-donus (binde 6)
UFUKLAR = (1, 5, 20)
YOL = 20
DENEME_HAKKI = 3
MIN_GUN = {"egitim": 20, "test": 10}
ONERI = {"giris": "Sinyalin girişte avantajı yok: stop/çıkış/filtre denemesi YAPMA. Kazanan ile kaybedeni ayıran koşula (kristal) bak.",
         "cikis": "Fiyat önce lehe gidip geri veriyor: çıkışı dene (kâr al, daha kısa süre).",
         "maliyet": "Brüt kârlı, maliyet yiyor: daha seyrek işlem veya daha uzun ufuk dene.",
         "ortam": "Kayıplar kötü piyasa rejiminde toplanıyor: rejim kapısı dene (BIST100 200 günlük ortalamanın üstü)."}


def seri(mumlar):
    """Yahoo mumlari -> {'g': [TR tarihi], 'o', 'c', 'v'}. Temettuye gore duzeltilmis (m[5]): temettu gunu dususu kayip sayilmasin."""
    s = {"g": [], "o": [], "c": [], "v": []}
    for m in mumlar or []:
        d = datetime.datetime.fromtimestamp(m[0], TR).date().isoformat()
        if (s["g"] and s["g"][-1] >= d) or not m[1] or not m[4]:
            continue
        k_ = (m[5] or m[4]) / m[4]
        for k, v in zip("gocv", (d, m[1] * k_, m[4] * k_, m[6] or 0)):
            s[k].append(v)
    return s


def giris(s, t):
    d = datetime.datetime.fromtimestamp(t, TR)
    gun = d.date().isoformat()
    i = bisect.bisect_left(s["g"], gun)
    if i < len(s["g"]) and s["g"][i] == gun and (d.hour, d.minute) < (9, 30):
        return i
    return bisect.bisect_right(s["g"], gun)


def ornek(t, s, e):
    """Olay ani t, hisse serisi s, endeks serisi e -> ornek veya None (yetersiz/eslesmeyen veri)."""
    i = giris(s, t)
    if i < 21 or i + YOL > len(s["g"]):
        return None
    j = bisect.bisect_left(e["g"], s["g"][i])
    if j < 200 or j + YOL > len(e["g"]) or e["g"][j:j + YOL] != s["g"][i:i + YOL]:
        return None
    p0, x0 = s["o"][i], e["o"][j]
    yol = [round(s["c"][i + k] / p0 - e["c"][j + k] / x0, 5) for k in range(YOL)]
    r = [s["c"][k] / s["c"][k - 1] - 1 for k in range(i - 20, i)]
    saat = datetime.datetime.fromtimestamp(t, TR).hour
    return {"t": t, "gun": s["g"][i], "fark": {u: yol[u - 1] for u in UFUKLAR}, "yol": yol,
            "ozellik": {"rejim": e["c"][j - 1] > statistics.fmean(e["c"][j - 200:j]), "seans_ici": 10 <= saat < 18,
                        "once": s["c"][i - 1] / s["c"][i - 21] - e["c"][j - 1] / e["c"][j - 21],
                        "likidite": statistics.fmean(s["c"][k] * s["v"][k] for k in range(i - 20, i)), "oynak": statistics.pstdev(r)}}


def seyrelt(ornekler, ara=YOL):
    """Ayni hissede ust uste binen ornekler: bir ornekten sonra 'ara' takvim gunu (~islem gunu x1.4) dolmadan yenisi sayilmaz."""
    son, out = {}, []
    for o in sorted(ornekler, key=lambda x: x["t"]):
        if o["t"] - son.get(o["kod"], -1e18) >= ara * 1.4 * 86400:
            out.append(o)
            son[o["kod"]] = o["t"]
    return out


def _ozet(xs, ufuk, engel):
    gun = collections.defaultdict(list)
    for x in xs:
        gun[x["gun"]].append(x["fark"][ufuk] - engel)
    g = [statistics.fmean(v) for v in gun.values()]
    if len(g) < 2:
        return {"n": len(xs), "gun": len(g)}
    m, sd = statistics.fmean(g), statistics.stdev(g)
    return {"n": len(xs), "gun": len(g), "ort": round(m + engel, 5), "t": round(m / (sd / math.sqrt(len(g))), 2) if sd else 0.0,
            "kazanma": round(sum(x["fark"][ufuk] > engel for x in xs) / len(xs), 3)}


def olc(ornekler, ufuk, kesim, iddia=1):
    """Egitim (gun < kesim) / test. Alim iddiasinda t istatistigi getiri - maliyet uzerinden."""
    engel = MALIYET if iddia > 0 else 0.0
    return {"egitim": _ozet([x for x in ornekler if x["gun"] < kesim], ufuk, engel),
            "test": _ozet([x for x in ornekler if x["gun"] >= kesim], ufuk, engel)}


def yon_sec(ornekler, ufuk, kesim):
    """Iddianin yonu SADECE egitimden: ortalama maliyeti geciyorsa al (1), endeksin altindaysa kacin (-1), arada 0 (iddia yok).
    Yonu insan etiketi belirlemez (ornek: 'olumlu' sanilan bedelsiz verisi aksini gosterebilir); test donemi dogrular."""
    f = [x["fark"][ufuk] for x in ornekler if x["gun"] < kesim]
    if not f:
        return 0
    m = statistics.fmean(f)
    return 1 if m > MALIYET else -1 if m < 0 else 0


def esik(deneme):
    """Iki yonlu %5, denenen hipotez sayisina gore (Bonferroni)."""
    return round(statistics.NormalDist().inv_cdf(1 - 0.05 / (2 * max(1, deneme))), 2)


def karar(o, iddia, deneme):
    """kanitli | red | yetersiz. iddia > 0: al (maliyeti gecer); iddia < 0: kacin (endeksin altinda kalir)."""
    e, s = o["egitim"], o["test"]
    if e.get("gun", 0) < MIN_GUN["egitim"] or s.get("gun", 0) < MIN_GUN["test"] or "t" not in e or "t" not in s:
        return "yetersiz"
    if iddia > 0:
        ok = e["ort"] > MALIYET and s["ort"] > MALIYET and e["t"] >= esik(deneme) and s["t"] >= 1.0
    else:
        ok = e["ort"] < 0 and s["ort"] < 0 and e["t"] <= -esik(deneme) and s["t"] <= -1.0
    return "kanitli" if ok else "red"


def teshis(ornekler, ufuk):
    """Kaybedenlerin nedeni. None: yeterli kayip yok."""
    kayip = [x for x in ornekler if x["fark"][ufuk] <= MALIYET]
    if len(kayip) < 10:
        return None
    sebep = collections.Counter()
    for x in kayip:
        yol = x["yol"][:ufuk]
        if 0 < yol[-1] <= MALIYET:
            sebep["maliyet"] += 1
        elif max(yol) >= 2 * MALIYET:
            sebep["cikis"] += 1
        else:
            sebep["giris"] += 1
    oran = lambda xs: sum(x["fark"][ufuk] <= MALIYET for x in xs) / len(xs) if xs else None
    kotu, iyi = [x for x in ornekler if not x["ozellik"]["rejim"]], [x for x in ornekler if x["ozellik"]["rejim"]]
    ortam = len(kotu) >= 10 and len(iyi) >= 10 and oran(kotu) - oran(iyi) >= 0.15
    baskin = "ortam" if ortam else sebep.most_common(1)[0][0]
    return {"sebep": dict(sebep), "kayip": len(kayip), "n": len(ornekler), "baskin": baskin, "oneri": ONERI[baskin],
            "rejim_kayip": {"kotu": oran(kotu), "iyi": oran(iyi)}}


def kristal(ornekler, ufuk, n_min=15, k=3):
    """Kazananla kaybedeni en cok ayiran kosullar: [{kosul, ort_ic, n_ic, ort_dis, n_dis, fark}], farka gore."""
    out = []
    for ad in ("rejim", "seans_ici", "once", "likidite", "oynak"):
        v = [x["ozellik"][ad] for x in ornekler]
        if isinstance(v[0], bool):
            kosullar = [(ad, "==", True), (ad, "==", False)]
        else:
            med = statistics.median(v)
            kosullar = [(ad, ">", med), (ad, "<=", med)]
        for kosul in kosullar:
            ic = [x for x in ornekler if sagla(x, kosul)]
            dis = [x for x in ornekler if not sagla(x, kosul)]
            if len(ic) < n_min or len(dis) < n_min:
                continue
            a, b = statistics.fmean(x["fark"][ufuk] for x in ic), statistics.fmean(x["fark"][ufuk] for x in dis)
            out.append({"kosul": list(kosul), "ort_ic": round(a, 5), "n_ic": len(ic), "ort_dis": round(b, 5), "n_dis": len(dis), "fark": round(a - b, 5)})
    return sorted(out, key=lambda x: -x["fark"])[:k]


def sagla(x, kosul):
    ad, op, d = kosul
    v = x["ozellik"][ad]
    return v == d if op == "==" else v > d if op == ">" else v <= d


class Defter:
    """Hipotez defteri. Her test kaydedilir (deneme sayisi = kanit esigi). Kayit: {ad, tur, iddia, ufuk, kosul, ust, durum, olcum, teshis, kristal}."""

    def __init__(self, kayit=None):
        self.h = list(kayit or [])

    @property
    def deneme(self):
        return len(self.h)

    def alt_sayisi(self, ust):
        return sum(1 for x in self.h if x.get("ust") == ust)

    def test_et(self, ad, tur, iddia, ufuk, ornekler, kesim, kosul=None, ust=None):
        """Hipotezi deftere yazip olcer. Ust hipotezin deneme hakki bittiyse test edilmez (None)."""
        if ust and self.alt_sayisi(ust) >= DENEME_HAKKI:
            return None
        xs = [x for x in ornekler if not kosul or sagla(x, kosul)]
        h = {"ad": ad, "tur": tur, "iddia": iddia, "ufuk": ufuk, "kosul": kosul, "ust": ust}
        self.h.append(h)
        h["olcum"] = olc(xs, ufuk, kesim, iddia)
        h["durum"] = karar(h["olcum"], iddia, self.deneme)
        h["esik"] = esik(self.deneme)
        if h["durum"] == "red":
            h["teshis"] = teshis(xs, ufuk)
            # kristal SADECE egitim verisinden aranir (test donemi kosul secerken gorulmez)
            h["kristal"] = kristal([x for x in xs if x["gun"] < kesim], ufuk) if h["teshis"] and h["teshis"]["baskin"] == "giris" else []
        return h

    def yeniden_karar(self):
        """Tum testler bittikten sonra: herkes SON deneme sayisinin esigiyle yeniden yargilanir (erken test edilen avantajli olmasin)."""
        for h in self.h:
            if "olcum" in h:
                h["durum"] = karar(h["olcum"], h["iddia"], self.deneme)
                h["esik"] = esik(self.deneme)
        for h in self.h:
            if h["durum"] != "kanitli" and h.get("ust") is None and self.alt_sayisi(h["ad"]) >= DENEME_HAKKI \
                    and not any(x["durum"] == "kanitli" for x in self.h if x.get("ust") == h["ad"]):
                h["durum"] = "emekli"
        return self

    def kanitlilar(self):
        return [h for h in self.h if h["durum"] == "kanitli"]
