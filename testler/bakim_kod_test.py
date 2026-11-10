"""Gece yedegi: kendi kod dali olan botun (gelistir.py) kod kopyasi git bundle olarak yedege girer. Ag yok."""
import os
import subprocess
import sys
import tarfile
import tempfile

sys.path.insert(0, sys.argv[1])
import sunucu_bakim as SB

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:200], flush=True)


tmp = tempfile.mkdtemp(prefix="bakim_")
SB.ROOT = tmp
kod = os.path.join(tmp, "aurumquantbot-fatih")
os.makedirs(kod)
g = lambda *a: subprocess.run(["git", "-C", kod, "-c", "user.name=t", "-c", "user.email=t@t", *a], capture_output=True, text=True, check=True)
g("init", "-q", "-b", "fatih")
open(os.path.join(kod, "a.py"), "w").write("x = 1\n")
g("add", "-A")
g("commit", "-qm", "fatihin degisikligi")
os.makedirs(os.path.join(tmp, "aurumquantbot-bos"))  # git olmayan klasor atlanir
veri = os.path.join(tmp, "veri-fatih")
os.makedirs(veri)
open(os.path.join(veri, "state.json"), "w").write("{}")
ad = SB.yedek(bots={"fatih": (8766, veri)}, out=os.path.join(tmp, "yedek"))
with tarfile.open(ad) as t:
    adlar = t.getnames()
check("veri yedegi var", "kqb_yedek/fatih/state.json" in adlar, adlar)
check("kod kopyasi bundle olarak yedekte", "kqb_yedek/kod/aurumquantbot-fatih.bundle" in adlar, adlar)
check("git olmayan klasor atlanir", not any("aurumquantbot-bos" in x for x in adlar))
with tarfile.open(ad) as t:
    t.extract("kqb_yedek/kod/aurumquantbot-fatih.bundle", tmp)
geri = os.path.join(tmp, "geri")
r = subprocess.run(["git", "clone", "-q", "-b", "fatih", os.path.join(tmp, "kqb_yedek", "kod", "aurumquantbot-fatih.bundle"), geri], capture_output=True, text=True)
check("bundle'dan geri yuklenir, degisiklik yerinde", r.returncode == 0 and open(os.path.join(geri, "a.py")).read() == "x = 1\n", r.stderr)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
