"""KAP haber motoru (BIST). Bildirimler hafta ici 08-23 arasi dakikada bir (aksam bildirimleri dahil), diger saatlerde 10 dakikada bir
okunur; resmi konu basligi + ozet metniyle kural tablosundan siniflanir ve olay kaydina yazilir (haber_olay.jsonl, ogrenme bunu kullanir).
Hizli tepki SAVUNMADADIR: SPK tedbiri, VBTS, gozalti pazari, kottan cikma, konkordato, iflas bildiriminde o hissede yeni alim kapanir
(Borsa._kapi, kara listeden hemen sonra). 5'ten fazla hisse sayan toplu duyurular engel koymaz. Olumlu haber alim yaptirmaz.
Eldeki hisse otomatik satilmaz: Telegram uyarisi gider. KAP metni sirket metnidir: veri olarak siniflanir, talimat sayilmaz."""
import collections
import datetime
import json
import logging
import os
import re
import threading
import time

import borsa_veri as V

log = logging.getLogger("bot")
SEANS_SN, GECE_SN = 60, 600
TAZE = 2 * 3600   # bundan eski bildirim Telegram'a gitmez (ilk acilista son 2 gun sessizce islenir, engeller yine konur)
TOPLU = 5         # bundan fazla hisse kodu iceren duyuru hisse bazli engel koymaz
OLAY = "haber_olay.jsonl"
_tr = lambda s: str(s or "").replace("İ", "i").replace("I", "ı").lower()

# (konu deseni, konu+ozet deseni, tur, etiket, yon, siddet, engel_gun). Ilk eslesen kazanir; desenler kucuk harf.
# siddet: engel (alim kapanir) > uyari > olay (ogrenme kaydi + portfoydeyse bildirim) > bilgi (sadece hisse detayinda) > gurultu (atilir)
KURAL = [
    (r"spk işlem yasağı nedeniyle", None, "kisi_yasagi", "", 0, "gurultu", 0),  # kisilerin yasagi; listelenen ~100 hisse icin olumsuz degil
    (r"sermaye piyasası kurulu tedbir", None, "spk_tedbir", "SPK tedbir kararı", -1, "engel", 30),
    (None, r"volatilite bazlı tedbir", "vbts", "Volatilite bazlı tedbir (VBTS)", -1, "engel", 30),
    (None, r"^(?!.*(borçlanma|kira sertifika|varant|yatırım fon)).*gözaltı pazar", "gozalti", "Gözaltı pazarına alındı", -1, "engel", 365),
    (None, r"kot(tan|undan|asyondan) çıkar", "kottan_cikma", "Borsa kotundan çıkma", -1, "engel", 365),
    (None, r"konkordato", "konkordato", "Konkordato", -1, "engel", 30),
    (None, r"\biflas", "iflas", "İflas", -1, "engel", 30),
    (r"devre kesici|pay dışında sermaye|pay dışı sermaye|piyasa yapıcılığı|likidite sağlayıcılık|varant|sorumluluk beyanı|faaliyet raporu|"
     r"kurumsal yönetim|portföy sınırlamaları|haftalık rapor|tertip|ihraç belgesi|ihraç tavanı|katılım finansı|yatırımcı raporu|borçlanma araç|"
     r"kira sertifika|hak kullanım|mali hak|fiili dolaşım|merkezi kayıt|izahname|değerleme raporu|esas sözleşme|fonun kullanım|genel bilgi formu|"
     r"bağımsız denetim|genel kurul|komiteleri|şirket merkezi|halka arz|fon kurucusu", None, "gurultu", "", 0, "gurultu", 0),
    (r"bistech|duyurusu", r"varant|borçlanma|kira sertifika|hak kullanım|birincil piyasa|sertifika", "gurultu", "", 0, "gurultu", 0),
    (r"geri alınan payların elden çıkarılması", None, "geri_alim_satis", "Geri alınan payların satışı", -1, "olay", 0),
    (r"geri alın", r"başlat|yönetim kurulu kararı", "geri_alim_program", "Pay geri alım programı", 1, "olay", 0),
    (r"geri alın", None, "geri_alim", "Pay geri alımı (günlük)", 1, "bilgi", 0),
    (r"yeni iş ilişkisi", None, "yeni_is", "Yeni iş / sipariş", 1, "olay", 0),
    (r"ihale", r"kazan|uhdesinde|en avantajlı|sözleşme imzala", "ihale_kazanim", "İhale kazanımı", 1, "olay", 0),
    (r"ihale", None, "ihale", "İhale süreci", 0, "olay", 0),
    (r"kar payı|kâr payı", None, "kar_payi", "Kâr payı (temettü)", 1, "olay", 0),
    (r"sermaye artırım", r"bedelsiz", "bedelsiz", "Bedelsiz sermaye artırımı", 1, "olay", 0),
    (r"sermaye artırım", r"bedelli|rüçhan", "bedelli", "Bedelli sermaye artırımı", -1, "olay", 0),
    (r"sermaye artırım|sermaye tavanı", None, "sermaye", "Sermaye işlemi", 0, "bilgi", 0),
    (r"kredi derecelendirme", r"yükselt|pozitif|artırıl", "not_artis", "Kredi notu / görünüm yükseldi", 1, "olay", 0),
    (r"kredi derecelendirme", r"düşür|negatif|indiril", "not_dusus", "Kredi notu / görünüm düştü", -1, "olay", 0),
    (r"kredi derecelendirme", None, "not_teyit", "Kredi notu teyit", 0, "bilgi", 0),
    (r"dava", r"reddi|reddedil|\blehine\b|\blehe\b", "dava_lehte", "Dava lehte sonuçlandı", 1, "olay", 0),
    (r"dava", None, "dava", "Dava", -1, "olay", 0),
    (r"haber ve söylenti", None, "soylenti", "Haber / söylenti açıklaması", 0, "olay", 0),
    (r"birleşme|bölünme", None, "birlesme", "Birleşme / bölünme", 0, "bilgi", 0),
    (r"varlık sat", None, "varlik_satisi", "Varlık satışı", 0, "olay", 0),
    (r"finansal tablo ve-veya dipnot|finansal tablolarda değişiklik", None, "tablo_duzeltme", "Finansal tablo düzeltmesi", 0, "uyari", 0),
    (r"pay alım satım", None, "pay_alim_satim", "Pay alım satım bildirimi", 0, "bilgi", 0),
    (r"geleceğe dönük", None, "beklenti", "Geleceğe dönük beklenti", 0, "olay", 0),
    (r"kamuyu aydınlatma platformu duyurusu", r"12-\(4\)", "olagandisi", "Olağandışı fiyat / miktar hareketi", 0, "uyari", 0),
    (None, r"net aktif değer", "nav_asim", "Fiyat net aktif değerin 2 katını aştı", -1, "uyari", 0),
    (None, r"işlem sıra\w* .*(durdur|kapat)", "sira_durdurma", "İşlem sırası durduruldu", 0, "uyari", 0),
    (r"^finansal rapor", None, "finansal_rapor", "Finansal rapor", 0, "olay", 0),
    (r"özel durum açıklaması", None, "ozel_durum", "Özel durum açıklaması", 0, "olay", 0),
]
_KURAL = [(re.compile(k) if k else None, re.compile(m) if m else None, *r) for k, m, *r in KURAL]
_KILIT = threading.Lock()
DURUM = {"son": collections.deque(maxlen=300), "yuklendi": False, "t": 0, "hata": None}


