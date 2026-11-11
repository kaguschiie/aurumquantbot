"""Uygulama ici gelistirme: bot sahibi istek yazar, Claude AYRI bir calisma kopyasinda kodu degistirir.
Akis: istek -> calisma kopyasi (git worktree, HEAD) -> Claude duzenler (sadece dosya listeleme/okuma/arama/degistirme; komut
calistiramaz) -> korunan bolge kontrolu -> testler (ONAYLI surumdeki testler/ ile, yeni koda karsi) -> ozet + fark -> sahibi
onaylarsa bu botun dalina commit + yeniden baslatma. Geri al: git revert + yeniden baslatma.
Sadece sunucuda, git klonunda ve KQB_GELISTIRME=1 iken (sunucu_fatih_gelistirme.sh). Para/emir/stop kodu, giris, guncelleme,
ayarlar ve testler korunur: bunlara dokunan degisiklik ayrica onay kodu ister (kod sadece kurulumu yapanda)."""
import ast
import datetime
import fnmatch
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import subprocess
import threading
import time

import anthropic

import agent as A
import config as C
import guncelle

log = logging.getLogger("bot")
DIR = os.path.join(C.APP_DIR, "gelistirme")
W = os.path.join(DIR, "calisma")
GECMIS = os.path.join(DIR, "gecmis.json")
HARCAMA = os.path.join(DIR, "harcama.json")
ONAY = os.path.join(C.APP_DIR, "gelistirme_onay.sha256")
MAX_TUR = 40
STATE = {"is": None}
_LOCK = threading.Lock()
_ONAY_HATA = {"n": 0, "t": 0.0}

KORUNAN_DOSYA = ("auth.py", "guncelle.py", "gelistir.py", "config.py", "agresif.py", "requirements.txt", "requirements-sunucu.txt",
                 "testler/*", "*.sh", "sunucu*.py", ".gitignore")
KORUNAN_FONK = {  # para, emir, stop, risk ve giris yollari
    "bot.py": {"order_cid", "order_fail", "_flock", "Account.fence", "Account.gate", "Account.order", "Account._no_unknown",
               "Account._maybe_sold", "Account._resolve", "Account._settle", "Account._apply", "Account.place_stop", "Account._cancel_stop",
               "Account._replace_stop", "Account._stop_done", "Account.nostop", "Account.blocked", "Account.locked", "Account.take_lock",
               "Account.balance_check", "Account.reconcile", "Account.enter", "Account.exit", "Account.book", "Account._manage",
               "Account.sync", "Account.report", "Account.equity", "Account.free_cash", "Account.risk_mult", "Account.day_pct",
               "Account.roll_pct", "Account._sellable", "Account._status", "Account.check_key", "Account.manage_positions"},
    "borsa.py": {"Borsa._kapi", "Borsa.al", "Borsa.sat", "Borsa.kara_mi", "Borsa.kara_degistir", "Borsa._kayit", "Borsa._son_fiyat", "Borsa.firsatlar"},
    "app.py": {"Handler._guard", "Handler._local", "Handler._token", "Handler._send", "mode_for", "quit_app"},
}
YASAK = re.compile(r"(^|/)(\.git|veri[^/]*|dist|build|__pycache__)(/|$)|settings\.json$|\.(db|pkl|joblib|key|pem|env)$|sunucu_adresi\.txt$")
UZANTI = (".py", ".html", ".js", ".css", ".md", ".json", ".txt")


def etkin():
    return os.environ.get("KQB_GELISTIRME") == "1" and guncelle.GIT


def _git(*a, cwd=None):
    return subprocess.run(["git", "-C", cwd or C.BASE_DIR, *a], capture_output=True, text=True, timeout=120)


