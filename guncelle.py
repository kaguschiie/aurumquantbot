"""Uzaktan tek tikla guncelleme (GitHub Releases) + yeniden baslatma.
Yayin: guncelleme_yayinla.bat -> surum (yyyy.MM.dd.HHmm) + exe paketi (zip) + SHA-256 -> GitHub release.
Uygulama: surumu kontrol eder, paketi indirir, SHA-256 ile dogrular, cikarir; kapanir ve kucuk bir yardimci betik
dosyalari degistirip uygulamayi yeniden acar. 'veri' klasoru (hesaplar, anahtarlar, kasa, Ders Defteri) hic
kopyalanmaz ve silinmez; veri LOCALAPPDATA'daysa zaten kurulum klasorunun disindadir."""
import hashlib
import logging
import os
import subprocess
import sys
import time
import zipfile

import requests

import config as C

log = logging.getLogger("bot")
API = os.environ.get("KQB_UPDATE_API") or "https://api.github.com"
DIR = os.path.join(C.APP_DIR, "guncelleme")
STATE = {"t": 0.0, "latest": None, "error": "", "progress": None, "notified": ""}
DETACH = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | NEW_PROCESS_GROUP | NO_WINDOW


def ver(v):
    """'2026.10.25.1430' -> (2026, 10, 25, 1430); 'dev' -> (0,)"""
    parts = [int(x) for x in str(v).lstrip("vV").split(".") if x.isdigit()]
    return tuple(parts) or (0,)


def _headers(raw=False):
    h = {"User-Agent": "KriptoQuantBot", "Accept": "application/octet-stream" if raw else "application/vnd.github+json"}
    if C.UPDATE_TOKEN:
        h["Authorization"] = f"Bearer {C.UPDATE_TOKEN}"
    return h


GIT = not getattr(sys, "frozen", False) and os.path.isdir(os.path.join(C.BASE_DIR, ".git"))  # sunucu: git ile kurulu


def _git(*a):
    return subprocess.run(["git", "-C", C.BASE_DIR, *a], capture_output=True, text=True, timeout=120)


def _check_git():
    """Sunucu (git klonu): GitHub'daki main ile yerel surumu karsilastirir."""
    f = _git("fetch", "-q", "origin")
    if f.returncode:
        raise ValueError(f.stderr.strip()[:160] or "git fetch başarısız")
    head, remote = _git("rev-parse", "HEAD").stdout.strip(), _git("rev-parse", "origin/main").stdout.strip()
    notes = _git("log", "--format=• %s", f"{head}..{remote}").stdout.strip()
    STATE.update(error="", latest={"version": remote[:7], "notes": notes[:2000], "zip": None, "sha": None, "size": None,
                                   "published": _git("log", "-1", "--format=%cI", remote).stdout.strip(),
                                   "newer": bool(remote) and remote != head and bool(notes)})


def check():
    """En son yayini okur. Donus: durum sozlugu (arayuz icin)."""
    STATE["t"] = time.time()
    if GIT:
        try:
            _check_git()
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            STATE["error"] = f"Güncelleme kontrolü başarısız: {str(e)[:160]}"
        return status()
    if not C.UPDATE_REPO:
        STATE.update(latest=None, error="Güncelleme deposu ayarlanmadı (Ayarlar → Güncelleme).")
        return status()
    try:
        r = requests.get(f"{API}/repos/{C.UPDATE_REPO}/releases/latest", headers=_headers(), timeout=15)
        if r.status_code == 404:
            raise ValueError("Depoda yayın yok veya depo gizli (gizliyse erişim anahtarı girin).")
        r.raise_for_status()
        j = r.json()
        assets = j.get("assets") or []
        z = next((a for a in assets if a["name"].lower().endswith(".zip")), None)
        sha = next((a for a in assets if a["name"].lower().endswith(".sha256")), None)
        if not z:
            raise ValueError("Yayında .zip paketi yok.")
        tag = str(j.get("tag_name", "")).lstrip("vV")
        STATE.update(error="", latest={"version": tag, "notes": (j.get("body") or "")[:2000], "zip": z, "sha": sha,
                                       "size": z.get("size"), "published": j.get("published_at"),
                                       "newer": ver(tag) > ver(C.VERSION)})
    except (requests.RequestException, ValueError, KeyError) as e:
        STATE["error"] = f"Güncelleme kontrolü başarısız: {str(e)[:160]}"
    return status()


def status():
    lt = STATE["latest"]
    return {"version": C.VERSION if not GIT else _git("rev-parse", "--short", "HEAD").stdout.strip() or C.VERSION,
            "repo": C.UPDATE_REPO, "frozen": bool(getattr(sys, "frozen", False)) or GIT,
            "latest": {k: v for k, v in lt.items() if k not in ("zip", "sha")} if lt else None,
            "newer": bool(lt and lt["newer"]), "error": STATE["error"], "t": STATE["t"], "progress": STATE["progress"]}