def sinifla(x):
    """KAP satiri -> {tur, etiket, yon (-1/0/1), siddet, gun}. Sadece resmi konu basligi ve ozet metnine bakar."""
    konu, metin = _tr(x.get("konu")), _tr(f"{x.get('konu')} | {x.get('ozet')}")
    for kd, md, tur, etiket, yon, siddet, gun in _KURAL:
        if (kd is None or kd.search(konu)) and (md is None or md.search(metin)):
            return {"tur": tur, "etiket": etiket, "yon": yon, "siddet": siddet, "gun": gun}
    oda = x.get("sinif") == "ODA"
    return {"tur": "diger", "etiket": "Özel durum" if oda else "Diğer bildirim", "yon": 0, "siddet": "olay" if oda else "bilgi", "gun": 0}


def tarih(t, gun=False):
    return datetime.datetime.fromtimestamp(t, V.TR).strftime("%d.%m.%Y" if gun else "%d.%m.%Y %H:%M")


def _yol():
    return os.path.join(V.DIR, OLAY)


def _yukle():
    if DURUM["yuklendi"]:
        return
    DURUM["yuklendi"] = True
    try:
        with open(_yol(), encoding="utf-8") as fh:
            for s in collections.deque(fh, maxlen=300):
                try:
                    DURUM["son"].append(json.loads(s))
                except ValueError:
                    pass
    except OSError:
        pass


# ---------------- engel ----------------
def engeller(now=None, hepsi=False):
    e = (V._oku("haber_engel") or {}).get("veri") or {}
    now = now or time.time()
    return e if hepsi else {k: v for k, v in e.items() if v["son"] > now}


def engel_mi(kod, now=None):
    """Haber engeli varsa gerekce metni, yoksa None."""
    v = engeller(now).get(str(kod or "").upper())
    return f"{v['etiket']} (KAP {tarih(v['t'])}; yeni alım {tarih(v['son'], True)} tarihine kadar kapalı)" if v else None