def _oku_json(f, bos):
    try:
        with open(f, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return bos


def _yaz_json(f, v):
    os.makedirs(DIR, exist_ok=True)
    with open(f + ".tmp", "w", encoding="utf-8") as fh:
        json.dump(v, fh, ensure_ascii=False, indent=1)
    os.replace(f + ".tmp", f)


def harcanan():
    return float(_oku_json(HARCAMA, {}).get(datetime.date.today().isoformat(), 0.0))


def _harca(usd):
    h = _oku_json(HARCAMA, {})
    g = datetime.date.today().isoformat()
    h = {k: v for k, v in h.items() if k >= (datetime.date.today() - datetime.timedelta(days=30)).isoformat()}
    h[g] = round(h.get(g, 0.0) + usd, 4)
    _yaz_json(HARCAMA, h)


def durum():
    j = STATE["is"]
    return {"etkin": etkin(), "anahtar": bool(C.CLAUDE_API_KEY), "model": C.GELISTIRME_MODEL, "bugun_usd": round(harcanan(), 3),
            "gunluk_usd": C.GELISTIRME_GUNLUK_USD, "is_usd": C.GELISTIRME_IS_USD, "onay_kodu_var": os.path.exists(ONAY),
            "is": {k: v for k, v in j.items() if k != "msgs"} if j else None, "gecmis": _oku_json(GECMIS, [])[-30:][::-1],
            "dal": _git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip() if etkin() else None}


# ---------------- Claude araclari (sadece calisma kopyasinda) ----------------
def _yol(p):
    p = str(p or ".").replace("\\", "/").lstrip("/")
    tam = os.path.realpath(os.path.join(W, p))
    if not (tam == os.path.realpath(W) or tam.startswith(os.path.realpath(W) + os.sep)):
        raise ValueError("calisma kopyasi disina cikilamaz")
    rel = os.path.relpath(tam, W).replace("\\", "/")
    if rel != "." and YASAK.search(rel):
        raise ValueError(f"bu yola erisim yok: {rel}")
    return tam, rel


def _listele(klasor="."):
    tam, _ = _yol(klasor)
    out = []
    for kok, dirs, files in os.walk(tam):
        dirs[:] = sorted(d for d in dirs if not YASAK.search(d + "/") and not d.startswith("."))
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(kok, f), W).replace("\\", "/")
            if not YASAK.search(rel):
                out.append(f"{rel} ({os.path.getsize(os.path.join(kok, f))} bayt)")
        if len(out) > 400:
            break
    return "\n".join(out[:400]) or "(bos)"


def _oku(yol, bas=1, adet=400):
    tam, _ = _yol(yol)
    with open(tam, encoding="utf-8", errors="replace") as fh:
        satir = fh.read().split("\n")
    bas, adet = max(int(bas), 1), min(max(int(adet), 1), 800)
    parca = satir[bas - 1:bas - 1 + adet]
    return f"{yol}: {len(satir)} satir, {bas}-{bas + len(parca) - 1} gosteriliyor\n" + "\n".join(f"{bas + i}\t{s}" for i, s in enumerate(parca))


def _ara(desen, dosya_deseni="*"):
    rx, out = re.compile(desen), []
    for kok, dirs, files in os.walk(W):
        dirs[:] = [d for d in dirs if not YASAK.search(d + "/") and not d.startswith(".")]
        for f in files:
            rel = os.path.relpath(os.path.join(kok, f), W).replace("\\", "/")
            if YASAK.search(rel) or not fnmatch.fnmatch(f, dosya_deseni) or not f.endswith(UZANTI):
                continue
            with open(os.path.join(kok, f), encoding="utf-8", errors="replace") as fh:
                for i, s in enumerate(fh, 1):
                    if rx.search(s):
                        out.append(f"{rel}:{i}: {s.rstrip()[:200]}")
                        if len(out) >= 80:
                            return "\n".join(out) + "\n(ilk 80 sonuc)"
    return "\n".join(out) or "sonuc yok"


def _degistir(yol, eski, yeni):
    tam, rel = _yol(yol)
    with open(tam, encoding="utf-8", newline="") as fh:
        s = fh.read()
    n = s.count(eski)
    if n != 1:
        raise ValueError(f"'eski' metin dosyada {n} kez geciyor; tam bir kez gecmeli (daha fazla cevre satiri ekleyin)")
    with open(tam, "w", encoding="utf-8", newline="") as fh:
        fh.write(s.replace(eski, yeni))
    return f"{rel} degistirildi"


