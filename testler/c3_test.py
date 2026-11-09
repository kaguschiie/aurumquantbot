"""3 gun cikis kurali + ML duzeltmeleri. Kullanim: python -X utf8 c3_test.py <repo>. Emir yok, ucretli cagri yok."""
import os, sys, time, tempfile
os.environ["KQB_APP_DIR"] = tempfile.mkdtemp(prefix="kqb_c3_")
sys.path.insert(0, sys.argv[1])
TV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tv")
import numpy as np, pandas as pd, joblib
import portfoy as PF

bad = 0
def check(name, cond, info=""):
    global bad
    print(("OK   " if cond else "HATA ") + name, info if not cond else "")
    bad += 0 if cond else 1

PF.TRAIN_LOCK = os.path.join(os.environ["KQB_APP_DIR"], "kilit")  # gercek ev dizinine yazma

def seri(son, n=260, taban=100.0):
    """n gunluk seri; son: son gunlerin tabana orani. Ortalama yaklasik taban."""
    idx = pd.date_range(end=PF.yesterday(), periods=n, freq="D")
    v = taban * (1 + 0.01 * np.sin(np.arange(n)))
    v[-len(son):] = [taban * x for x in son] if len(son) else v[-len(son):]
    return pd.Series(v, index=idx)

check("3 kapanis altinda: cikis", PF.ml_exit(seri([0.9, 0.9, 0.9])))
check("2 kapanis altinda: cikis yok", not PF.ml_exit(seri([1.1, 0.9, 0.9])))
check("son kapanis ustunde: cikis yok", not PF.ml_exit(seri([0.9, 0.9, 1.1])))

# targets: gercek akis, sahte veri
cur = {}
PF.fresh_close = lambda ex, s: cur[s]
PF.klines = lambda ex, s, n=2: pd.DataFrame({"c": [100.0, 100.0]})
PF.ml_state = lambda m: "calisiyor"
PF.load_model = lambda: {"t": time.time(), "ic": 0.2}
pick_ok = {"v": True}
def fake_pick(ex, cache, say=None, st=None):
    if not pick_ok["v"]:
        raise ValueError("kota")
    return [("AAA/USDT", 1.0), ("BBB/USDT", 0.9)], PF.ml_reg(cur["BTC/USDT"]), "n"
real_pick, PF.ml_pick = PF.ml_pick, fake_pick
st = {}
def tur(son, ml_day):
    cur["BTC/USDT"] = seri(son); cur["ETH/USDT"] = seri([1.05])
    return PF.targets(None, st, ml_day)

w, i = tur([1.1, 1.1, 1.1], True)
check("sepet gunu ustte: sepet alinir", w.get("AAA/USDT", 0) > 0 and st["ml"]["reg"], w)
w, i = tur([1.1, 0.9, 0.9], False)
check("2 kapanis altinda: sepet duruyor", w.get("AAA/USDT", 0) > 0 and not i["uyari"], i["uyari"])
w, i = tur([0.9, 0.9, 0.9], False)
check("3 kapanis altinda: sepet nakit, uyari", "AAA/USDT" not in w and st["ml"]["reg"] is False and any("nakde" in u for u in i["uyari"]), (w, i["uyari"]))
check("secim silinmez", st["ml"]["picks"])
w, i = tur([0.9, 0.9, 0.9], False)
check("uyari tekrarlanmaz", not i["uyari"], i["uyari"])
w, i = tur([1.1, 1.1, 1.1], False)
check("hafta ici toparlanma: geri donus yok", "AAA/USDT" not in w and st["ml"]["reg"] is False, w)
w, i = tur([1.1, 1.1, 1.1], True)
check("sepet gunu toparlanmis: geri donus", w.get("AAA/USDT", 0) > 0 and st["ml"]["reg"], w)
del cur["BTC/USDT"]
PF.fresh_close = lambda ex, s: (_ for _ in ()).throw(PF.VeriBekle("bozuk")) if s == "BTC/USDT" else seri([1.05])
w, i = PF.targets(None, st, False)
check("BTC verisi bozuk: sepet durumu degismez", st["ml"]["reg"] is True and w.get("AAA/USDT", 0) > 0, w)
PF.fresh_close = lambda ex, s: cur[s]

# basarisiz secim: hafta harcanmaz
pick_ok["v"] = False
st.clear(); st["ml"] = {"picks": [("AAA/USDT", 1.0)], "reg": True, "t": time.time()}
w, i = tur([1.1, 1.1, 1.1], True)
check("secim basarisiz: ml_hata, korunur", i.get("ml_hata") and st["ml"].get("kept") and w.get("AAA/USDT", 0) > 0, i)
pick_ok["v"] = True
w, i = tur([1.1, 1.1, 1.1], True)
check("sonraki tur basarili: kept kalkar", not st["ml"].get("kept") and not i.get("ml_hata"), st["ml"])
pick_ok["v"] = False
st["ml"]["t"] = time.time() - 22 * 86400
w, i = tur([1.1, 1.1, 1.1], True)
check("21 gunden eski secim nakde", "AAA/USDT" not in w and not st["ml"]["picks"], w)
pick_ok["v"] = True

