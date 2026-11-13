"""Borsa (BIST) veri katmani: ucretsiz ve resmi olmayan kaynaklar, hepsi salt okuma. Her sonuc kaynagini, alindigi ani ve
bayat/hatali olup olmadigini tasir: {"veri", "t", "kaynak", "bayat", "hata"}. Kaynak cevap vermezse son iyi veri 'bayat' isaretiyle
doner; hic veri yoksa veri None olur (ust katman kesin sonuc uretmez).
  evren  : Is Yatirim 'Temel Degerler' tablosu (kod, ad, sektor, kapanis, piyasa degeri, halka aciklik, sermaye)
  fiyat  : Yahoo Finance gunluk mumlar (5 yil; temettu, bolunme, duzeltilmis kapanis). BIST verisi ~15 dk gecikmeli.
  tablo  : Is Yatirim mali tablolar (son 12 ceyrek; sanayi XI_29, banka UFRS_K, sigorta UFRS). Gelir/nakit kalemleri yilbasindan kumulatif.
  kap    : KAP bildirimleri (son gunler, tum sirketler)
  makro  : Yahoo: BIST100/30, USD/TRY, EUR/TRY, ons altin/gumus; gram altin = ons x USD/TRY / 31,1035
Emir, hesap, anahtar yok. Kripto motorundan bagimsizdir."""
import datetime
import json
import logging
import os
import re
import threading
import time

import requests

import config as C