def _yeni_dosya(yol, icerik):
    tam, rel = _yol(yol)
    if os.path.exists(tam):
        raise ValueError("dosya zaten var; mevcut dosyada 'degistir' kullanin")
    if not rel.endswith(UZANTI):
        raise ValueError(f"izin verilen uzantilar: {', '.join(UZANTI)}")
    os.makedirs(os.path.dirname(tam), exist_ok=True)
    with open(tam, "w", encoding="utf-8", newline="") as fh:
        fh.write(icerik)
    return f"{rel} olusturuldu"


def _arac(ad, aciklama, alanlar):
    return {"name": ad, "description": aciklama, "strict": True,
            "input_schema": {"type": "object", "properties": alanlar, "required": list(alanlar), "additionalProperties": False}}


S, I = {"type": "string"}, {"type": "integer"}
ARACLAR = [
    _arac("listele", "Klasordeki dosyalari (alt klasorler dahil) boyutlariyla listeler.", {"klasor": S}),
    _arac("oku", "Dosyanin satirlarini numarali okur (bas: 1'den baslar, adet: en cok 800).", {"yol": S, "bas": I, "adet": I}),
    _arac("ara", "Duzenli ifadeyle (Python regex) dosyalarda arar. dosya_deseni or. '*.py' ya da '*'.", {"desen": S, "dosya_deseni": S}),
    _arac("degistir", "Dosyada 'eski' metnin TEK gecisini 'yeni' ile degistirir. 'eski' dosyada aynen ve bir kez gecmeli (girintiler dahil).",
          {"yol": S, "eski": S, "yeni": S}),
    _arac("yeni_dosya", "Yeni dosya olusturur (var olan dosyanin uzerine yazmaz).", {"yol": S, "icerik": S}),
]
CALIS = {"listele": lambda a: _listele(a["klasor"]), "oku": lambda a: _oku(a["yol"], a["bas"], a["adet"]),
         "ara": lambda a: _ara(a["desen"], a["dosya_deseni"]), "degistir": lambda a: _degistir(a["yol"], a["eski"], a["yeni"]),
         "yeni_dosya": lambda a: _yeni_dosya(a["yol"], a["icerik"])}


def _sistem():
    try:
        with open(os.path.join(W, "CLAUDE.md"), encoding="utf-8") as fh:
            proje = fh.read()[:14000]
    except OSError:
        proje = ""
    return ("Sen KriptoQuantBot (Python kripto al-sat botu + tek dosya arayuz ui.html) uzerinde calisan bir yazilim muhendisisin. Istegi "
            "yazan kisi bot sahibi; yazilimci degil. Kodu SADECE verilen araclarla okuyup degistirebilirsin; komut calistiramazsin. "
            "Isin bitince testler otomatik calisir, degisiklik ancak sahibi onaylarsa canliya gecer.\n"
            "Kurallar:\n1) Once ilgili kodu oku, anla; sonra istenen degisikligi en kucuk ve cerrahi bicimde yap. Istenmeyen yere dokunma, "
            "mevcut calisan ozellikleri bozma, kod stilini koru.\n2) Para/emir/stop/risk kodu (bot.py Account.fence/order/enter/exit/_manage/"
            "place_stop/blocked vb.), giris (auth.py), guncelleme, config.py, agresif.py ve testler KORUNUR: zorunlu olmadikca degistirme; "
            "degistirirsen ayrica yonetici onayi gerekir, nedenini ozette acikca yaz.\n3) Gercek para islemini acan, risk sinirlarini "
            "kaldiran/gevseten, API anahtari/sifre okuyan veya disari gonderen, guvenligi zayiflatan istekleri YAPMA; nedenini acikla.\n"
            "4) Python sozdizimine ve girintiye dikkat et; 'degistir' icin 'eski' metni dosyadan aynen kopyala.\n"
            "5) Bitince (arac cagirmadan) son mesajini sade Turkce yaz: ne degisti, hangi dosyalar, nasil denenir, bilinen risk. Teknik "
            "terimleri acikla, kisa tut. Istek belirsizse veya yapilmamasi gerekiyorsa kod degistirmeden nedenini yaz.\n\n"
            "PROJE NOTLARI (CLAUDE.md):\n" + proje)


