"""Sunucu bakimi (sadece standart kutuphane; botlardan bagimsiz calisir).
  bekci: dakikada bir botlari yoklar; 3 dakika cevap yoksa Telegram'dan haber verir, duzelince de bildirir.
  yedek: gunluk veri yedegi (kasa, ayarlar, hesaplar, Ders Defteri; indirilebilir veri onbellekleri haric), 14 gun saklanir."""
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import urllib.parse
import urllib.request

ROOT = "/opt/crypto_bot"
BOTS = {"melik": (8765, f"{ROOT}/veri"), "fatih": (8766, f"{ROOT}/veri-fatih")}
STATE = f"{ROOT}/bekci.json"
BACKUP = f"{ROOT}/yedek"
LIMIT = 3      # art arda bu kadar basarisiz yoklama (dakika): guncelleme/yeniden baslama alarm vermesin
KEEP = 14
SKIP_DIRS = {"data", "gunluk_veri", "cli", "guncelleme", "pencere", "__pycache__"}
SKIP_EXT = (".pkl", ".joblib", ".log", ".tmp")


def probe(port):
    """(saglikli mi, sebep)."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=10) as r:
            d = json.load(r)
        return bool(d.get("ok")), f"motor {d.get('age')} sn'dir güncellenmedi ({d.get('status')})"
    except Exception as e:  # kapali, zaman asimi, 5xx
        return False, f"cevap yok ({type(e).__name__})"


def _read(path):
    try:
        with open(path, encoding="utf-8-sig") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def channels(data_dir):
    """Bu botun Telegram'i: (token, sohbetler)."""
    s = _read(os.path.join(data_dir, "settings.json"))
    chats = {v.get("chat_id") for v in _read(os.path.join(data_dir, "telegram.json")).values() if isinstance(v, dict)}
    if s.get("TELEGRAM_CHAT_ID"):
        chats.add(s["TELEGRAM_CHAT_ID"])
    chats.discard(None)
    return (s.get("TELEGRAM_TOKEN"), chats) if s.get("TELEGRAM_TOKEN") and chats else (None, set())


def send(text, data_dir, bots):
    """Botun kendi Telegram'i; yoksa diger botlarinki (biri mutlaka duysun). Gonderildiyse True. Token yazdirilmaz."""
    cands = [data_dir] + [d for _, d in bots.values() if d != data_dir]
    for d in cands:
        token, chats = channels(d)
        ok = False
        for c in chats:
            try:
                body = urllib.parse.urlencode({"chat_id": c, "text": text}).encode()
                urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=15).close()
                ok = True
            except Exception as e:
                print("telegram gonderilemedi:", type(e).__name__)
        if ok:
            return True
    return False


def bekci(bots=None, state=STATE, probe_fn=probe, send_fn=send):
    bots = bots or BOTS
    st = _read(state)
    for name, (port, data) in bots.items():
        if not os.path.isdir(data):
            continue
        ok, why = probe_fn(port)
        s = st.setdefault(name, {"fails": 0, "alarm": False})
        if ok:
            if s["alarm"] and send_fn(f"🟢 {name} botu yeniden çalışıyor.", data, bots):
                s["alarm"] = False
            s["fails"] = 0
            continue
        s["fails"] += 1
        print(name, "basarisiz", s["fails"], why)
        if s["fails"] >= LIMIT and not s["alarm"] and \
                send_fn(f"🔴 {name} botu {s['fails']} dakikadır çalışmıyor: {why}. Sunucuda bakın: systemctl status kriptoquantbot", data, bots):
            s["alarm"] = True  # gonderilemediyse sonraki dakika tekrar dener
    tmp = state + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(st, fh)
    os.replace(tmp, state)


def _copy(src, dst):
    """Calisan botun verisi: veritabanlari tutarli kopyalanir, onbellekler atlanir."""
    for base, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rel = os.path.relpath(base, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for f in files:
            if f.endswith(SKIP_EXT):
                continue
            a, b = os.path.join(base, f), os.path.join(dst, rel, f)
            try:
                if f.endswith(".db"):
                    con, out = sqlite3.connect(f"file:{a}?mode=ro", uri=True), sqlite3.connect(b)
                    with out:
                        con.backup(out)
                    con.close()
                    out.close()
                else:
                    shutil.copy2(a, b)
            except (OSError, sqlite3.Error) as e:  # o anda degisen dosya tum yedegi bozmasin
                print("atlandi:", f, type(e).__name__)


def yedek(bots=None, out=BACKUP, keep=KEEP):
    bots = bots or BOTS
    os.umask(0o077)  # yedekte anahtarlar var: sadece root okur
    os.makedirs(out, exist_ok=True)
    os.chmod(out, 0o700)
    name = os.path.join(out, time.strftime("yedek_%Y-%m-%d.tar.gz"))
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        for bot, (_, data) in bots.items():
            if os.path.isdir(data):
                _copy(data, os.path.join(tmp, bot))
        part = os.path.join(out, "yeni.part")
        with tarfile.open(part, "w:gz") as tar:
            tar.add(tmp, arcname="kqb_yedek")
    os.replace(part, name)
    shutil.copy2(name, os.path.join(out, "son.part"))
    os.replace(os.path.join(out, "son.part"), os.path.join(out, "son.tar.gz"))
    olds = sorted(f for f in os.listdir(out) if f.startswith("yedek_") and f.endswith(".tar.gz"))
    for f in olds[:-keep]:
        os.remove(os.path.join(out, f))
    print("yedek:", name, f"{os.path.getsize(name) / 1e6:.1f} MB")
    return name


if __name__ == "__main__":
    {"bekci": bekci, "yedek": yedek}[sys.argv[1]]()
