"""Borsa motoru (BIST): arka plan taramasi, kara liste, sanal portfoy, yapay zeka degerlendirmesi. Kripto motorundan bagimsiz.
GERCEK EMIR YOK: bu modulde borsaya/araci kuruma emir gonderen kod bulunmaz; portfoy sanaldir (TL).
Kara liste ZORUNLU kapidir: pozisyon acan tek yol Borsa.al() -> _kapi(); ilk kontrol kara listedir, yapay zeka dahil hicbir cagri
atlayamaz, engellenen her girisim kara_liste_kayit.jsonl'a yazilir. Kara listedeki eldeki hisse otomatik satilmaz: onay gerekir.
Puanlar ve yapay zeka gorusu analizdir, yatirim tavsiyesi degildir."""
import json
import logging
import math
import os
import re
import threading
import time

import borsa_analiz as BA
import borsa_haber as H
import borsa_veri as V
import config as C

log = logging.getLogger("bot")
_tr = lambda s: str(s or "").replace("İ", "i").replace("I", "ı").lower().strip()


class Borsa:
    _tek = None

    @classmethod
    def shared(cls):
        if cls._tek is None:
            cls._tek = cls()
        return cls._tek

    def __init__(self):
        self.kilit, self.dur, self.is_ = threading.RLock(), threading.Event(), None
        self.ozet = (V._oku("ozet") or {}).get("veri") or {}       # {kod: {oran, teknik, yuzde, puan, t}}
        self.ozet_t = (V._oku("ozet") or {}).get("t") or 0
        self.ilerleme = {"durum": "bekliyor", "n": 0, "toplam": 0, "hata": 0, "t": 0}
        self.ai_onbellek = {}

    # ---------------- arka plan ----------------
    def start(self):
        with self.kilit:
            if self.is_ and self.is_.is_alive():
                return
            self.dur.clear()
            self.is_ = threading.Thread(target=self._dongu, name="borsa", daemon=True)
            self.is_.start()
            threading.Thread(target=H.dongu, args=(self, self.dur), name="borsa-haber", daemon=True).start()

    def _dongu(self):
        while not self.dur.is_set():
            try:
                if time.time() - self.ozet_t > C.BORSA_TARAMA_SAAT * 3600:
                    self.tara()
            except Exception:
                log.exception("borsa tarama")
            self.dur.wait(300)

    def tara(self, kodlar=None, bekle=0.0):
        """Tum hisseler: mali tablo + fiyat -> oranlar + teknik; sonra sektor yuzdelikleri ve puan. Buyukten kucuge (once likit olanlar)."""
        ev = V.evren()["veri"]
        if not ev:
            self.ilerleme.update(durum="hata: hisse listesi alınamadı", t=time.time())
            return
        sira = kodlar or sorted(ev, key=lambda k: -(ev[k].get("pd_mn") or 0))
        self.ilerleme.update(durum="taranıyor", n=0, toplam=len(sira), hata=0, t=time.time())
        yeni = dict(self.ozet) if kodlar else {}
        for i, kod in enumerate(sira):
            if self.dur.is_set():
                return
            try:
                yeni[kod] = self._hisse(kod, ev[kod])
            except Exception as e:  # tek hisse hatasi taramayi durdurmaz
                self.ilerleme["hata"] += 1
                log.warning("borsa %s: %s", kod, str(e)[:120])
            self.ilerleme["n"] = i + 1
            if bekle:
                time.sleep(bekle)
        yz = BA.yuzdelikler({k: {"sektor": ev.get(k, {}).get("sektor", "Diğer"), "oran": {**v["oran"], **{x: (v["teknik"] or {}).get(y) for x, y in
                                  (("getiri_12_1", "getiri_12_1"), ("sma200_fark", "sma200_fark"), ("teknik", "rec"))}}} for k, v in yeni.items()})
        for k, v in yeni.items():
            v["yuzde"] = yz.get(k, {})
            v["puan"] = BA.puan({**v["oran"], "getiri_12_1": (v["teknik"] or {}).get("getiri_12_1"), "sma200_fark": (v["teknik"] or {}).get("sma200_fark"),
                                 "teknik": (v["teknik"] or {}).get("rec")}, v["yuzde"])
        with self.kilit:
            self.ozet, self.ozet_t = yeni, time.time()
            V._yaz("ozet", {"t": self.ozet_t, "veri": yeni})
        self.ilerleme.update(durum="hazır", t=time.time())

    def _hisse(self, kod, ev):
        tb, fy = V.tablo(kod), V.fiyat(kod)
        oran = BA.oranlar(tb["veri"], ev, fy["veri"])
        tk = BA.teknik((fy["veri"] or {}).get("mumlar"))
        kalite = [f"{ad}: {x['hata']}" for ad, x in (("mali tablo", tb), ("fiyat", fy)) if x["hata"]] + \
                 [f"{ad} verisi eski" for ad, x in (("mali tablo", tb), ("fiyat", fy)) if x["bayat"]]
        return {"oran": {k: v for k, v in oran.items() if k not in ("eksik", "uyari")}, "eksik": oran["eksik"], "uyari": oran["uyari"],
                "teknik": tk, "kalite": kalite, "t": time.time(), "tablo_t": tb["t"], "fiyat_t": fy["t"]}

    # ---------------- kara liste ----------------
    def kara(self):
        return (V._oku("kara_liste") or {}).get("veri") or {"kod": {}, "sektor": {}, "ad": {}}

    def kara_degistir(self, tur, deger, neden="", sil=False, kullanici=""):
        """tur: kod (THYAO) | sektor (tam sektor adi) | ad (sirket adinda gecen metin). Hata metni veya None."""
        deger = str(deger or "").strip()
        if tur not in ("kod", "sektor", "ad") or len(deger) < 2:
            return "Geçersiz kayıt."
        anahtar = deger.upper() if tur == "kod" else _tr(deger)
        with self.kilit:
            k = self.kara()
            if sil:
                if anahtar not in k[tur]:
                    return "Kayıt bulunamadı."
                del k[tur][anahtar]
            else:
                if tur == "kod" and not re.fullmatch(r"[A-Z0-9]{3,6}", anahtar):
                    return "Hisse kodu 3-6 harf/rakam olmalı (ör. AEFES)."
                k[tur][anahtar] = {"deger": deger, "neden": str(neden or "")[:200], "t": time.time(), "kim": kullanici}
            V._yaz("kara_liste", {"t": time.time(), "veri": k})
        return None

    def kara_mi(self, kod, ev=None):
        """Kara listede ise gerekce metni, degilse None. Sektor/ad bilgisi alinamiyorsa sadece kod kontrolu yapilir (ust katman ayrica dener)."""
        k, kod = self.kara(), str(kod or "").upper()
        if kod in k["kod"]:
            return f"{kod} kara listede" + (f" ({k['kod'][kod]['neden']})" if k["kod"][kod]["neden"] else "")
        e = (ev if ev is not None else (V.evren()["veri"] or {})).get(kod) or {}
        sek, ad = _tr(e.get("sektor")), _tr(e.get("ad"))
        if sek and sek in k["sektor"]:
            return f"{e.get('sektor')} sektörü kara listede" + (f" ({k['sektor'][sek]['neden']})" if k["sektor"][sek]["neden"] else "")
        for a, v in k["ad"].items():
            if ad and a in ad:
                return f"şirket adı '{v['deger']}' içeriyor, kara listede" + (f" ({v['neden']})" if v["neden"] else "")
        return None

    def kara_ekran(self):
        """Kara liste + her kaydin kapsadigi hisseler + engellenen girisimler."""
        k, ev = self.kara(), V.evren()["veri"] or {}
        kapsam = {}
        for kod, e in ev.items():
            sek, ad = _tr(e.get("sektor")), _tr(e.get("ad"))
            if kod in k["kod"]:
                kapsam.setdefault("kod:" + kod, []).append(kod)
            if sek in k["sektor"]:
                kapsam.setdefault("sektor:" + sek, []).append(kod)
            for a in k["ad"]:
                if a in ad:
                    kapsam.setdefault("ad:" + a, []).append(kod)
        return {"liste": k, "kapsam": kapsam, "engellenen": self.engellenenler(), "sektorler": sorted({e["sektor"] for e in ev.values()})}

    def _kayit(self, kod, neden, kaynak, tutar):
        """Engellenen islem girisimi kalici kayda."""
        os.makedirs(V.DIR, exist_ok=True)
        with self.kilit, open(os.path.join(V.DIR, "kara_liste_kayit.jsonl"), "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"t": time.time(), "kod": kod, "neden": neden, "kaynak": kaynak, "tutar": tutar}, ensure_ascii=False) + "\n")
        log.warning("borsa kara liste engeli: %s %s (%s)", kod, neden, kaynak)

    def engellenenler(self, n=100):
        try:
            with open(os.path.join(V.DIR, "kara_liste_kayit.jsonl"), encoding="utf-8") as fh:
                return [json.loads(x) for x in fh.read().split("\n")[-n - 1:] if x.strip()][::-1]
        except (OSError, ValueError):
            return []

    # ---------------- sanal portfoy ----------------
    def _pf(self):
        return (V._oku("portfoy") or {}).get("veri") or {"nakit": C.BORSA_BASLANGIC_TL, "baslangic": C.BORSA_BASLANGIC_TL, "t0": time.time(),
                                                           "poz": {}, "islemler": [], "cid": [], "xu100_0": None}

    def _pf_yaz(self, pf):
        V._yaz("portfoy", {"t": time.time(), "veri": pf})

    def _son_fiyat(self, kod):
        """(fiyat, sebep). Fiyat guncel ve guvenilir degilse (None, neden): eski veriyle sanal islem de yapilmaz."""
        f = V.fiyat(kod)
        d = f["veri"]
        if not d or not d.get("son") or not d.get("son_t"):
            return None, "fiyat alınamadı" + (f" ({f['hata']})" if f["hata"] else "")
        if f["bayat"] or time.time() - d["son_t"] > 5 * 86400:
            return None, f"fiyat güncel değil (son veri {time.strftime('%d.%m.%Y %H:%M', time.localtime(d['son_t']))})"
        return float(d["son"]), None

    def _deger(self, pf):
        """(toplam, {kod: (fiyat|None, deger)}). Fiyati alinamayan pozisyon maliyetinden sayilir."""
        fy = {}
        for kod, p in pf["poz"].items():
            px, _ = self._son_fiyat(kod)
            fy[kod] = (px, p["adet"] * (px or p["maliyet"]))
        return pf["nakit"] + sum(v for _, v in fy.values()), fy

    def _kapi(self, kod, tutar, kaynak):
        """Alimdan once ZORUNLU kontroller. Red sebebi veya (fiyat, adet). Ilk kontrol kara listedir; bu fonksiyon atlanarak pozisyon acilamaz."""
        kod = str(kod or "").upper()
        ev = V.evren()["veri"] or {}
        kara = self.kara_mi(kod, ev)
        if kara:
            self._kayit(kod, kara, kaynak, tutar)
            return f"İşlem engellendi: {kara}."
        haber = H.engel_mi(kod)
        if haber:
            self._kayit(kod, haber, kaynak, tutar)
            return f"İşlem engellendi: {haber}."
        if kod not in ev:
            return "Hisse listede yok" if ev else "Hisse listesi alınamadı: sektör/kara liste kontrolü yapılamadığı için işlem yapılmadı."
        if kaynak != "elle" and not C.BORSA_OTO:
            return "Otomatik alım kapalı (geçmiş testi geçen bir strateji olmadan açılmaz)."
        if not isinstance(tutar, (int, float)) or not math.isfinite(tutar) or tutar < C.BORSA_MIN_TUTAR:
            return f"Tutar en az {C.BORSA_MIN_TUTAR:g} TL olmalı."
        px, neden = self._son_fiyat(kod)
        if not px:
            return f"İşlem yapılmadı: {neden}."
        al = px * (1 + C.BORSA_KAYMA)
        adet = math.floor(tutar / (al * (1 + C.BORSA_KOMISYON)))
        if adet < 1:
            return "Tutar bir adet hisseye yetmiyor."
        return al, adet

    def al(self, kod, tutar, kaynak="elle", cid=None):
        """Sanal alim (TL). Hata metni veya None. Tek giris yolu: kara liste, fiyat tazeligi, limitler burada."""
        kod = str(kod or "").upper()
        with self.kilit:
            pf = self._pf()
            if cid and cid in pf["cid"]:
                return "Bu emir zaten işlendi (çift emir engellendi)."
            r = self._kapi(kod, tutar, kaynak)
            if isinstance(r, str):
                return r
            al, adet = r
            toplam, _ = self._deger(pf)
            mal = adet * al * (1 + C.BORSA_KOMISYON)
            mevcut = pf["poz"].get(kod, {"adet": 0, "maliyet": 0.0})
            if (mevcut["adet"] + adet) * al > toplam * C.BORSA_TEK_HISSE_PCT / 100:
                return f"Tek hisse sınırı: bir hisse portföyün en çok %{C.BORSA_TEK_HISSE_PCT:g}'i olabilir."
            if mal > pf["nakit"]:
                return f"Nakit yetersiz (gereken {mal:,.0f} TL, eldeki {pf['nakit']:,.0f} TL)."
            if self.kara_mi(kod):  # son kontrol: kayit aninda yeniden (arada listeye eklenmis olabilir)
                self._kayit(kod, self.kara_mi(kod), kaynak, tutar)
                return "İşlem engellendi: hisse kara listede."
            pf["nakit"] -= mal
            pf["poz"][kod] = {"adet": mevcut["adet"] + adet, "maliyet": (mevcut["adet"] * mevcut["maliyet"] + mal) / (mevcut["adet"] + adet),
                              "t": mevcut.get("t") or time.time()}
            self._isle(pf, kod, "AL", adet, al, adet * al * C.BORSA_KOMISYON, kaynak, cid)
        return None

    def sat(self, kod, adet=None, kaynak="elle", onay=False, cid=None):
        """Sanal satis. Kara listedeki eldeki hisse otomatik satilmaz: kaynak 'elle' degilse onay=True gerekir."""
        kod = str(kod or "").upper()
        with self.kilit:
            pf = self._pf()
            if cid and cid in pf["cid"]:
                return "Bu emir zaten işlendi (çift emir engellendi)."
            p = pf["poz"].get(kod)
            if not p:
                return "Bu hissede pozisyon yok."
            if kaynak != "elle" and not onay:
                return "Otomatik satış için onayınız gerekir."
            adet = p["adet"] if adet is None else int(adet)
            if not 1 <= adet <= p["adet"]:
                return "Geçersiz adet."
            px, neden = self._son_fiyat(kod)
            if not px:
                return f"İşlem yapılmadı: {neden}."
            sat_px = px * (1 - C.BORSA_KAYMA)
            kom = adet * sat_px * C.BORSA_KOMISYON
            pf["nakit"] += adet * sat_px - kom
            kz = adet * (sat_px - p["maliyet"]) - kom
            if adet == p["adet"]:
                del pf["poz"][kod]
            else:
                p["adet"] -= adet
            self._isle(pf, kod, "SAT", adet, sat_px, kom, kaynak, cid, kz)
        return None

    def _isle(self, pf, kod, yon, adet, px, kom, kaynak, cid, kz=None):
        pf["islemler"] = (pf["islemler"] + [{"t": time.time(), "kod": kod, "yon": yon, "adet": adet, "fiyat": px, "komisyon": kom, "kaynak": kaynak,
                                             "kz": kz}])[-500:]
        if cid:
            pf["cid"] = (pf["cid"] + [cid])[-500:]
        if pf.get("xu100_0") is None:
            pf["xu100_0"] = ((V.makro()["veri"] or {}).get("XU100") or {}).get("son")
        self._pf_yaz(pf)

    def portfoy(self):
        with self.kilit:
            pf = self._pf()
            toplam, fy = self._deger(pf)
        ev = V.evren()["veri"] or {}
        xu = ((V.makro()["veri"] or {}).get("XU100") or {}).get("son")
        poz = [{"kod": k, "ad": ev.get(k, {}).get("ad", k), "adet": p["adet"], "maliyet": p["maliyet"], "fiyat": fy[k][0], "deger": fy[k][1],
                "kz": fy[k][1] - p["adet"] * p["maliyet"], "kz_pct": (fy[k][1] / (p["adet"] * p["maliyet"]) - 1) * 100,
                "pay": fy[k][1] / toplam * 100 if toplam else 0, "fiyat_yok": fy[k][0] is None,
                "kara": self.kara_mi(k, ev)} for k, p in pf["poz"].items()]
        return {"toplam": toplam, "nakit": pf["nakit"], "baslangic": pf["baslangic"], "getiri_pct": (toplam / pf["baslangic"] - 1) * 100,
                "xu100_pct": (xu / pf["xu100_0"] - 1) * 100 if xu and pf.get("xu100_0") else None, "t0": pf.get("t0"),
                "poz": sorted(poz, key=lambda x: -x["deger"]), "islemler": pf["islemler"][::-1][:100],
                "komisyon": sum(x["komisyon"] for x in pf["islemler"]), "sinir_pct": C.BORSA_TEK_HISSE_PCT,
                "kara_uyari": [f"{x['kod']}: {x['kara']}. Satış sizin onayınıza bağlı; otomatik satılmaz." for x in poz if x["kara"]]}

    def portfoy_sifirla(self):
        with self.kilit:
            V._yaz("portfoy", {"t": time.time(), "veri": None})

    # ---------------- ekran verisi ----------------
    def liste(self):
        """Tarama tablosu: her hisse tek satir (kara listedekiler isaretli; puanlari gosterilir ama firsat sayilmaz)."""
        e, eng = V.evren(), H.engeller()
        ev, out = e["veri"] or {}, []
        for kod, x in ev.items():
            o = self.ozet.get(kod) or {}
            orn, tk, pn = o.get("oran") or {}, o.get("teknik") or {}, o.get("puan") or {}
            out.append({"kod": kod, "ad": x["ad"], "sektor": x["sektor"], "fiyat": x["kapanis"], "pd_mn": x["pd_mn"], "puan": pn.get("puan"),
                        "guven": pn.get("guven"), "bilesen": pn.get("bilesen"), "fk": orn.get("fk"), "pd_dd": orn.get("pd_dd"),
                        "fd_favok": orn.get("fd_favok"), "roe": orn.get("roe"), "net_marj": orn.get("net_marj"), "temettu": orn.get("temettu_verimi"),
                        "getiri_1a": tk.get("getiri_1a"), "getiri_12a": tk.get("getiri_12a"), "teknik": tk.get("etiket"), "trend": tk.get("trend"),
                        "hacim_tl": tk.get("hacim_tl"), "kara": self.kara_mi(kod, ev), "engel": (eng.get(kod) or {}).get("etiket"),
                        "kalite": o.get("kalite") or []})
        return {"satirlar": out, "evren_t": e["t"], "evren_hata": e["hata"], "ozet_t": self.ozet_t, "ilerleme": self.ilerleme,
                "sektorler": sorted({x["sektor"] for x in ev.values()}), "seans": V.seans_acik()}

    def firsatlar(self, n=20):
        """En yuksek puanlilar: guven dusuk/yetersiz, likiditesi zayif, KARA LISTEDEKI ve haber engelli hisseler haric."""
        s = [x for x in self.liste()["satirlar"] if x["puan"] is not None and x["guven"] in ("yüksek", "orta") and not x["kara"] and not x["engel"]
             and (x["hacim_tl"] or 0) >= C.BORSA_MIN_HACIM_TL]
        return sorted(s, key=lambda x: -x["puan"])[:n]

    def detay(self, kod):
        kod = str(kod or "").upper()
        ev = (V.evren()["veri"] or {}).get(kod)
        if not ev:
            return {"hata": "Hisse bulunamadı."}
        fy, tb, kp = V.fiyat(kod), V.tablo(kod), V.kap(5)
        o = self.ozet.get(kod)
        if not o:  # tarama bu hisseye gelmediyse aninda hesapla (sektor yuzdeligi/puan tarama bitince olusur)
            o = self._hisse(kod, ev)
        tablo = None
        if tb["veri"]:
            t, kodlar = tb["veri"], BA.K.get(tb["veri"]["grup"], {})
            tablo = {"grup": t["grup"], "donemler": [f"{y}/{a}" for y, a in t["donemler"][:8]],
                     "satirlar": [{"ad": t["kalemler"][c]["ad"].strip(), "v": t["kalemler"][c]["v"][:8], "kumulatif": k in BA.AKIS}
                                  for k, c in kodlar.items() if c in t["kalemler"]]}
        return {"kod": kod, "ev": ev, "oran": o["oran"], "eksik": o.get("eksik"), "uyari": o.get("uyari"), "teknik": o.get("teknik"),
                "yuzde": o.get("yuzde") or {}, "puan": o.get("puan"), "adlar": BA.AD,
                "mumlar": [{"time": m[0], "open": m[1], "high": m[2], "low": m[3], "close": m[4], "volume": m[6]} for m in ((fy["veri"] or {}).get("mumlar") or [])[-750:]],
                "temettu": ((fy["veri"] or {}).get("temettu") or [])[-12:], "bolunme": ((fy["veri"] or {}).get("bolunme") or [])[-6:], "tablo": tablo,
                "kap": [y for y in ({**x, **H.sinifla(x)} for x in (kp["veri"] or []) if kod in x["kodlar"]) if y["siddet"] != "gurultu"][:30],
                "kara": self.kara_mi(kod), "haber_engel": H.engel_mi(kod),
                "kaynak": [{"ad": ad, "kaynak": x["kaynak"], "t": x["t"], "bayat": x["bayat"], "hata": x["hata"]}
                           for ad, x in (("Fiyat", fy), ("Mali tablo", tb), ("KAP bildirimleri", kp))]}

    def ozet_ekran(self):
        m, hd = V.makro(), H.durum()
        return {"makro": {k: {a: b for a, b in v.items() if a != "seri"} for k, v in (m["veri"] or {}).items()}, "makro_t": m["t"],
                "makro_hata": m["hata"], "kap": H.son(40), "kap_t": hd["t"], "kap_hata": hd["hata"], "haber_engel": hd["engeller"],
                "firsatlar": self.firsatlar(12),
                "ilerleme": self.ilerleme, "ozet_t": self.ozet_t, "seans": V.seans_acik()}