# ---------------- korunan bolge kontrolu ----------------
def _fonklar(kaynak):
    """{nitelikli ad: kaynak parcasi}; modul duzeyi BUYUK_HARF sabitler dahil. Derlenemezse None."""
    try:
        agac = ast.parse(kaynak)
    except SyntaxError:
        return None
    out = {}
    for n in agac.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[n.name] = ast.get_source_segment(kaynak, n)
        elif isinstance(n, ast.ClassDef):
            for m in n.body:
                if isinstance(m, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out[f"{n.name}.{m.name}"] = ast.get_source_segment(kaynak, m)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if isinstance(t, ast.Name) and t.id.isupper():
                    out[t.id] = ast.get_source_segment(kaynak, n)
    return out


def korunan(dosyalar, eski_oku, yeni_oku):
    """Degisen dosyalardan korunan bolgeye dokunanlar: ['bot.py: Account.order', 'auth.py (tum dosya)', ...]."""
    out = []
    for f in dosyalar:
        if any(fnmatch.fnmatch(f, d) for d in KORUNAN_DOSYA):
            out.append(f"{f} (tüm dosya)")
            continue
        if f.endswith(".py"):
            eski, yeni = _fonklar(eski_oku(f) or ""), _fonklar(yeni_oku(f) or "")
            if yeni is None:
                out.append(f"{f} (derlenemiyor)")
                continue
            adlar = KORUNAN_FONK.get(f, set()) | ({k for k in (eski or {}) if k.isupper()} if f == "bot.py" else set())
            out += [f"{f}: {a}" for a in sorted(adlar) if (eski or {}).get(a) != yeni.get(a)]
    return out


def onay_kodu_dogru(kod):
    if time.time() < _ONAY_HATA["t"]:
        return False
    try:
        with open(ONAY, encoding="utf-8") as fh:
            want = fh.read().strip().lower()
    except OSError:
        return False
    ok = bool(kod) and hmac.compare_digest(hashlib.sha256(str(kod).encode()).hexdigest(), want)
    if not ok:
        _ONAY_HATA["n"] += 1
        if _ONAY_HATA["n"] >= 5:
            _ONAY_HATA.update(n=0, t=time.time() + 3600)
    return ok


# ---------------- is akisi ----------------
def _adim(j, metin):
    j["adimlar"] = (j["adimlar"] + [{"t": time.time(), "m": metin}])[-200:]


def baslat(istek, kullanici):
    """Hata metni veya None. Is arka planda surer; durum() ile izlenir."""
    istek = str(istek or "").strip()
    if not etkin():
        return "Geliştirme bu botta açık değil."
    if not C.CLAUDE_API_KEY:
        return "Claude API anahtarı girilmemiş (Ayarlar → Claude)."
    if len(istek) < 5:
        return "İsteği biraz daha açık yazın."
    if harcanan() >= C.GELISTIRME_GUNLUK_USD:
        return f"Bugünkü geliştirme bütçesi doldu ({C.GELISTIRME_GUNLUK_USD:g} $). Yarın tekrar deneyin."
    with _LOCK:
        j = STATE["is"]
        if j and j["durum"] in ("calisiyor", "test"):
            return "Zaten çalışan bir istek var."
        if j and j["durum"] == "hazir":
            return "Önceki değişiklik onay bekliyor: önce uygulayın ya da vazgeçin."
        _temizle()
        os.makedirs(DIR, exist_ok=True)
        r = _git("worktree", "add", "--detach", W, "HEAD")
        if r.returncode:
            return f"Çalışma kopyası açılamadı: {r.stderr.strip()[:160]}"
        STATE["is"] = j = {"id": int(time.time()), "istek": istek[:4000], "kullanici": kullanici, "durum": "calisiyor", "t": time.time(),
                           "taban": _git("rev-parse", "HEAD").stdout.strip(), "adimlar": [], "usd": 0.0, "ozet": "", "dosyalar": [],
                           "fark": "", "korunan": [], "testler": [], "gecti": False, "hata": ""}
    threading.Thread(target=_calis, args=(j,), name="gelistirme", daemon=True).start()
    return None


def _calis(j):
    try:
        _claude(j)
        if j["durum"] != "calisiyor":
            return
        _git("add", "-A", cwd=W)
        j["dosyalar"] = [x for x in _git("diff", "--cached", "--name-only", cwd=W).stdout.split("\n") if x]
        if not j["dosyalar"]:
            j["durum"] = "degisiklik_yok"
            return
        j["fark"] = _git("diff", "--cached", "--stat", cwd=W).stdout[-3000:] + "\n" + _git("diff", "--cached", cwd=W).stdout[:60000]
        eski = lambda f: _git("show", f"{j['taban']}:{f}").stdout if _git("cat-file", "-e", f"{j['taban']}:{f}").returncode == 0 else ""

        def yeni(f):
            try:
                with open(os.path.join(W, f), encoding="utf-8") as fh:
                    return fh.read()
            except OSError:
                return ""
        j["korunan"] = korunan(j["dosyalar"], eski, yeni)
        j["durum"] = "test"
        _adim(j, "Testler çalışıyor (birkaç dakika sürer)...")
        spec = importlib.util.spec_from_file_location("kqb_testler", os.path.join(C.BASE_DIR, "testler", "calistir.py"))
        T = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(T)  # onayli surumun testleri, yeni koda karsi
        j["testler"] = T.kos(W, yaz=lambda s: _adim(j, "Test: " + _test_ozet(s)))
        j["gecti"] = T.gecti(j["testler"])
        j["durum"] = "hazir"
        _adim(j, "Hazır: " + ("testler geçti, onayınızı bekliyor." if j["gecti"] else "testler GEÇMEDİ, uygulanamaz."))
    except Exception as e:
        log.exception("gelistirme")
        j.update(durum="hata", hata=str(e)[:300])
        _adim(j, "Hata: " + str(e)[:200])


def _test_ozet(s):
    try:
        r = json.loads(s)
        return f"{r['takim']}: {'geçti' if r['rc'] == 0 and not r['hata'] else 'HATA'} ({r['ok']} kontrol)"
    except (ValueError, KeyError):
        return s[:120]


def _claude(j):
    msgs = [{"role": "user", "content": "İstek:\n" + j["istek"]}]
    sistem = [{"type": "text", "text": _sistem(), "cache_control": {"type": "ephemeral"}}]
    for tur in range(MAX_TUR):
        if j["durum"] != "calisiyor":
            return
        if j["usd"] >= C.GELISTIRME_IS_USD or harcanan() >= C.GELISTIRME_GUNLUK_USD:
            raise RuntimeError(f"Bütçe sınırı ({j['usd']:.2f} $ bu istekte): iş durduruldu, değişiklikler uygulanmadı.")
        try:
            r = A._call(False, model=C.GELISTIRME_MODEL, max_tokens=16000, system=sistem, tools=ARACLAR, messages=msgs,
                        output_config={"effort": "high"})
        except anthropic.APIError as e:
            raise RuntimeError(A.friendly(e))
        usd = A.cost(C.GELISTIRME_MODEL, r.usage)
        j["usd"] = round(j["usd"] + usd, 4)
        _harca(usd)
        if r.stop_reason == "refusal":
            raise RuntimeError("Claude bu isteği güvenlik nedeniyle yapmadı.")
        metin = "\n".join(b.text for b in r.content if b.type == "text").strip()
        cagri = [b for b in r.content if b.type == "tool_use"]
        msgs.append({"role": "assistant", "content": r.content})
        if not cagri:
            if r.stop_reason == "max_tokens":
                msgs.append({"role": "user", "content": "Devam et."})
                continue
            j["ozet"] = metin or "(özet yok)"
            return
        if metin:
            _adim(j, metin[:300])
        sonuc = []
        for b in cagri:
            girdi = b.input if isinstance(b.input, dict) else {}
            try:
                out = CALIS[b.name](girdi)
                if b.name in ("degistir", "yeni_dosya"):
                    _adim(j, out)
                elif b.name == "oku":
                    _adim(j, f"Okudu: {girdi.get('yol')}")
                sonuc.append({"type": "tool_result", "tool_use_id": b.id, "content": out[:60000]})
            except Exception as e:  # araci hatasi Claude'a doner, is durmaz
                sonuc.append({"type": "tool_result", "tool_use_id": b.id, "content": f"Hata: {str(e)[:300]}", "is_error": True})
        msgs.append({"role": "user", "content": sonuc})
    raise RuntimeError(f"{MAX_TUR} adımda bitmedi: istek daha küçük parçalara bölünmeli.")


def _temizle():
    if os.path.isdir(W):
        _git("worktree", "remove", "--force", W)
    _git("worktree", "prune")


def vazgec():
    with _LOCK:
        j = STATE["is"]
        if j and j["durum"] in ("calisiyor", "test"):
            j["durum"] = "iptal"  # Claude dongusu sonraki turda durur
        _temizle()
        STATE["is"] = None
    return None


def uygula(onay_kodu, quit_cb):
    """Testleri gecmis degisikligi bu botun dalina isler ve botu yeniden baslatir. Hata metni veya None."""
    with _LOCK:
        j = STATE["is"]
        if not j or j["durum"] != "hazir":
            return "Uygulanacak hazır değişiklik yok."
        if not j["gecti"]:
            return "Testler geçmedi; bu değişiklik uygulanamaz."
        if j["korunan"] and not onay_kodu_dogru(onay_kodu):
            return "Korunan kod değişiyor: yönetici onay kodu gerekli veya kod yanlış."
        if _git("rev-parse", "HEAD").stdout.strip() != j["taban"]:
            return "Bu arada kod değişti (güncelleme?). Vazgeçip isteği yeniden gönderin."
        if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
            return "Bot klasöründe kaydedilmemiş değişiklik var; uygulanmadı."
        msg = f"Gelistirme ({j['kullanici']}): {j['istek'][:120]}"
        r = _git("-c", "user.name=KQB Gelistirme", "-c", "user.email=gelistirme@kqb.local", "commit", "-q", "-m", msg, cwd=W)
        if r.returncode:
            return f"Kayıt yapılamadı: {r.stderr.strip()[:160]}"
        sha = _git("rev-parse", "HEAD", cwd=W).stdout.strip()
        r = _git("merge", "--ff-only", "-q", sha)
        if r.returncode:
            return f"Değişiklik bota işlenemedi: {r.stderr.strip()[:160]}"
        g = _oku_json(GECMIS, [])
        g.append({"t": time.time(), "sha": sha, "istek": j["istek"][:500], "ozet": j["ozet"][:2000], "dosyalar": j["dosyalar"],
                  "kullanici": j["kullanici"], "usd": j["usd"], "korunan": j["korunan"], "tur": "uygulandi"})
        _yaz_json(GECMIS, g[-200:])
        _temizle()
        STATE["is"] = None
    log.info("Gelistirme uygulandi: %s", sha[:7])
    threading.Thread(target=lambda: (time.sleep(1.0), quit_cb()), daemon=True).start()  # systemd yeni kodla baslatir
    return None


def geri_al(sha, quit_cb):
    """Uygulanmis bir gelistirmeyi geri alir (git revert) ve botu yeniden baslatir. Hata metni veya None."""
    with _LOCK:
        g = _oku_json(GECMIS, [])
        x = next((e for e in g if e.get("sha") == sha and e.get("tur") == "uygulandi" and not e.get("geri")), None)
        if not x:
            return "Bu değişiklik bulunamadı ya da zaten geri alındı."
        if STATE["is"] and STATE["is"]["durum"] in ("calisiyor", "test", "hazir"):
            return "Önce açık isteği bitirin ya da vazgeçin."
        r = _git("-c", "user.name=KQB Gelistirme", "-c", "user.email=gelistirme@kqb.local", "revert", "--no-edit", sha)
        if r.returncode:
            _git("revert", "--abort")
            return "Geri alınamadı: sonraki değişiklikler aynı yerlere dokunuyor. Önce onları geri alın."
        x["geri"] = time.time()
        g.append({"t": time.time(), "sha": _git("rev-parse", "HEAD").stdout.strip(), "istek": "Geri alındı: " + x["istek"][:200],
                  "ozet": "", "dosyalar": x["dosyalar"], "kullanici": "", "usd": 0, "korunan": [], "tur": "geri_alindi"})
        _yaz_json(GECMIS, g[-200:])
    threading.Thread(target=lambda: (time.sleep(1.0), quit_cb()), daemon=True).start()
    return None
