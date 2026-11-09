"""futures.py sanal adaptor: LONG/SHORT, stop/hedef, kismi dolum, cift emir, bosluk, ayni mum, likidasyon, fonlama, mutabakat. Ag yok."""
import sys

sys.path.insert(0, sys.argv[1])
import futures as F

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, info, flush=True)


def yeni(bal=1000.0):
    return F.FuturesPaper(bal, rules={"BTC/USDT": F.Rules(0.1, 0.001, 0.001, 5), "ETH/USDT": F.Rules(0.01, 0.001, 0.001, 5)})


# 1) acilis kurallari
f = yeni()
check("stopsuz emir reddedilir (fail-closed)", f.open("BTC/USDT", "long", 0.002, 100000, None, cid="a1") == "stop zorunlu")
check("LONG stop giris ustunde reddedilir", "yanlış" in f.open("BTC/USDT", "long", 0.002, 100000, 101000, cid="a2"))
check("SHORT stop giris altinda reddedilir", "yanlış" in f.open("BTC/USDT", "short", 0.002, 100000, 99000, cid="a3"))
check("kaldirac 1x ustu reddedilir", "kaldıraç" in f.open("BTC/USDT", "long", 0.002, 100000, 98000, lev=3, cid="a4"))
check("kimliksiz emir reddedilir", "kimliği" in f.open("BTC/USDT", "long", 0.002, 100000, 98000))
check("teminat yetersiz reddedilir", "teminat" in f.open("BTC/USDT", "long", 0.02, 100000, 98000, cid="a5"))
check("alt sinir alti reddedilir", "alt sınır" in f.open("BTC/USDT", "long", 0.00001, 100000, 98000, cid="a6"))
cash0 = f.cash
check("LONG acilir", f.open("BTC/USDT", "long", 0.004, 100000, 98000, 104000, cid="L1") is None and f.pos["BTC/USDT"]["side"] == "long")
p = f.pos["BTC/USDT"]
check("giris kaymali, teminat 1x = nominal", abs(p["entry"] - 100050) < 1e-6 and abs(p["margin"] - 0.004 * 100050) < 1e-6, p["entry"])
check("1x LONG likidasyon ~0 (mmr)", abs(p["liq"] - 100050 * F.MMR) < 1e-6, p["liq"])
check("ayni kimlikle ikinci emir reddedilir", "kullanıldı" in f.open("ETH/USDT", "long", 0.1, 3000, 2900, cid="L1"))
check("ayni sembolde cakisan (ters) pozisyon reddedilir", "açık pozisyon" in f.open("BTC/USDT", "short", 0.001, 100000, 102000, cid="L2"))
# 2) stop sadece lehe
check("LONG stop asagi tasinamaz", "lehine" in f.move_stop("BTC/USDT", 97000) and p["sl"] == 98000)
check("LONG stop yukari tasinir", f.move_stop("BTC/USDT", 99000) is None and p["sl"] == 99000)
# 3) ayni mumda stop + hedef: once stop
t = f.bar("BTC/USDT", 100000, 105000, 98500, 104000)
check("ayni mumda stop ve hedef: stop sayilir", t and t["reason"] == "STOP" and "BTC/USDT" not in f.pos, t and t["reason"])
check("stop dolumu stop fiyati - kayma", abs(t["exit"] - 99000 * (1 - F.SLIPPAGE)) < 1e-6, t["exit"])
check("defter: nakit degisimi = islem net", abs((f.cash - cash0) - t["pnl"]) < 1e-6, (f.cash - cash0, t["pnl"]))
# 4) SHORT, hedef, bosluk
f = yeni()
check("SHORT acilir", f.open("ETH/USDT", "short", 0.1, 3000, 3090, 2820, cid="S1") is None)
ps = f.pos["ETH/USDT"]
check("1x SHORT likidasyon ~2x giris", abs(ps["liq"] - ps["entry"] * (2 - F.MMR)) < 1e-6, ps["liq"])
check("SHORT stop yukari tasinamaz", "lehine" in f.move_stop("ETH/USDT", 3100))
check("SHORT stop asagi tasinir", f.move_stop("ETH/USDT", 3050) is None)
c0 = f.cash
t = f.bar("ETH/USDT", 2900, 2950, 2800, 2810)
check("SHORT hedef", t["reason"] == "HEDEF" and abs(t["exit"] - 2820) < 1e-9 and t["pnl"] > 0, t)
f.open("ETH/USDT", "short", 0.1, 3000, 3090, cid="S2")
t = f.bar("ETH/USDT", 3200, 3250, 3150, 3220)
check("bosluk: SHORT stop acilistan (3200) dolar, stoptan degil", abs(t["exit"] - 3200 * (1 + F.SLIPPAGE)) < 1e-6 and t["R"] < -2, (t["exit"], round(t["R"], 2)))
f.open("BTC/USDT", "long", 0.004, 100000, 98000, cid="L9")
t = f.bar("BTC/USDT", 95000, 96000, 94000, 95500)
check("bosluk: LONG stop acilistan dolar, zarar planlanandan buyuk", abs(t["exit"] - 95000 * (1 - F.SLIPPAGE)) < 1e-6 and t["R"] < -2)
# 5) kismi dolum: stop sadece dolan miktar
f = yeni()
f.open("BTC/USDT", "long", 0.004, 100000, 98000, cid="P1", fill=0.5)
check("kismi dolum: pozisyon dolan miktar", abs(f.pos["BTC/USDT"]["qty"] - 0.002) < 1e-12 and f.pos["BTC/USDT"]["partial"])
check("reduceOnly: fazla kapatma pozisyonu asamaz", f.close("BTC/USDT", 100000, "ELLE", qty=1.0, cid="P2") is None and "BTC/USDT" not in f.pos)
check("kapali pozisyon tekrar kapatilamaz", f.close("BTC/USDT", 100000, "ELLE") == "pozisyon yok")
f.open("BTC/USDT", "long", 0.004, 100000, 98000, cid="P3")
f.close("BTC/USDT", 101000, "KISMI", qty=0.002, cid="P4")
check("kismi kapanis: kalan yarisi, teminat yarisi", abs(f.pos["BTC/USDT"]["qty"] - 0.002) < 1e-12 and abs(f.pos["BTC/USDT"]["margin"] - 0.002 * 100050) < 1e-6)
check("cikis kimligi de tekrar kullanilamaz", "kullanıldı" in f.close("BTC/USDT", 101000, "X", cid="P4"))
# 6) fonlama ve likidasyon
f = yeni()
f.open("BTC/USDT", "long", 0.004, 100000, 98000, cid="F1")
f.open("ETH/USDT", "short", 0.1, 3000, 3090, cid="F2")
a, b = f.funding("BTC/USDT", 0.0001, 100000), f.funding("ETH/USDT", 0.0001, 3000)
check("fonlama pozitif: LONG oder, SHORT alir", a > 0 and b < 0, (a, b))
F.MAX_LEV = 10
g = yeni(1000)
check("10x LONG: stop likidasyon yolunun yarisindan uzaksa reddedilir", "likidasyon" in g.open("BTC/USDT", "long", 0.05, 100000, 93000, lev=10, cid="X1"))
check("10x LONG acilir (stop yakin)", g.open("BTC/USDT", "long", 0.05, 100000, 97000, lev=10, cid="X2") is None)
q = g.pos["BTC/USDT"]
liq0 = q["liq"]
for _ in range(40):  # fonlama teminati eritir: likidasyon yaklasir
    g.funding("BTC/USDT", 0.001, 100000)
