"""Guvenlik: sifre kurali, POST govde siniri, guncellemede dogrulama + geri alma, SHA-256 zorunlu. Ag yok (yerel sunucu)."""
import http.client
import os
import subprocess
import sys
import threading
import types
from http.server import ThreadingHTTPServer

sys.path.insert(0, sys.argv[1])
import auth
import app
import guncelle as G

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, str(info)[:200], flush=True)


# 1) sifre kurali
check("kisa sifre reddedilir", auth.weak("abc12345") and auth.weak("123456"))
check("sadece harf reddedilir", auth.weak("sadeceharfler"))
check("sadece rakam reddedilir", auth.weak("1234567890"))
check("10+ karakter harf+rakam kabul", auth.weak("guclu-sifre-42") is None and auth.weak("Türkçe12345") is None)
check("create zayif sifreyi reddeder", "10 karakter" in (auth.create("zayif_kul", "123456", "user") or ""))
check("set_password zayif sifreyi reddeder", "10 karakter" in (auth.set_password("yok", "kisa1") or ""))

# 2) POST govde siniri (gercek yerel HTTP sunucusu, rastgele port)
srv = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


def post(n, body=b"{}"):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    c.putrequest("POST", "/api/auth/login")
    c.putheader("Host", f"127.0.0.1:{port}")
    c.putheader("X-KQB", "1")
    c.putheader("Content-Type", "application/json")
    c.putheader("Content-Length", str(n))
    c.endheaders()
    if n > 0 and n <= len(body):
        c.send(body)
    r = c.getresponse()
    return r.status


check("2 MB ustu govde 413 (okunmadan)", post(app.MAX_BODY + 1) == 413)
check("negatif uzunluk 413", post(-5) == 413)
b = b'{"user": "yok", "password": "yanlis-sifre-1"}'
check("normal giris istegi islenir (413 degil)", post(len(b), b) == 200)
srv.shutdown()

# 3) guncelleme: dogrulama basarisizsa geri al, servis yeniden baslatilmaz
calls, quit_ = [], []


def fake_git(*a):
    calls.append(a)
    out = {"rev-parse": "eskihash123\n"}.get(a[0], "")
    return types.SimpleNamespace(returncode=0, stdout=out, stderr="")


G.GIT, G._git = True, fake_git
_run = subprocess.run


def run_fail_pip(cmd, *a, **k):
    if "pip" in cmd:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="ERROR: No matching distribution")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


G.subprocess.run = run_fail_pip
r = G.apply(lambda: quit_.append(1))
check("pip basarisiz: guncelleme geri alinir, kapanmaz", r and "geri alındı" in r and ("reset", "--hard", "-q", "eskihash123") in calls and not quit_, (r, calls[-1:]))
calls.clear()


def run_fail_import(cmd, *a, **k):
    if "-c" in cmd:
        return types.SimpleNamespace(returncode=1, stdout="", stderr="SyntaxError: invalid syntax")
    return types.SimpleNamespace(returncode=0, stdout="", stderr="")


G.subprocess.run = run_fail_import
r = G.apply(lambda: quit_.append(1))
check("yeni kod yuklenemezse geri alinir", r and "yüklenemedi" in r and any(c[0] == "reset" for c in calls) and not quit_, r)
check("dogrulama ajan botu yuklemez (melikte 77 ile cikardi)", "agresif" not in G._verify_git.__code__.co_consts.__str__())
calls.clear()
G.subprocess.run = lambda cmd, *a, **k: types.SimpleNamespace(returncode=0, stdout="", stderr="")
r = G.apply(lambda: quit_.append(1))
check("dogrulama gecerse geri alma yok, yeniden baslatilir", r is None and quit_ and not any(c[0] == "reset" for c in calls), r)
G.subprocess.run = _run

# 4) exe yolu: SHA-256 dosyasi olmayan paket kurulmaz
G.GIT = False
G.sys = types.SimpleNamespace(frozen=True, executable=sys.executable, argv=sys.argv)
G.STATE["latest"] = {"version": "2099.01.01.0000", "newer": True, "zip": {"browser_download_url": "x", "url": "x"}, "sha": None}
G._download = lambda *a: (_ for _ in ()).throw(AssertionError("indirilmemeli"))
r = G.apply(lambda: None)
check("SHA-256 yoksa guncelleme iptal (indirme bile yok)", r and "SHA-256" in r, r)
print("SONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
sys.exit(0 if ok_all else 1)
