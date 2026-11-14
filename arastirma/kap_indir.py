"""KAP olay calismasi icin veri: 2022'den bugune gun gun KAP bildirimleri (borsa_veri/kap/YYYY-MM-DD.json) ve olay gecen hisselerin +
BIST100'un 5 yillik gunluk mumlari (borsa_veri/fiyat/KOD.json, Yahoo). Kaldigi yerden devam eder; bugun ve dun her calistirmada yenilenir.
KAP'i yormamak icin istekler arasi 2 sn. Calistirma: python kap_indir.py [baslangic=2022-01-01]  (~1 saat ilk sefer)"""
import datetime
import json
import os
import re
import sys
import time

import requests

H = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(H, "borsa_veri")
KAP = "https://www.kap.org.tr/tr/api/disclosure/members/byCriteria"
PENCERE = 4
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json", "Accept-Language": "tr-TR,tr;q=0.9"}


def kap_gun(g, g2=None):
    govde = {"fromDate": g, "toDate": g2 or g, "memberType": "IGS", "mkkMemberOidList": [], "disclosureClass": "", "subjectList": [], "isLate": "",
             "mainSector": "", "sector": "", "subSector": "", "marketOidList": [], "index": "", "bdkReview": "", "bdkMemberOidList": [], "year": "",
             "term": "", "ruleType": "", "period": "", "fromSrc": False, "srcCategory": "", "disclosureIndexList": []}
    for deneme in range(8):
        try:
            r = requests.post(KAP, json=govde, headers=UA, timeout=60)
            if r.status_code == 429:  # KAP istek siniri: uzun bekle
                print("  429 bekleniyor", g, flush=True)
                time.sleep(90 * (deneme + 1))
                continue
            r.raise_for_status()
            return [{"id": x.get("disclosureIndex"), "tarih": x.get("publishDate"), "kodlar": x.get("stockCodes") or x.get("relatedStocks") or "",
                     "konu": x.get("subject") or "", "ozet": (x.get("summary") or "")[:400], "sinif": x.get("disclosureClass")} for x in r.json()]
        except (requests.RequestException, ValueError) as e:
            print("  tekrar", g, type(e).__name__, flush=True)
            time.sleep(10 * (deneme + 1))
    raise RuntimeError(f"KAP {g} alinamadi")


def yahoo(sembol):
    r = requests.get("https://query1.finance.yahoo.com/v8/finance/chart/" + sembol, params={"range": "5y", "interval": "1d"}, headers=UA, timeout=30)
    r.raise_for_status()
    x = r.json()["chart"]["result"][0]
    q, adj = x["indicators"]["quote"][0], (x["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    return [[t, q["open"][i], q["high"][i], q["low"][i], q["close"][i], adj[i] if adj else q["close"][i], q["volume"][i] or 0]
            for i, t in enumerate(x["timestamp"]) if q["close"][i] is not None and q["open"][i] is not None]


def main(bas):
    os.makedirs(os.path.join(D, "kap"), exist_ok=True)
    os.makedirs(os.path.join(D, "fiyat"), exist_ok=True)
    g, bugun = datetime.date.fromisoformat(bas), datetime.date.today()
    dosya = lambda x: os.path.join(D, "kap", x.isoformat() + ".json")
    eksik = lambda x: not os.path.exists(dosya(x)) or x >= bugun - datetime.timedelta(days=1)
    n = 0
    while g <= bugun:  # 4 gunluk pencere tek istek; 2000 satir sinirina takilirsa gun gun
        p = [g + datetime.timedelta(days=k) for k in range(PENCERE) if g + datetime.timedelta(days=k) <= bugun]
        if any(eksik(x) for x in p):
            v = kap_gun(p[0].isoformat(), p[-1].isoformat())
            time.sleep(2)
            if len(v) >= 2000:
                for x in p:
                    w = kap_gun(x.isoformat())
                    if len(w) >= 2000:
                        print("  UYARI 2000 satir siniri:", x, flush=True)
                    json.dump(w, open(dosya(x), "w", encoding="utf-8"), ensure_ascii=False)
                    time.sleep(2)
            else:
                for x in p:
                    gs = x.strftime("%d.%m.%Y")
                    json.dump([r for r in v if (r["tarih"] or "").startswith(gs)], open(dosya(x), "w", encoding="utf-8"), ensure_ascii=False)
            n += 1
            if n % 10 == 0:
                print("kap", p[-1], len(v), flush=True)
        g = p[-1] + datetime.timedelta(days=1)
    kodlar = set()
    for f in os.listdir(os.path.join(D, "kap")):
        for x in json.load(open(os.path.join(D, "kap", f), encoding="utf-8")):
            kodlar.update(k for k in re.split(r"[,\s]+", x["kodlar"]) if re.fullmatch(r"[A-Z0-9]{3,6}", k or ""))
    print("fiyat:", len(kodlar), "kod", flush=True)
    for i, k in enumerate(sorted(kodlar) + ["XU100"]):
        f = os.path.join(D, "fiyat", k + ".json")
        if os.path.exists(f) and time.time() - os.path.getmtime(f) < 86400:
            continue
        try:
            json.dump(yahoo(k + ".IS"), open(f, "w"))
        except Exception as e:  # tahvil/fon kodlari Yahoo'da yok: bos dosya, tekrar sorulmaz
            json.dump([], open(f, "w"))
            if not isinstance(e, (requests.HTTPError, KeyError, TypeError, IndexError)):
                print("  fiyat", k, type(e).__name__, flush=True)
        time.sleep(0.3)
        if i % 100 == 0:
            print("fiyat", i, k, flush=True)
    print("BITTI", flush=True)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2022-01-01")
