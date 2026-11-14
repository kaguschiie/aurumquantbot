"""Ogrenme defteri: olay ornegi (giris zamani, endekse gore fark), kanit kurali (gercek avantaji bulur, gurultuyu reddeder, esik deneme
sayisiyla yukselir), teshis (giris / cikis / maliyet / ortam), kristal, deneme hakki ve emeklilik. Sentetik veri, ag yok."""
import datetime
import random
import sys

sys.path.insert(0, sys.argv[1])
import borsa_ogren as O

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:240], flush=True)


TR = O.TR
rnd = random.Random(7)

# ---------------- 1) seri ve olay ornegi ----------------
gunler = []
d = datetime.date(2022, 1, 3)
while len(gunler) < 400:
    if d.weekday() < 5:
        gunler.append(d)
    d += datetime.timedelta(days=1)
ts = lambda g, s=10, dk=0: datetime.datetime(g.year, g.month, g.day, s, dk, tzinfo=TR).timestamp()
endeks = [[ts(g), 100.0, 101, 99, 100.0, 100.0, 1000] for g in gunler]
OLAY_I = 300
hisse = []
for k, g in enumerate(gunler):  # olaydan sonraki gun acilis 10, sonra her gun +%0,5
    c = 10.0 if k <= OLAY_I else 10.0 * (1 + 0.005 * (k - OLAY_I))
    hisse.append([ts(g), 10.0 if k <= OLAY_I + 1 else c, c, c, c, c, 1e6])
s, e = O.seri(hisse), O.seri(endeks)
check("seri: TR gunu, tekrarsiz", len(s["g"]) == 400 and s["g"][0] == "2022-01-03")
check("09:30 oncesi yayin: ayni gun acilistan giris", O.giris(s, ts(gunler[OLAY_I], 8, 50)) == OLAY_I)
check("seans ici / aksam yayini: ertesi gun acilistan giris (veri 15 dk gecikmeli)",
      O.giris(s, ts(gunler[OLAY_I], 11)) == OLAY_I + 1 and O.giris(s, ts(gunler[OLAY_I], 21)) == OLAY_I + 1)
o = O.ornek(ts(gunler[OLAY_I], 21), s, e)
check("ornek: endeks sabitken fark = hisse getirisi (1 gun %0,5, 5 gun %2,5, 20 gun %10)",
      abs(o["fark"][1] - 0.005) < 1e-9 and abs(o["fark"][5] - 0.025) < 1e-9 and abs(o["fark"][20] - 0.10) < 1e-9, o["fark"])
check("ornek: ozellikler (rejim, seans, onceki momentum, likidite, oynaklik)", set(o["ozellik"]) == {"rejim", "seans_ici", "once", "likidite", "oynak"}
      and o["ozellik"]["seans_ici"] is False and o["ozellik"]["once"] == 0)
