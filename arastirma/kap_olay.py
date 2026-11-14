"""KAP olay calismasi (kap_indir.py sonrasi): her olay turu bir hipotez; borsa_ogren.Defter ile egitim (ilk 2/3) / test (son 1/3),
gune gore toplanmis, BIST100'e gore fark, maliyet dahil. Kontrol: rastgele gun + rastgele hisse (yontem sapmasizsa reddedilmeli).
Asama 1: tur x ufuk (5, 20) alim iddiasi + olumsuz turlerde kacin iddiasi. Asama 2: reddedilen ve teshisi 'giris' olanlarda egitimden
bulunan kristal kosullari (en cok 3) alt hipotez olarak. Butun testler deneme sayisina girer; sonunda herkes son esikle yargilanir.
Cikti: sonuclar/kap_olay.json + sonuclar/kap_olay.txt. Calistirma: python kap_olay.py"""
import collections
import datetime
import json
import os
import random
import re
import sys

H = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(H))
import borsa_haber as BH
import borsa_ogren as O

D = os.path.join(H, "borsa_veri")
TR = O.TR


def yukle():
    fy = {}
    for f in os.listdir(os.path.join(D, "fiyat")):
        m = json.load(open(os.path.join(D, "fiyat", f)))
        if len(m) > 250:
            fy[f[:-5]] = O.seri(m)
    olay = collections.defaultdict(list)
    for f in sorted(os.listdir(os.path.join(D, "kap"))):
        for x in json.load(open(os.path.join(D, "kap", f), encoding="utf-8")):
            s = BH.sinifla(x)
            if s["siddet"] == "gurultu":
                continue
            try:
                t = datetime.datetime.strptime(x["tarih"], "%d.%m.%Y %H:%M:%S").replace(tzinfo=TR).timestamp()
            except (TypeError, ValueError):
                continue
            for k in {k for k in re.split(r"[,\s]+", x["kodlar"]) if k in fy and k != "XU100"}:
                olay[s["tur"]].append((t, k, s["yon"]))
    return fy, olay


def ornekler(olaylar, fy):
    e, out = fy["XU100"], []
    for t, k, _ in olaylar:
        o = O.ornek(t, fy[k], e)
        if o:
            o["kod"] = k
            out.append(o)
    return O.seyrelt(out)


def main():
    fy, olay = yukle()
    print("hisse", len(fy) - 1, "| olay turu", len(olay), {k: len(v) for k, v in sorted(olay.items(), key=lambda kv: -len(kv[1]))}, flush=True)
    rnd = random.Random(1)
    kodlar = [k for k in fy if k != "XU100"]
    g0, g1 = min(t for v in olay.values() for t, _, _ in v), max(t for v in olay.values() for t, _, _ in v)
    olay["KONTROL_rastgele"] = [(rnd.uniform(g0, g1), rnd.choice(kodlar), 0) for _ in range(3000)]
    orn = {tur: ornekler(v, fy) for tur, v in olay.items()}
    gunler = sorted({x["gun"] for v in orn.values() for x in v})
    kesim = gunler[len(gunler) * 2 // 3]
    print("kesim", kesim, "| ornek", {k: len(v) for k, v in orn.items()}, flush=True)
    df = O.Defter()
    for tur, xs in sorted(orn.items()):
        if len(xs) < 40:
            continue
        for u in (5, 20):  # tur x ufuk basina TEK test; yon egitimden (yon 0 da deneme sayilir: bakildi)
            iddia = O.yon_sec(xs, u, kesim)
            h = df.test_et(f"{tur}_{'al' if iddia > 0 else 'kacin' if iddia < 0 else 'yonsuz'}_{u}", tur, iddia or 1, u, xs, kesim)
            if not iddia:
                h["durum"], h["kristal"] = "red", []
    for h in list(df.h):
        for kr in (h.get("kristal") or [] if h["iddia"] > 0 else []):
            if kr["fark"] > 0:
                df.test_et(f"{h['ad']}|{kr['kosul'][0]}{kr['kosul'][1]}{kr['kosul'][2]:.4g}" if not isinstance(kr["kosul"][2], bool)
                           else f"{h['ad']}|{kr['kosul'][0]}={kr['kosul'][2]}", h["tur"], 1, h["ufuk"], orn[h["tur"]], kesim,
                           kosul=tuple(kr["kosul"]), ust=h["ad"])
    df.yeniden_karar()
    os.makedirs(os.path.join(H, "sonuclar"), exist_ok=True)
    json.dump({"kesim": kesim, "deneme": df.deneme, "esik": O.esik(df.deneme), "maliyet": O.MALIYET, "hipotez": df.h},
              open(os.path.join(H, "sonuclar", "kap_olay.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1, default=str)
    L = [f"KAP olay calismasi | {gunler[0]} - {gunler[-1]} | kesim {kesim} | deneme {df.deneme} | esik t >= {O.esik(df.deneme)} | maliyet %{O.MALIYET * 100:.1f}", ""]
    for h in sorted(df.h, key=lambda h: ({"kanitli": 0, "red": 1, "emekli": 2, "yetersiz": 3}[h["durum"]], h["ad"])):
        e, s = h["olcum"]["egitim"], h["olcum"]["test"]
        f = lambda z: f"n {z.get('n', 0):4d} gun {z.get('gun', 0):4d} ort {z.get('ort', 0) * 100:+6.2f}% t {z.get('t', 0):+5.2f} kaz {z.get('kazanma', 0) * 100:3.0f}%"
        L.append(f"{h['durum']:9s} {h['ad'][:58]:58s} | egitim {f(e)} | test {f(s)}")
        if h.get("teshis"):
            L.append(f"{'':10s}teshis: {h['teshis']['baskin']} {h['teshis']['sebep']} | rejimde kayip kotu {h['teshis']['rejim_kayip']['kotu']} iyi {h['teshis']['rejim_kayip']['iyi']}")
    open(os.path.join(H, "sonuclar", "kap_olay.txt"), "w", encoding="utf-8").write("\n".join(L))
    print("\n".join(L), flush=True)


if __name__ == "__main__":
    main()
