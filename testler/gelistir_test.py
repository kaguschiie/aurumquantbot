"""Uygulama ici gelistirme (gelistir.py) + kendi dali olan botun guncellemesi (guncelle.py DAL). Sahte Claude cevaplari, gecici git
depolari; ag yok, ucretli cagri yok, gercek emir yok."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types

sys.path.insert(0, sys.argv[1])
os.environ["KQB_GELISTIRME"] = "1"
import config as C
import agent as A
import guncelle
import gelistir as G

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:240], flush=True)


def git(*a, cwd):
    r = subprocess.run(["git", "-C", cwd, *a], capture_output=True, text=True)
    assert r.returncode == 0, (a, r.stderr)
    return r.stdout.strip()


TMP = tempfile.mkdtemp(prefix="gel_")
BASE = os.path.join(TMP, "repo")
os.makedirs(os.path.join(BASE, "testler"))
DOSYA = {
    "bot.py": "ORDER_RETRY_SEC = 2\n\n\nclass Account:\n    def order(self, s):\n        return 'emir ' + s\n\n    def ozet(self):\n        return 'ozet'\n",
    "ui.html": "<html><body><h1>Kasa</h1></body></html>\n",
    "CLAUDE.md": "# test projesi\n",
    "auth.py": "def login():\n    return None\n",
    "testler/calistir.py": "import os, json\nSONUC = os.environ.get('GEL_TEST_SONUC', 'gecti')\n"
                           "def kos(proje, takimlar=None, sure=900, yaz=print):\n"
                           "    r = [{'takim': 'sahte_test', 'rc': 0 if SONUC == 'gecti' else 1, 'ok': 3, 'hata': 0 if SONUC == 'gecti' else 1, 'sonuc': '-', 'sn': 0, 'hatalar': []}]\n"
                           "    yaz(json.dumps(r[0])); return r\n"
                           "def gecti(s):\n    return all(r['rc'] == 0 and not r['hata'] for r in s)\n",
}
for f, v in DOSYA.items():
    with open(os.path.join(BASE, f), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(v)
git("init", "-q", "-b", "fatih", cwd=BASE)
git("-c", "user.name=t", "-c", "user.email=t@t", "add", "-A", cwd=BASE)
git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "ilk", cwd=BASE)
APP = os.path.join(TMP, "veri")
os.makedirs(APP)
C.BASE_DIR, C.APP_DIR, C.CLAUDE_API_KEY = BASE, APP, "sahte"
G.DIR = os.path.join(APP, "gelistirme")
G.W, G.GECMIS, G.HARCAMA, G.ONAY = os.path.join(G.DIR, "calisma"), os.path.join(G.DIR, "gecmis.json"), os.path.join(G.DIR, "harcama.json"), \
    os.path.join(APP, "gelistirme_onay.sha256")
guncelle.GIT = True

# 1) etkinlik
check("etkin: KQB_GELISTIRME=1 + git", G.etkin())
os.environ["KQB_GELISTIRME"] = "0"
check("etkin degil: bayrak kapali", not G.etkin() and "açık değil" in G.baslat("bir sey yap", "f"))
os.environ["KQB_GELISTIRME"] = "1"

# 2) sahte Claude
SENARYO, CAGRI = [], []
B = lambda **k: types.SimpleNamespace(**k)
U = B(input_tokens=1000, output_tokens=500, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def arac(ad, girdi, i):
    return B(type="tool_use", name=ad, input=girdi, id=f"t{i}")


def sahte_call(stream, model=None, **kw):
    CAGRI.append(kw)
    if not SENARYO:
        return B(stop_reason="end_turn", content=[B(type="text", text="Bitti.")], usage=U)
    adim = SENARYO.pop(0)
    if isinstance(adim, str):
        return B(stop_reason="end_turn", content=[B(type="text", text=adim)], usage=U)
    return B(stop_reason="tool_use", content=[arac(a, g, i) for i, (a, g) in enumerate(adim)], usage=U)


A._call = sahte_call


def bekle(durumlar=("hazir", "hata", "degisiklik_yok", "iptal"), sn=30):
    t0 = time.time()
    while time.time() - t0 < sn:
        j = G.STATE["is"]
        if j and j["durum"] in durumlar:
            return j
        time.sleep(0.1)
    return G.STATE["is"]


# 3) basit degisiklik: oku + degistir + ozet
SENARYO[:] = [[("oku", {"yol": "ui.html", "bas": 1, "adet": 50})],
              [("degistir", {"yol": "ui.html", "eski": "<h1>Kasa</h1>", "yeni": "<h1>Kasa</h1><p>7 gun</p>"})],
              "Kasa kartina 7 gunluk satir eklendi."]
check("baslat", G.baslat("Kasa kartina 7 gunluk kar ekle", "fatih") is None)
j = bekle()
check("is hazir, testler gecti, ozet var", j["durum"] == "hazir" and j["gecti"] and "7 gunluk" in j["ozet"], (j["durum"], j.get("hata")))
check("degisen dosya ve fark", j["dosyalar"] == ["ui.html"] and "7 gun" in j["fark"], j["dosyalar"])
check("korunan yok", j["korunan"] == [], j["korunan"])
check("Claude'a araclar, sistem notu ve onbellek gitti", CAGRI and len(CAGRI[0]["tools"]) == 5 and CAGRI[0]["system"][0]["cache_control"] and "test projesi" in CAGRI[0]["system"][0]["text"])
check("bot klasoru henuz degismedi", "7 gun" not in open(os.path.join(BASE, "ui.html"), encoding="utf-8").read())
check("acik is varken ikinci istek reddedilir", "onay bekliyor" in G.baslat("baska", "fatih"))
check("harcama yazildi", G.harcanan() > 0 and j["usd"] > 0, G.harcanan())
cik = []
check("uygula", G.uygula(None, lambda: cik.append(1)) is None)
time.sleep(1.5)
check("bot dalina islendi, yeniden baslatildi", "7 gun" in open(os.path.join(BASE, "ui.html"), encoding="utf-8").read() and cik == [1])
sha1 = git("rev-parse", "HEAD", cwd=BASE)
g = json.load(open(G.GECMIS, encoding="utf-8"))
check("gecmise yazildi", g[-1]["sha"] == sha1 and g[-1]["tur"] == "uygulandi" and g[-1]["dosyalar"] == ["ui.html"])
check("calisma kopyasi temizlendi", not os.path.isdir(G.W) and G.STATE["is"] is None)

# 4) arac guvenligi
os.makedirs(G.W, exist_ok=True)
for yol in ("../veri/x.txt", ".git/config", "veri/settings.json", "a/b/../../../dis.txt", "x.db"):
    try:
        G._yol(yol)
        check(f"yol reddi: {yol}", False)
    except ValueError:
        check(f"yol reddi: {yol}", True)
shutil.rmtree(G.W)

# 5) korunan bolge + onay kodu + test basarisizligi
SENARYO[:] = [[("degistir", {"yol": "bot.py", "eski": "return 'emir ' + s", "yeni": "return 'EMIR ' + s"})], "Emir metni buyuk harf."]
G.baslat("emir metnini degistir", "fatih")
j = bekle()
check("korunan fonksiyon tespit edildi", j["korunan"] == ["bot.py: Account.order"], j["korunan"])
check("onay kodu yokken uygulanmaz", "onay kodu" in G.uygula(None, lambda: None))
with open(G.ONAY, "w") as fh:
    fh.write(hashlib.sha256(b"dogru-kod-123").hexdigest())
check("yanlis kodla uygulanmaz", "onay kodu" in G.uygula("yanlis", lambda: None))
check("dogru kodla uygulanir", G.uygula("dogru-kod-123", lambda: None) is None)
time.sleep(1.2)
G.vazgec()
SENARYO[:] = [[("degistir", {"yol": "bot.py", "eski": "return 'ozet'", "yeni": "return 'ozet2'"})], "ozet degisti"]
G.baslat("ozeti degistir", "fatih")
j = bekle()
check("korunmayan fonksiyon serbest", j["korunan"] == [], j["korunan"])
G.vazgec()
SENARYO[:] = [[("degistir", {"yol": "bot.py", "eski": "ORDER_RETRY_SEC = 2", "yeni": "ORDER_RETRY_SEC = 0"})], "sabit"]
G.baslat("sabiti degistir", "fatih")
check("bot.py buyuk harf sabit korunur", bekle()["korunan"] == ["bot.py: ORDER_RETRY_SEC"], G.STATE["is"]["korunan"])
G.vazgec()
SENARYO[:] = [[("degistir", {"yol": "auth.py", "eski": "return None", "yeni": "return 1"})], "giris"]
G.baslat("girisi degistir", "fatih")
check("korunan dosya (auth.py) tespit", bekle()["korunan"] == ["auth.py (tüm dosya)"], G.STATE["is"]["korunan"])
G.vazgec()
SENARYO[:] = [[("degistir", {"yol": "bot.py", "eski": "    def ozet(self):", "yeni": "    def ozet(self)"})], "bozuk"]
G.baslat("bozuk kod", "fatih")
check("derlenemeyen kod korunan sayilir", "bot.py (derlenemiyor)" in bekle()["korunan"])
G.vazgec()
os.environ["GEL_TEST_SONUC"] = "kaldi"
SENARYO[:] = [[("degistir", {"yol": "ui.html", "eski": "<h1>Kasa</h1>", "yeni": "<h1>KASA</h1>"})], "baslik"]
G.baslat("baslik", "fatih")
j = bekle()
check("testler gecmezse uygulanamaz", j["durum"] == "hazir" and not j["gecti"] and "geçmedi" in G.uygula(None, lambda: None))
os.environ["GEL_TEST_SONUC"] = "gecti"
G.vazgec()

# 6) arac hatasi Claude'a doner, degisiklik yoksa durum
SENARYO[:] = [[("degistir", {"yol": "ui.html", "eski": "yok-boyle-metin", "yeni": "x"})], "Yapamadim."]
G.baslat("olmayan", "fatih")
j = bekle()
son = next(m for m in reversed(CAGRI[-1]["messages"]) if m["role"] == "user" and isinstance(m["content"], list))["content"][0]
check("eslesmeyen degistir hata olarak doner", son.get("is_error") and "0 kez" in son["content"], son)
check("degisiklik yoksa degisiklik_yok", j["durum"] == "degisiklik_yok")
G.vazgec()

# 7) butce
C.GELISTIRME_IS_USD = 0.001
SENARYO[:] = [[("oku", {"yol": "ui.html", "bas": 1, "adet": 5})]] * 5
G.baslat("butce testi", "fatih")
j = bekle()
check("istek butcesi asilinca durur", j["durum"] == "hata" and "Bütçe" in j["hata"], j.get("hata"))
G.vazgec()
C.GELISTIRME_IS_USD, C.GELISTIRME_GUNLUK_USD = 2.0, 0.0
check("gunluk butce doluysa baslamaz", "bütçesi doldu" in G.baslat("x yap lutfen", "fatih"))
C.GELISTIRME_GUNLUK_USD = 5.0

# 8) geri al
check("geri al", G.geri_al(sha1, lambda: None) is None)
time.sleep(1.2)
check("geri alininca dosya eski hali", "7 gun" not in open(os.path.join(BASE, "ui.html"), encoding="utf-8").read())
check("ikinci kez geri alinamaz", "zaten" in G.geri_al(sha1, lambda: None))

# 9) guncelleme: kendi dali olan bot ana surumu birlestirir; cakisirsa uygulamaz
ORIG = os.path.join(TMP, "origin.git")
git("init", "-q", "--bare", "-b", "main", ORIG, cwd=TMP)
git("push", "-q", ORIG, "fatih:main", cwd=BASE)
git("remote", "add", "origin", ORIG, cwd=BASE)
git("fetch", "-q", "origin", cwd=BASE)
DIS = os.path.join(TMP, "melik")
git("clone", "-q", ORIG, DIS, cwd=TMP)
with open(os.path.join(DIS, "CLAUDE.md"), "a", encoding="utf-8") as fh:
    fh.write("ana surum notu\n")
git("-c", "user.name=m", "-c", "user.email=m@m", "commit", "-qam", "ana", cwd=DIS)
git("push", "-q", "origin", "HEAD:main", cwd=DIS)
guncelle.DAL, guncelle._verify_git = "fatih", lambda: None
guncelle.subprocess = types.SimpleNamespace(run=subprocess.run, SubprocessError=subprocess.SubprocessError)
cik = []
r = guncelle.apply(lambda: cik.append(1))
check("dal modu: ana surum birlesir, botun kendi degisikligi kalir", r is None and cik and "ana surum notu" in open(os.path.join(BASE, "CLAUDE.md"), encoding="utf-8").read()
      and git("merge-base", "--is-ancestor", sha1, "HEAD", cwd=BASE) == "", r)
with open(os.path.join(DIS, "ui.html"), "w", encoding="utf-8") as fh:
    fh.write("<html>ana surum baska</html>\n")
git("-c", "user.name=m", "-c", "user.email=m@m", "commit", "-qam", "cakisan", cwd=DIS)
git("push", "-q", "origin", "HEAD:main", cwd=DIS)
with open(os.path.join(BASE, "ui.html"), "w", encoding="utf-8") as fh:
    fh.write("<html>fatihin degisikligi</html>\n")
git("-c", "user.name=f", "-c", "user.email=f@f", "commit", "-qam", "fatih", cwd=BASE)
once = git("rev-parse", "HEAD", cwd=BASE)
r = guncelle.apply(lambda: cik.append(2))
check("cakismada guncelleme uygulanmaz, bot eski halinde kalir", r and "çakışıyor" in r and git("rev-parse", "HEAD", cwd=BASE) == once
      and not git("status", "--porcelain", "--untracked-files=no", cwd=BASE) and 2 not in cik, r)
shutil.rmtree(TMP, ignore_errors=True)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