def _download(asset, path):
    url = asset["url"] if C.UPDATE_TOKEN else asset["browser_download_url"]
    with requests.get(url, headers=_headers(raw=True), stream=True, timeout=60) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("Content-Length") or asset.get("size") or 0), 0
        with open(path, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                STATE["progress"] = round(done / total * 100) if total else None


TASK = "KriptoQuantBot_guncelle"


def _helper(lines):
    """Uygulama kapandiktan sonra calisan yardimci betik. Windows Gorev Zamanlayici ile baslatilir: uygulamanin surec
    agacindan tamamen bagimsizdir (servis/zamanlayici altinda calisan 7/24 bilgisayarda da kapanista oldurulmez)."""
    bat = os.path.join(os.environ.get("TEMP") or DIR, "kqb_guncelle.bat")  # kisa yol (gorev komutu 261 karakter siniri)
    pid = os.getpid()
    env = "".join(f'set "{k}={v}"\r\n' for k, v in os.environ.items() if k.startswith("KQB_"))  # gelistirici ayarlari
    with open(bat, "w", encoding="utf-8", newline="") as fh:
        fh.write("@echo off\r\nchcp 65001 >nul\r\ntitle KriptoQuantBot guncelleniyor\r\n" + env + ":bekle\r\n"
                 f'tasklist /FI "PID eq {pid}" | find "{pid}" >nul && (timeout /t 1 /nobreak >nul & goto bekle)\r\n'
                 + "\r\n".join(lines) + f"\r\nschtasks /delete /tn {TASK} /f >nul 2>nul\r\n")
    nw = {"capture_output": True, "creationflags": 0x08000000}
    try:
        if subprocess.run(["schtasks", "/create", "/tn", TASK, "/tr", f'cmd /c "{bat}"', "/sc", "once", "/st", "23:59",
                           "/f"], **nw).returncode == 0 and subprocess.run(["schtasks", "/run", "/tn", TASK], **nw).returncode == 0:
            return
    except OSError:
        pass
    subprocess.Popen(["cmd", "/c", bat], creationflags=DETACH, close_fds=True, cwd=DIR)  # yedek yol


def _relaunch_cmd():
    if getattr(sys, "frozen", False):
        return f'start "" "{sys.executable}" ' + " ".join(f'"{a}"' for a in sys.argv[1:])
    return f'start "" "{sys.executable}" ' + " ".join(f'"{a}"' for a in [os.path.abspath(sys.argv[0])] + sys.argv[1:])


def restart(quit_cb):
    """Uygulamayi kapatip yeniden acar (or. sunucu modu degisince)."""
    _helper([_relaunch_cmd()])
    quit_cb()


def apply(quit_cb):
    """Indir -> SHA-256 dogrula -> cikar -> kapan; yardimci betik dosyalari degistirip yeniden acar. Hata metni/None.
    Sunucuda (git): git pull + paketler; kapanir, systemd yeniden baslatir (veri, oturumlar, Telegram korunur)."""
    if GIT:
        p = _git("pull", "--ff-only", "-q", "origin", "main")
        if p.returncode:
            return f"git pull başarısız: {p.stderr.strip()[:200]}"
        req = os.path.join(C.BASE_DIR, "requirements-sunucu.txt")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", req], capture_output=True, timeout=900)
        log.info("Sunucu guncellendi: %s", _git("log", "-1", "--format=%h %s").stdout.strip())
        quit_cb()  # systemd (Restart=always) yeni kodla yeniden baslatir
        return None
    if not getattr(sys, "frozen", False):
        return "Geliştirici modunda (python) güncelleme uygulanmaz; exe sürümünde çalışır."
    lt = STATE["latest"] or (check() and STATE["latest"])
    if not lt:
        return STATE["error"] or "Yayın bulunamadı."
    if not lt["newer"]:
        return f"Zaten en güncel sürüm ({C.VERSION})."
    try:
        os.makedirs(DIR, exist_ok=True)
        pkg, new = os.path.join(DIR, "paket.zip"), os.path.join(DIR, "yeni")
        _download(lt["zip"], pkg)
        if lt["sha"]:
            want = requests.get(lt["sha"]["url"] if C.UPDATE_TOKEN else lt["sha"]["browser_download_url"],
                                headers=_headers(raw=True), timeout=30).text.split()[0].strip().lower()
            h = hashlib.sha256()
            with open(pkg, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != want:
                return "Paket doğrulanamadı (SHA-256 uyuşmuyor); güncelleme iptal edildi."
        if os.path.isdir(new):
            subprocess.run(["cmd", "/c", "rmdir", "/s", "/q", new], creationflags=0x08000000)
        with zipfile.ZipFile(pkg) as z:
            z.extractall(new)
        src = next((d for d, _, fs in os.walk(new) if "KriptoQuantBot.exe" in fs), None)
        if not src or not os.path.isdir(os.path.join(src, "_internal")):
            return "Paket bozuk: KriptoQuantBot.exe bulunamadı."
    except (requests.RequestException, OSError, zipfile.BadZipFile, IndexError) as e:
        STATE["progress"] = None
        return f"İndirme/çıkarma hatası: {str(e)[:160]}"
    inst = os.path.dirname(sys.executable)
    log.info("Guncelleme %s -> %s uygulanıyor", C.VERSION, lt["version"])
    _helper([f'cd /d "{inst}"', f'robocopy "{src}" "{inst}" /MIR /XD veri /R:10 /W:2 /NFL /NDL /NJH /NJS /NP >nul',
             _relaunch_cmd(), f'rmdir /s /q "{new}"', f'del /q "{pkg}"'])
    quit_cb()
    return None
