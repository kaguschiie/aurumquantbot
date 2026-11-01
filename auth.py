"""Kullanici hesaplari: yerel SQLite veritabani, PBKDF2 sifre ozeti, bellek ici oturumlar.
Roller: admin (her sey) ve user (sadece gercek hesap ekranlari + kendi Binance API'si)."""
import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

import config as C

DB = os.path.join(C.APP_DIR, "kullanicilar.db")
ROLES = ("admin", "user")
TTL = 12 * 3600            # islem yapilmayan oturum 12 saatte kapanir
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{3,32}$")
_lock = threading.Lock()
SESSIONS = {}              # token -> {"user", "role", "t"}
FAILS = {}                 # kullanici -> [hatali deneme, kilit bitis]


def _db():
    con = sqlite3.connect(DB)
    con.execute("CREATE TABLE IF NOT EXISTS users(name TEXT PRIMARY KEY COLLATE NOCASE, role TEXT NOT NULL, "
                "salt BLOB NOT NULL, hash BLOB NOT NULL, created REAL NOT NULL)")
    return con


def _hash(pw, salt):
    return hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 300_000)


def has_users():
    with _lock, _db() as con:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0] > 0


def users():
    with _lock, _db() as con:
        return [{"name": n, "role": r, "created": t} for n, r, t in
                con.execute("SELECT name, role, created FROM users ORDER BY role, name")]


def create(name, pw, role, first=False):
    """Hata metni veya None dondurur. first: ilk kurulum (sadece hic kullanici yokken, tek islemde)."""
    if not NAME_RE.match(name or ""):
        return "Kullanıcı adı 3-32 karakter olmalı (harf, rakam, _ . -)."
    if len(pw or "") < 6:
        return "Şifre en az 6 karakter olmalı."
    if role not in ROLES:
        return "Geçersiz rol."
    salt = os.urandom(16)
    try:
        h = _hash(pw, salt)
        with _lock, _db() as con:
            if first and con.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
                return "Kurulum zaten yapılmış."
            con.execute("INSERT INTO users VALUES (?,?,?,?,?)", (name, role, salt, h, time.time()))
    except sqlite3.IntegrityError:
        return "Bu kullanıcı adı zaten var."
    return None


def set_password(name, pw):
    if len(pw or "") < 6:
        return "Şifre en az 6 karakter olmalı."
    salt = os.urandom(16)
    with _lock, _db() as con:
        if not con.execute("UPDATE users SET salt=?, hash=? WHERE name=?", (salt, _hash(pw, salt), name)).rowcount:
            return "Kullanıcı bulunamadı."
    _drop_sessions(name)
    return None


def _drop_sessions(name):
    for tok, s in list(SESSIONS.items()):
        if s["user"].lower() == name.lower():
            SESSIONS.pop(tok, None)


def delete(name):
    with _lock, _db() as con:
        row = con.execute("SELECT role FROM users WHERE name=?", (name,)).fetchone()
        if not row:
            return "Kullanıcı bulunamadı."
        if row[0] == "admin" and con.execute("SELECT COUNT(*) FROM users WHERE role='admin'").fetchone()[0] <= 1:
            return "Son admin hesabı silinemez."
        con.execute("DELETE FROM users WHERE name=?", (name,))
    _drop_sessions(name)
    return None


_flock = threading.Lock()


def login(name, pw):
    """(token, rol, None) veya (None, None, hata)."""
    key = (name or "").lower()[:64]
    with _flock:  # deneme hakki sifre kontrolunden ONCE ayrilir: paralel isteklerle kilit asilamaz
        n, until = FAILS.get(key, [0, 0])
        if time.time() < until:
            return None, None, f"Çok fazla hatalı deneme. {int(until - time.time())} sn sonra tekrar deneyin."
        FAILS[key] = [n + 1, time.time() + 30 if n + 1 >= 5 else 0]
        if len(FAILS) > 1000:
            FAILS.clear()
    with _lock, _db() as con:
        row = con.execute("SELECT name, role, salt, hash FROM users WHERE name=?", (name or "",)).fetchone()
    h = _hash(pw or "", row[2] if row else b"\0" * 16)  # olmayan kullanicida da ayni sure (ad tahmini zorlasir)
    if not row or not hmac.compare_digest(h, row[3]):
        return None, None, "Kullanıcı adı veya şifre hatalı."
    FAILS.pop(key, None)
    tok = secrets.token_urlsafe(32)
    SESSIONS[tok] = {"user": row[0], "role": row[1], "t": time.time()}
    return tok, row[1], None


def session(tok):
    s = SESSIONS.get(tok or "")
    if not s or time.time() - s["t"] > TTL:
        SESSIONS.pop(tok or "", None)
        return None
    s["t"] = time.time()
    return s


def logout(tok):
    SESSIONS.pop(tok or "", None)