def _engel_koy(kod, o):
    now = time.time()
    e = {k: v for k, v in engeller(hepsi=True).items() if v["son"] > now}
    son = o["t"] + o["gun"] * 86400
    if son > now and son > (e.get(kod) or {}).get("son", 0):
        e[kod] = {"son": son, "etiket": o["etiket"], "tur": o["tur"], "t": o["t"], "url": o.get("url"), "konu": o.get("konu")}
        V._yaz("haber_engel", {"t": now, "veri": e})
        log.warning("borsa haber engeli: %s %s", kod, o["etiket"])


def engel_kaldir(kod):
    e = engeller(hepsi=True)
    if e.pop(str(kod or "").upper(), None) is None:
        return "Bu hissede haber engeli yok."
    V._yaz("haber_engel", {"t": time.time(), "veri": e})
    return None


# ---------------- okuma ve tepki ----------------
def tick(borsa, now=None):
    """Yeni KAP bildirimlerini isler. Donus: kayda giren yeni olaylar (gurultu haric)."""
    now = now or time.time()
    with _KILIT:
        _yukle()
        d = V._oku("haber_durum") or {"gorulen": [], "t": 0}
        try:
            satir = V._kap(1 if d["t"] else 2)
        except Exception as e:  # ag/bicim hatasi: sonraki turda tekrar denenir, gorulen ilerlemez
            DURUM["hata"] = str(e)[:160]
            log.warning("kap haber: %s", DURUM["hata"])
            return []
        gor, yeni = set(d["gorulen"]), []
        for x in sorted(satir, key=lambda r: r["t"]):
            i = str(x.get("id") or "")
            if not i or i in gor:
                continue
            gor.add(i)
            d["gorulen"].append(i)
            o = {**x, **sinifla(x), "alindi": now}
            if o["siddet"] != "gurultu":
                yeni.append(o)
        if yeni:
            os.makedirs(V.DIR, exist_ok=True)
            with open(_yol(), "a", encoding="utf-8") as fh:
                fh.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in yeni)
            DURUM["son"].extend(yeni)
        d["gorulen"], d["t"] = d["gorulen"][-6000:], now
        V._yaz("haber_durum", d)
        DURUM.update(t=now, hata=None)
    for o in yeni:
        try:
            tepki(borsa, o, now)
        except Exception:
            log.exception("kap haber tepki")
    return yeni


def tepki(borsa, o, now=None):
    now = now or time.time()
    ev = V.evren()["veri"] or {}
    kodlar = [k for k in o["kodlar"] if not ev or k in ev]
    engel = o["siddet"] == "engel" and 0 < len(o["kodlar"]) <= TOPLU
    if engel:
        for k in kodlar:
            _engel_koy(k, o)
    if now - o["t"] > TAZE or o["siddet"] not in ("engel", "uyari", "olay"):
        return
    eldeki = set(borsa._pf()["poz"]) & set(kodlar)
    if (engel and kodlar) or eldeki:
        bildir(mesaj(o, kodlar, eldeki, engel))


def mesaj(o, kodlar, eldeki, engel):
    s = [f"{', '.join(kodlar[:3]) or o.get('sirket')} · {o['etiket']}" + {1: " ↑ olumlu", -1: " ↓ olumsuz"}.get(o["yon"], "")]
    if engel:
        s.append(f"Yeni alım {tarih(o['t'] + o['gun'] * 86400, True)} tarihine kadar kapalı.")
    if eldeki:
        s.append(f"Portföyde: {', '.join(sorted(eldeki))}. Otomatik satılmaz, karar sizin.")
    s += [f"{o.get('konu')}: {(o.get('ozet') or '')[:300]}", o.get("url") or ""]
    return "\n".join(x for x in s if x)


def bildir(msg):
    try:
        import telegram_bot
        telegram_bot.notify(msg, "haber")
    except Exception:
        log.exception("kap haber telegram")


def aralik(now=None):
    d = datetime.datetime.fromtimestamp(now or time.time(), V.TR)
    return SEANS_SN if d.weekday() < 5 and 8 <= d.hour < 23 else GECE_SN


def dongu(borsa, dur):
    while not dur.is_set():
        try:
            tick(borsa)
        except Exception:
            log.exception("kap haber")
        dur.wait(aralik())


# ---------------- ekran ----------------
def son(n=40, siddet=("engel", "uyari", "olay")):
    _yukle()
    return [o for o in reversed(DURUM["son"]) if o["siddet"] in siddet][:n]


def durum():
    return {"t": DURUM["t"], "hata": DURUM["hata"], "engeller": engeller()}