check("ornek: 20 gunluk yol yoksa (seri sonu) None", O.ornek(ts(gunler[395], 21), s, e) is None)
check("ornek: endeksin 200 gun gecmisi yoksa None", O.ornek(ts(gunler[100], 21), s, e) is None)
ey = O.seri([m for k, m in enumerate(endeks) if k != OLAY_I + 5])
check("ornek: hisse ve endeks gunleri tutmazsa None (yanlis hizalama yok)", O.ornek(ts(gunler[OLAY_I], 21), s, ey) is None)
tm = O.seri([[ts(gunler[0]), 10.0, 10, 10, 10.0, 9.0, 1], [ts(gunler[1]), 9.5, 9.5, 9.5, 9.5, 9.5, 1]])
check("temettu: duzeltilmis fiyat (temettu gunu dususu kayip gorunmez)", tm["c"] == [9.0, 9.5] and abs(tm["o"][0] - 9.0) < 1e-9, tm)
sey = O.seyrelt([{"kod": "A", "t": 0}, {"kod": "A", "t": 5 * 86400}, {"kod": "B", "t": 5 * 86400}, {"kod": "A", "t": 40 * 86400}])
check("seyreltme: ayni hissede ust uste binen ornek atilir", [(x["kod"], x["t"] // 86400) for x in sey] == [("A", 0), ("B", 5), ("A", 40)], sey)


# ---------------- 2) kanit kurali ----------------
def orn(i, f, yol=None, rejim=True, once=0.0, seans=True):
    g = (datetime.date(2022, 1, 3) + datetime.timedelta(days=i)).isoformat()
    yol = yol or [f * (k + 1) / 20 for k in range(20)]
    return {"t": i * 86400, "gun": g, "kod": f"K{i}", "fark": {1: yol[0], 5: yol[4], 20: yol[19]}, "yol": yol,
            "ozellik": {"rejim": rejim, "seans_ici": seans, "once": once, "likidite": 1e6, "oynak": 0.02}}


KESIM = "2023-09-01"
avantaj = [orn(i, 0.03 + rnd.gauss(0, 0.04)) for i in range(0, 900, 3)]
gurultu = [orn(i, rnd.gauss(0, 0.04)) for i in range(0, 900, 3)]
az = [orn(i, 0.05) for i in range(0, 60, 3)]
check("gercek avantaj (%3, oynaklik %4) kanitlanir", O.karar(O.olc(avantaj, 20, KESIM), 1, 10) == "kanitli", O.olc(avantaj, 20, KESIM))
check("avantajsiz gurultu reddedilir", O.karar(O.olc(gurultu, 20, KESIM), 1, 10) == "red", O.olc(gurultu, 20, KESIM))
check("az gun: yetersiz (sonuc uydurulmaz)", O.karar(O.olc(az, 20, KESIM), 1, 10) == "yetersiz")
check("esik deneme sayisiyla yukselir (1: 1.96, 50: >3)", O.esik(1) == 1.96 and O.esik(50) > 3.0, (O.esik(1), O.esik(50)))
zayif = [orn(i, 0.012 + rnd.gauss(0, 0.04)) for i in range(0, 900, 3)]
o1, o2 = O.karar(O.olc(zayif, 20, KESIM), 1, 1), O.karar(O.olc(zayif, 20, KESIM), 1, 500)
check("ayni zayif sinyal: tek denemede kanit, 500 denemeden sonra artik kanit sayilmaz", (o1, o2) == ("kanitli", "red"), (o1, o2, O.olc(zayif, 20, KESIM)))
maliyet_alti = [orn(i, 0.004 + rnd.gauss(0, 0.001)) for i in range(0, 900, 3)]
check("brut arti ama maliyetin (binde 6) altindaki getiri alim icin kanit degil", O.karar(O.olc(maliyet_alti, 20, KESIM), 1, 1) == "red")
kacin = [orn(i, -0.03 + rnd.gauss(0, 0.04)) for i in range(0, 900, 3)]
check("kacin iddiasi: tutarli olumsuz getiri kanitlanir", O.karar(O.olc(kacin, 20, KESIM, -1), -1, 10) == "kanitli")
check("yon egitimden: avantaj al, olumsuz kacin, maliyet alti yonsuz",
      (O.yon_sec(avantaj, 20, KESIM), O.yon_sec(kacin, 20, KESIM), O.yon_sec(maliyet_alti, 20, KESIM)) == (1, -1, 0))
ayni_gun =[dict(orn(0, 0.05), kod=f"X{k}") for k in range(200)] + [orn(i, rnd.gauss(0, 0.04)) for i in range(3, 900, 3)]
check("ayni gunun 200 olayi tek gozlem sayilir (piyasa gunu sisirmez)", O.olc(ayni_gun, 20, KESIM)["egitim"]["gun"] < O.olc(ayni_gun, 20, KESIM)["egitim"]["n"]
      and O.karar(O.olc(ayni_gun, 20, KESIM), 1, 10) == "red")

# ---------------- 3) teshis ----------------
asagi = [orn(i, 0, yol=[-0.001 * (k + 1) for k in range(20)]) for i in range(30)]
check("hic lehe gitmeyen kayip: GIRIS (cikisla ugrasma onerisi)", O.teshis(asagi, 20)["baskin"] == "giris" and "YAPMA" in O.teshis(asagi, 20)["oneri"])
geri = [orn(i, 0, yol=[0.03] * 10 + [-0.01] * 10) for i in range(30)]
check("once lehe gidip geri veren: CIKIS", O.teshis(geri, 20)["baskin"] == "cikis")
ince = [orn(i, 0, yol=[0.002] * 20) for i in range(30)]
check("brut arti net eksi: MALIYET", O.teshis(ince, 20)["baskin"] == "maliyet")
ort = [orn(i, -0.02, rejim=False) for i in range(20)] + [orn(i, 0.03 if i % 3 else -0.02, rejim=True) for i in range(20, 50)]
check("kayiplar kotu rejimde toplanmis: ORTAM", O.teshis(ort, 20)["baskin"] == "ortam", O.teshis(ort, 20))
check("az kayipta teshis yok", O.teshis(avantaj[:5], 20) is None)

# ---------------- 4) kristal ----------------
kr = [orn(i, (0.04 if i % 2 else -0.02) + rnd.gauss(0, 0.005), once=(0.1 if i % 2 else -0.1), seans=bool(i % 3)) for i in range(120)]
k = O.kristal(kr, 20)
check("kristal: kazanani ayiran kosul ilk sirada (onceki momentum yuksek)", k and k[0]["kosul"][0] == "once" and k[0]["kosul"][1] == ">" and k[0]["fark"] > 0.05, k[:1])
check("kosullu alt kume dogru secilir", all(O.sagla(x, k[0]["kosul"]) == (x["ozellik"]["once"] > 0) for x in kr))

# ---------------- 5) defter ----------------
D = O.Defter()
h = D.test_et("geri_alim_20", "geri_alim", 1, 20, gurultu, KESIM)
check("defter: test kaydedilir, red ise teshis yazilir", D.deneme == 1 and h["durum"] == "red" and "teshis" in h)
alt = [D.test_et(f"geri_alim_20_k{i}", "geri_alim", 1, 20, gurultu, KESIM, kosul=("rejim", "==", True), ust="geri_alim_20") for i in range(4)]
check("deneme hakki: ayni hipoteze en cok 3 alt deneme, 4.'su yapilmaz", all(alt[:3]) and alt[3] is None and D.deneme == 4)
D.test_et("avantaj_20", "x", 1, 20, avantaj, KESIM)
D.yeniden_karar()
check("hakki biten ve alti kanitlanamayan hipotez EMEKLI (ayni duvara vurma)", D.h[0]["durum"] == "emekli")
check("herkes son deneme sayisinin esigiyle yargilanir", len({h["esik"] for h in D.h}) == 1 and D.h[0]["esik"] == O.esik(D.deneme))
check("kanitli liste", [h["ad"] for h in D.kanitlilar()] == ["avantaj_20"])
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