# load_model: en yeni yapi, tahmin edemeyen model elenir
import importlib
PF2 = importlib.reload(PF)
PF2.TRAIN_LOCK = os.path.join(os.environ["KQB_APP_DIR"], "kilit")
class M:
    def __init__(s, ok=True): s.ok = ok
    def predict(s, X):
        if not s.ok: raise AttributeError("surum")
        return np.zeros(len(X))
sys.modules["__main__"].M = M
d = os.environ["KQB_APP_DIR"]
PF2.MODEL_FILE, PF2.BUNDLED = os.path.join(d, "a.joblib"), os.path.join(d, "b.joblib")
joblib.dump({"model": M(), "feats": ["x"], "t": 1}, PF2.MODEL_FILE)
joblib.dump({"model": M(), "feats": ["x"], "v": 2, "t": 2}, PF2.BUNDLED)
check("eski kurulum modeli pakettekinin onune gecmez", PF2.load_model()["t"] == 2)
joblib.dump({"model": M(), "feats": ["x"], "v": 2, "t": 3}, PF2.MODEL_FILE)
check("esit yapida kurulumdaki kazanir", PF2.load_model()["t"] == 3)
joblib.dump({"model": M(False), "feats": ["x"], "v": 2, "t": 3}, PF2.MODEL_FILE)
check("tahmin edemeyen elenir", PF2.load_model()["t"] == 2)
joblib.dump({"model": M(False), "feats": ["x"], "v": 2, "t": 2}, PF2.BUNDLED)
PF2._M.clear()
check("ikisi de bozuk: yok", PF2.load_model() is None)

# egitim kilidi
check("kilit alinir", PF2.train_lock())
check("ikinci bot alamaz", not PF2.train_lock())
os.utime(PF2.TRAIN_LOCK, (time.time() - 3 * 3600,) * 2)
check("bayat kilit devralinir", PF2.train_lock())
os.remove(PF2.TRAIN_LOCK)
PF2.TRAIN_LOCK = os.path.join(d, "yok", "kilit")
check("kilit acilamiyorsa engellemez", PF2.train_lock())
PF2.TRAIN_LOCK = os.path.join(d, "kilit")
ran = []
PF2.sync_data, PF2.train = (lambda say: ran.append("s")), (lambda say: ran.append("t"))
PF2.load_model = lambda: None
PF2.train_lock(); PF2.maybe_train(lambda t: None); time.sleep(0.3)
check("diger bot egitirken egitim yok ve deneme yazilmaz", not ran and PF2.last_try() == 0.0, ran)
os.remove(PF2.TRAIN_LOCK)
PF2.maybe_train(lambda t: None); time.sleep(0.5)
check("kilit bosalinca egitir, kilit birakilir", ran == ["s", "t"] and not os.path.exists(PF2.TRAIN_LOCK), ran)

# son denetim duzeltmeleri
src = open(os.path.join(sys.argv[1], "bot.py"), encoding="utf-8").read()
check("kept cekirdegi engellemez", 'info["ml"]["picks"]} - set(PF.CORE_SYMS) if info["ml"].get("kept")' in src)
now = time.time()
for gun, beklenen in ((pd.Timestamp("2026-11-04 12:00"), False), (pd.Timestamp("2026-11-05 00:05"), True), (pd.Timestamp("2026-11-06 00:05"), False)):
    x = gun.timestamp()
    wk = time.strftime("%G-%V", time.gmtime(x - 3 * 86400))
    mg = time.strftime("%G-%V", time.gmtime(x - 4 * 86400))
    check(f"gocu {gun.date()}: secim {beklenen}", (mg != wk) is beklenen, (mg, wk))

# canli kod ile replika ayni gunlerde ayni karari veriyor mu (gercek BTC, 2020 -> son)
if os.path.isdir(os.path.join(TV, "d1")):
    sys.path.insert(0, TV); cwd = os.getcwd(); os.chdir(TV)
    import canli_kopya_c3 as K
    os.chdir(cwd)
    PF3 = importlib.reload(PF2)
    btc = K.b.dropna()
    cur2 = {}
    PF3.fresh_close = lambda ex, s: cur2["c"]
    PF3.klines = lambda ex, s, n=2: pd.DataFrame({"c": [1.0, 1.0]})
    PF3.ml_state = lambda m: "calisiyor"
    PF3.load_model = lambda: {"t": time.time(), "ic": 0.2}
    PF3.ml_pick = lambda ex, cache, say=None, st=None: ([("AAA/USDT", 1.0)], PF3.ml_reg(cur2["c"]), "n")
    g = pd.Series(K.gate(2, 3).ravel(), index=K.c.index)
    st3, fark, n, cik = {}, [], 0, 0
    days = [d for d in btc.index if d >= pd.Timestamp("2020-01-01")]
    first = next(i for i, d in enumerate(days) if d.dayofweek == 2)
    for d in days[first:]:
        cur2["c"] = btc.loc[:d].iloc[-260:]
        PF3.targets(None, st3, d.dayofweek == 2)
        n += 1
        if st3["ml"]["reg"] != bool(g.loc[d]):
            fark.append(str(d.date()))
    print("gun", n, "reg acik", int(g.loc[days[first]:].sum()))
    check("canli kod = replika (her gun)", not fark, fark[:10])
sys.exit(1 if bad else 0)