check("fonlama teminati eritince likidasyon fiyati yukselir", q["liq"] > liq0, (round(liq0), round(q["liq"])))
t = g.bar("BTC/USDT", 99000, 99500, q["liq"] - 1, 99000)
check("likidasyon stoptan once gelirse LIKIDASYON, zarar teminatla sinirli", t["reason"] in ("LIKIDASYON", "STOP") and g.cash >= 0, t["reason"])
F.MAX_LEV = 1
# 7) mutabakat + rapor
f = yeni()
f.open("BTC/USDT", "long", 0.004, 100000, 98000, cid="M1")
bad = f.reconcile({"BTC/USDT": ("long", 0.003), "ETH/USDT": ("short", 0.1)})
check("mutabakat: miktar farki ve defterde olmayan pozisyon bulunur", len(bad) == 2 and any("defterde yok" in x for x in bad), bad)
check("mutabakat: uyumlu durumda bos", f.reconcile({"BTC/USDT": ("long", 0.004)}) == [])
check("acik risk = miktar x stop mesafesi", abs(f.open_risk() - 0.004 * (100050 - 98000)) < 1e-6)
f.bar("BTC/USDT", 100000, 100100, 97000, 97500)
f.open("ETH/USDT", "short", 0.1, 3000, 3090, 2820, cid="M2")
f.bar("ETH/USDT", 2900, 2950, 2800, 2810)
r = f.report()
check("rapor LONG/SHORT ayri", r["long"]["islem"] == 1 and r["short"]["islem"] == 1 and r["long"]["net"] < 0 < r["short"]["net"], r)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
