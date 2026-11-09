"""Sahte borsa testleri: gercek emir, gercek anahtar, ucretli Claude cagrisi yok. Her takim gecici klasorde, bos veri dizinleriyle calisir.
Kullanim: python testler/calistir.py [proje_klasoru] [takim ...]   (proje verilmezse bu klasorun ustu; takim verilmezse hepsi)
Her takim icin bir JSON satiri yazar; hepsi gecerse cikis kodu 0."""
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

BURASI = os.path.dirname(os.path.abspath(__file__))
TAKIMLAR = sorted(os.path.basename(f)[:-3] for f in glob.glob(os.path.join(BURASI, "*_test*.py")))


def kos(proje, takimlar=None, sure=900, yaz=print):
    """Takimlari calistirir. Donus: [{takim, rc, ok, hata, sonuc, sn}]."""
    tmp = tempfile.mkdtemp(prefix="kqb_test_")
    out = []
    try:
        for f in glob.glob(os.path.join(BURASI, "*.py")):
            shutil.copy(f, tmp)  # testler kendi klasorune yazar: depo kirlenmesin
        env = {k: v for k, v in os.environ.items() if not re.match(r"(ANTHROPIC|CLAUDE|BINANCE|KQB_|TELEGRAM)", k.upper())}
        for t in takimlar or TAKIMLAR:
            veri = tempfile.mkdtemp(prefix="veri_", dir=tmp)
            env.update(KQB_APP_DIR=veri, CLAUDE_CONFIG_DIR=os.path.join(tmp, "noclaude"), PYTHONIOENCODING="utf-8")
            t0, log = time.time(), os.path.join(tmp, t + ".log")
            with open(log, "wb") as fh:
                try:
                    rc = subprocess.run([sys.executable, "-I", "-X", "utf8", os.path.join(tmp, t + ".py"), os.path.abspath(proje)], cwd=tmp,
                                        env=env, stdout=fh, stderr=subprocess.STDOUT, timeout=sure).returncode
                except subprocess.TimeoutExpired:
                    rc = "ZAMAN ASIMI"
            txt = open(log, encoding="utf-8", errors="replace").read()
            r = {"takim": t, "rc": rc, "sn": round(time.time() - t0), "ok": len(re.findall(r"^OK\b", txt, re.M)),
                 "hata": len(re.findall(r"^HATA\b", txt, re.M)), "sonuc": (re.findall(r"^SONUC.*$", txt, re.M) or ["-"])[-1][:80],
                 "hatalar": [x[:200] for x in re.findall(r"^HATA\b.*$", txt, re.M)][:5] + ([txt[-600:]] if rc not in (0,) and "Traceback" in txt else [])}
            out.append(r)
            yaz(json.dumps(r, ensure_ascii=False))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return out


def gecti(sonuc):
    return bool(sonuc) and all(r["rc"] == 0 and not r["hata"] for r in sonuc)


if __name__ == "__main__":
    a = sys.argv[1:]
    proje = a.pop(0) if a and os.path.isdir(a[0]) else os.path.dirname(BURASI)
    sys.exit(0 if gecti(kos(proje, a or None)) else 1)