log = logging.getLogger("bot")
DIR = os.path.join(C.APP_DIR, "borsa")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json,text/html,*/*", "Accept-Language": "tr-TR,tr;q=0.9"}
ISY = "https://www.isyatirim.com.tr"
YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/"
KAP = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
GRUPLAR = ("XI_29", "UFRS_K", "UFRS")  # sanayi/ticaret, banka (konsolide), sigorta ve diger finans
TR = datetime.timezone(datetime.timedelta(hours=3))
_KILIT, _SON = threading.Lock(), {}  # kaynak basina istek araligi (kaynagi yormamak icin)
ARALIK = {"isy": 0.6, "yahoo": 0.25, "kap": 2.0}
MAKRO = {"XU100": "XU100.IS", "XU030": "XU030.IS", "USDTRY": "USDTRY=X", "EURTRY": "EURTRY=X", "ONS_ALTIN": "GC=F", "ONS_GUMUS": "SI=F"}


def seans_acik(now=None):
    """BIST pay piyasasi surekli islem: hafta ici 10:00-18:00 (TR). Resmi tatiller bilinmez (fiyat tazeligi ayrica kontrol edilir)."""
    d = datetime.datetime.fromtimestamp(now or time.time(), TR)
    return d.weekday() < 5 and (10, 0) <= (d.hour, d.minute) < (18, 0)


def _istek(kaynak, method, url, **kw):
    with _KILIT:
        bekle = ARALIK[kaynak] - (time.time() - _SON.get(kaynak, 0))
        if bekle > 0:
            time.sleep(bekle)
        _SON[kaynak] = time.time()
    r = requests.request(method, url, headers={**UA, **kw.pop("headers", {})}, timeout=kw.pop("timeout", 25), **kw)
    r.raise_for_status()
    return r


def _dosya(ad):
    return os.path.join(DIR, re.sub(r"[^A-Za-z0-9_.-]", "_", ad) + ".json")


def _oku(ad):
    try:
        with open(_dosya(ad), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _yaz(ad, d):
    os.makedirs(DIR, exist_ok=True)
    tmp = _dosya(ad) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, _dosya(ad))


def onbellek(ad, ttl, kaynak, getir, bayat_sn=None):
    """ttl icindeyse diskten; degilse getir(). Hata olursa son iyi veri bayat isaretiyle doner (hic yoksa veri None)."""
    eski = _oku(ad)
    now = time.time()
    if eski and now - eski["t"] < ttl:
        return {"veri": eski["veri"], "t": eski["t"], "kaynak": kaynak, "bayat": False, "hata": None}
    try:
        veri = getir()
        if veri is None or veri == [] or veri == {}:
            raise ValueError("kaynak boş cevap verdi")
        _yaz(ad, {"t": now, "veri": veri})
        return {"veri": veri, "t": now, "kaynak": kaynak, "bayat": False, "hata": None}
    except Exception as e:  # ag, bicim degisikligi, bos cevap: eski veriyle devam, acikca isaretli
        log.warning("borsa veri %s: %s", ad, str(e)[:160])
        if eski:
            return {"veri": eski["veri"], "t": eski["t"], "kaynak": kaynak, "bayat": now - eski["t"] > (bayat_sn or ttl * 3), "hata": str(e)[:160]}
        return {"veri": None, "t": 0, "kaynak": kaynak, "bayat": True, "hata": str(e)[:160]}


def sayi(s):
    """'2.146,5' -> 2146.5; bos/tire -> None."""
    s = re.sub(r"<[^>]+>|\s|&nbsp;", "", str(s or ""))
    if not s or s in ("-", "A/D"):
        return None
    try:
        return float(s.replace(".", "").replace(",", "."))
    except ValueError:
        return None


# ---------------- evren ----------------
def _evren():
    t = _istek("isy", "GET", ISY + "/tr-tr/analiz/hisse/Sayfalar/Temel-Degerler-Ve-Oranlar.aspx", timeout=40).text
    m = re.search(r'<table[^>]*id="summaryBasicData".*?</table>', t, re.S)
    if not m:
        raise ValueError("hisse tablosu bulunamadı (sayfa yapısı değişmiş olabilir)")
    out = {}
    for satir in re.findall(r"<tr[^>]*>(.*?)</tr>", m.group(0), re.S):
        h = [re.sub(r"<[^>]+>", "", x).strip() for x in re.findall(r"<td[^>]*>(.*?)</td>", satir, re.S)]
        if len(h) < 8 or not re.fullmatch(r"[A-Z0-9]{3,6}", h[0]):
            continue
        out[h[0]] = {"kod": h[0], "ad": re.sub(r"\s+", " ", h[1]), "sektor": re.sub(r"\s+", " ", h[2]) or "Diğer", "kapanis": sayi(h[3]),
                     "pd_mn": sayi(h[4]), "pd_mn_usd": sayi(h[5]), "halka_aciklik": sayi(h[6]), "sermaye_mn": sayi(h[7])}
    if len(out) < 100:
        raise ValueError(f"hisse tablosu eksik ({len(out)} satır)")
    return out


def evren():
    return onbellek("evren", 6 * 3600, "İş Yatırım", _evren, bayat_sn=3 * 86400)


# ---------------- fiyat ----------------
def _yahoo(sembol, aralik="5y"):
    j = _istek("yahoo", "GET", YAHOO + sembol, params={"range": aralik, "interval": "1d", "events": "div,splits"}).json()
    r = (j.get("chart", {}).get("result") or [None])[0]
    if not r or not r.get("timestamp"):
        raise ValueError("fiyat verisi yok")
    q, adj = r["indicators"]["quote"][0], (r["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    mumlar = [[t, q["open"][i], q["high"][i], q["low"][i], q["close"][i], (adj[i] if adj else q["close"][i]), q["volume"][i] or 0]
              for i, t in enumerate(r["timestamp"]) if q["close"][i] is not None and q["open"][i] is not None]
    ev, m = r.get("events") or {}, r["meta"]
    return {"mumlar": mumlar, "temettu": sorted([x["date"], x["amount"]] for x in (ev.get("dividends") or {}).values()),
            "bolunme": sorted([x["date"], x.get("numerator"), x.get("denominator")] for x in (ev.get("splits") or {}).values()),
            "son": m.get("regularMarketPrice"), "son_t": m.get("regularMarketTime"), "para": m.get("currency"), "ad": m.get("longName")}


def fiyat(kod):
    """BIST hissesi (kod: THYAO). Seans icinde 15 dk, disinda 6 saat onbellek."""
    return onbellek("fiyat_" + kod, 900 if seans_acik() else 6 * 3600, "Yahoo Finance (≈15 dk gecikmeli)", lambda: _yahoo(kod + ".IS"),
                    bayat_sn=4 * 86400)


def makro():
    def getir():
        out = {}
        for ad, s in MAKRO.items():
            try:
                y = _yahoo(s, "1y")
                c = [m[4] for m in y["mumlar"]]
                out[ad] = {"son": y["son"] or c[-1], "t": y["son_t"], "onceki": c[-2] if len(c) > 1 else None,
                           "ay": c[-22] if len(c) > 22 else None, "yil": c[0], "seri": [[m[0], m[4]] for m in y["mumlar"][-260:]]}
            except Exception as e:
                log.warning("borsa makro %s: %s", ad, str(e)[:100])
        if "ONS_ALTIN" in out and "USDTRY" in out:  # gram altin (TL): hesaplanan deger, piyasa fiyati degil
            g = lambda k: out["ONS_ALTIN"][k] * out["USDTRY"][k] / 31.1035 if out["ONS_ALTIN"].get(k) and out["USDTRY"].get(k) else None
            out["GRAM_ALTIN"] = {"son": g("son"), "t": min(out["ONS_ALTIN"]["t"] or 0, out["USDTRY"]["t"] or 0), "onceki": g("onceki"),
                                 "ay": g("ay"), "yil": g("yil"), "hesaplanan": True}
        return out
    return onbellek("makro", 600 if seans_acik() else 3600, "Yahoo Finance", getir, bayat_sn=3 * 86400)


# ---------------- mali tablolar ----------------
def donemler(n=12, now=None):
    """Son n ceyrek (yil, ay), yeniden eskiye. Aciklanmamis donemler tabloda bos gelir ve atilir."""
    d = datetime.datetime.fromtimestamp(now or time.time(), TR)
    y, p = d.year, ((d.month - 1) // 3) * 3
    if p == 0:
        y, p = y - 1, 12
    out = []
    for _ in range(n):
        out.append((y, p))
        y, p = (y, p - 3) if p > 3 else (y - 1, 12)
    return out


def _tablo(kod, grup_ipucu=None):
    dl = donemler(12)
    for grup in ([grup_ipucu] if grup_ipucu else []) + [g for g in GRUPLAR if g != grup_ipucu]:
        kalem, bulundu = {}, False
        for k in range(0, 12, 4):
            p = {"companyCode": kod, "exchange": "TRY", "financialGroup": grup}
            for i, (y, a) in enumerate(dl[k:k + 4], 1):
                p[f"year{i}"], p[f"period{i}"] = y, a
            v = _istek("isy", "GET", ISY + "/_layouts/15/IsYatirim.Website/Common/Data.aspx/MaliTablo", params=p).json().get("value") or []
            if not v:
                break
            bulundu = True
            for x in v:
                e = kalem.setdefault(x["itemCode"], {"ad": (x.get("itemDescTr") or "").strip(), "v": [None] * 12})
                for i in range(4):
                    s = x.get(f"value{i + 1}")
                    e["v"][k + i] = float(s) if s not in (None, "") else None
        if bulundu:
            top = kalem.get("1BL") or kalem.get("1Z") or {"v": [None] * 12}  # aktif toplami bos/0 olan donem aciklanmamis
            var = [i for i in range(12) if top["v"][i]]
            if not var:
                continue
            return {"grup": grup, "donemler": [list(dl[i]) for i in var],
                    "kalemler": {c: {"ad": e["ad"], "v": [e["v"][i] for i in var]} for c, e in kalem.items()}}
    raise ValueError("mali tablo bulunamadı")


def tablo(kod):
    eski = _oku("tablo_" + kod)
    return onbellek("tablo_" + kod, 24 * 3600, "İş Yatırım (KAP mali tabloları)",
                    lambda: _tablo(kod, (eski or {}).get("veri", {}).get("grup")), bayat_sn=120 * 86400)


# ---------------- KAP ----------------
def _kap(gun):
    b = datetime.datetime.now(TR)
    govde = {"fromDate": (b - datetime.timedelta(days=gun)).strftime("%Y-%m-%d"), "toDate": b.strftime("%Y-%m-%d"), "memberType": "IGS",
             "mkkMemberOidList": [], "disclosureClass": "", "subjectList": [], "isLate": "", "mainSector": "", "sector": "", "subSector": "",
             "marketOidList": [], "index": "", "bdkReview": "", "bdkMemberOidList": [], "year": "", "term": "", "ruleType": "", "period": "",
             "fromSrc": False, "srcCategory": "", "disclosureIndexList": []}
    j = _istek("kap", "POST", KAP, json=govde, headers={"Content-Type": "application/json"}, timeout=45).json()
    out = []
    for x in j:
        kodlar = [k.strip() for k in re.split(r"[,\s]+", (x.get("stockCodes") or x.get("relatedStocks") or "")) if k.strip()]
        try:
            t = datetime.datetime.strptime(x["publishDate"], "%d.%m.%Y %H:%M:%S").replace(tzinfo=TR).timestamp()
        except (KeyError, ValueError):
            continue
        out.append({"id": x.get("disclosureIndex"), "t": t, "kodlar": kodlar, "sirket": x.get("kapTitle") or "", "konu": x.get("subject") or "", "ozet": (x.get("summary") or "")[:400],
                    "sinif": x.get("disclosureClass"), "tip": x.get("disclosureType"), "gec": bool(x.get("isLate")),
                    "url": f"https://www.kap.org.tr/tr/Bildirim/{x.get('disclosureIndex')}"})
    return sorted(out, key=lambda r: -r["t"])


def kap(gun=5):
    """Son gunlerin KAP bildirimleri. sinif: FR mali rapor, ODA ozel durum, DG diger, DKB devre kesici (gurultu)."""
    return onbellek(f"kap_{gun}", 600 if seans_acik() else 1800, "KAP", lambda: _kap(gun), bayat_sn=2 * 86400)
