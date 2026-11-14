import copy
import json
import logging
import math
import os
import re
import shutil
import sys
import threading
import time
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import requests

import auth
import config as C

log = logging.getLogger("bot")
URL = f"http://127.0.0.1:{C.PORT}"
HOSTS = {f"127.0.0.1:{C.PORT}", f"localhost:{C.PORT}"}
CACHE, LOCKS, ARKA, ARKA_KILIT = {}, {}, set(), threading.Lock()
ICON = os.path.join(C.APP_DIR, "icon.ico")


class Engine:

    def __init__(self):
        self.bot, self.thread, self.error, self.started = None, None, "", time.time()
        self.halt, self._rl = threading.Event(), threading.Lock()

    def start(self):
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self._loop, args=(self.halt,), daemon=True)
        self.thread.start()

    def _loop(self, halt):
        import bot as B
        import telegram_bot
        while not halt.is_set():
            try:
                bot = B.Bot()
                if halt.is_set():
                    break
                self.bot = bot
                bot.on_notify = lambda m: TRAY and TRAY.notify(m[:250], "KriptoQuantBot")
                telegram_bot.BOT = bot
                self.error = ""
                bot.run()
            except Exception as e:
                log.exception("Motor hatasi")
                self.error = str(e)
                halt.wait(30)

    def stop(self):
        self.halt.set()
        if self.bot:
            self.bot.stop.set()
        if self.thread:
            self.thread.join(180)

    def restart(self):
        with self._rl:
            self.stop()
            self.bot = None
            self.start()


ENGINE = Engine()


def cached(key, ttl, fn):
    t, v = CACHE.get(key, (0, None))
    if time.time() - t <= ttl:
        return v
    with LOCKS.setdefault(key, threading.Lock()):
        t, v = CACHE.get(key, (0, None))
        if time.time() - t > ttl:
            try:
                v = fn()
            except Exception:
                if not t:
                    raise
                log.warning("Onbellek yenilenemedi, eski deger kullaniliyor: %s", key)
            CACHE[key] = (time.time(), v)
    return v


def arkada(key, fn):
    with ARKA_KILIT:
        if key in ARKA:
            return
        ARKA.add(key)

    def run():
        try:
            fn()
        except Exception:
            log.exception("Arka plan isi: %s", key)
        finally:
            with ARKA_KILIT:
                ARKA.discard(key)
    threading.Thread(target=run, daemon=True).start()


def beyin_durumu():
    import agent as A

    def yenile():
        ok, info = A.status()
        CACHE["claude"] = (time.time(), (ok, info, A.brain()))
    t, v = CACHE.get("claude", (0, None))
    if time.time() - t > 60:
        arkada("claude", yenile)
    return v or (True, "Beyin kontrol ediliyor", None)


def clean(o):
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def hide_paper(text):
    return re.sub(r"sanal: [^;\n]*;? ?", "", text)


def B_norm(sym):
    s = str(sym or "").upper().replace("-", "/").replace(" ", "")
    return s[:-4] + "/USDT" if "/" not in s and s.endswith("USDT") else s


def mode_for(sess, q):
    m = str(q.get("acct") or "PAPER").upper()
    return (m if m in ("PAPER", "LIVE") else "PAPER") if sess["role"] == "admin" else "LIVE"


def acct_summary(a):
    st = a.st
    return {"mode": a.mode, "equity": a.equity(), "start": st["start_equity"], "pnl": st["pnl"], "n": st["wins"] + st["losses"],
            "open": len(st["positions"]), "blocked": a.blocked(), "error": a.error}


def payoff(trades):
    w = [t["pnl"] for t in trades if t["pnl"] > 0]
    lo = [t["pnl"] for t in trades if t["pnl"] <= 0]
    rs = [t["R"] for t in trades if t.get("R") is not None]
    return {"avg_win": sum(w) / len(w) if w else None, "avg_loss": sum(lo) / len(lo) if lo else None,
            "exp": sum(w + lo) / len(trades) if trades else None, "pf": sum(w) / -sum(lo) if lo and sum(lo) < 0 else None,
            "exp_r": sum(rs) / len(rs) if rs else None, "n_r": len(rs)}


def state(sess, q):
    b = ENGINE.bot
    admin = sess["role"] == "admin"
    import guncelle
    lt = guncelle.STATE["latest"] or {}
    base = {"user": sess["user"], "role": sess["role"], "live_allowed": C.LIVE_ALLOWED, "error": ENGINE.error,
            "uptime": time.time() - ENGINE.started, "version": C.VERSION,
            "update": lt.get("version") if lt.get("newer") and sess["role"] == "admin" else None}
    if not b:
        return {**base, "status": "Motor başlatılıyor", "ready": False}
    import agent as A
    mode = mode_for(sess, q)
    a = b.accounts.get(mode)
    ok, info, beyin = beyin_durumu()
    base.update(chat_ok=ok, chat_info=info, brain=beyin)
    with b.lock:
        ag = b.ag
        agent = {"spent": ag["spent"] if ag["day"] == time.strftime("%Y-%m-%d", time.gmtime()) else 0.0,
                 "budget": C.CLAUDE_DAILY_BUDGET, "calls": ag["calls"], "last": ag["last"], "error": ag["error"],
                 "model": A.MODELS.get(C.CLAUDE_MODEL, C.CLAUDE_MODEL), "total": ag.get("total", 0),
                 "memo": ag.get("memo", "") if admin else "", "quota": C.QUOTA_SAVER, "review_h": C.REVIEW_HOURS,
                 "last_strategy": ag.get("last_strategy", 0),
                 "next": 3600 - time.time() % 3600}
        if not admin:
            agent = {k: agent[k] for k in ("calls", "last", "error", "model", "next")}
        dec = copy.deepcopy(ag["decisions"][-1]) if ag["decisions"] else None
        scan = dict(b.last_scan)
        out = {**base, "ready": True, "status": b.status, "mode": mode, "symbols": len(b.symbols),
               "age": time.time() - b.updated, "regime": scan.get("regime"), "breadth": scan.get("breadth"),
               "scan_t": scan.get("t"), "live": {"binance": b.stream.live("binance", 5), "bybit": b.stream.live("bybit", 15),
                                                 "teknik": b.tech.live(400)},
               "max_positions": C.MAX_POSITIONS, "agent": agent, "decision": dec,
               "paused": os.path.exists(C.pause_flag(mode)),
               "accounts": {m: acct_summary(x) for m, x in b.accounts.items()} if admin else {}}
        notes = [dict(n, text=n["text"] if admin else hide_paper(n["text"]))
                 for n in b.notes[-12:] if admin or n.get("acct") in (None, "LIVE")]
        if dec and not admin:
            dec.pop("usd", None)
            for r in dec["actions"]:
                r["result"] = hide_paper(r.get("result", ""))
        if not a:
            out.update(no_account=True, notes=notes, positions=[], pending=[], locks=[])
            return out
        st, bid = a.st, b.bid
        positions = []
        for s, p in st["positions"].items():
            px = bid.get(s)
            unreal = p.get("realized", 0) + p["qty"] * ((px or p["entry"]) - p["entry"]) - p["fee"] \
                - p["qty"] * (px or p["entry"]) * C.FEE
            positions.append({**{k: v for k, v in p.items() if k not in ("stop_chk", "stop_try")}, "sym": s, "price": px,
                              "sure_kalan": max(p["sure_t"] - time.time(), 0) if p.get("sure_t") else None,
                              "pnl": unreal if px else None,
                              "pnl_pct": unreal / (p.get("qty0", p["qty"]) * p["entry"]) * 100 if px else None})
        curve = st.get("curve", [])
        step = max(1, len(curve) // 500)
        eq = a.equity()
        locks = [{"sym": s, "why": a.locked(s)} for s in sorted(b.market) if a.locked(s)]
        mult, mwhy = a.risk_mult()
        out["kasa"] = {"principal": st.get("principal"), "peak": st.get("peak"), "mult": mult, "why": mwhy,
                       "risk_usd": eq * C.RISK_PCT * min(mult, 1.0) / 100, "risk_pct": C.RISK_PCT,
                       "be_r": C.BE_TRIGGER_R, "trail": C.TRAIL_ATR}
        out.update({
            "equity": eq, "start_equity": st["start_equity"], "pnl": st["pnl"], "wins": st["wins"],
            "losses": st["losses"], "n": st["wins"] + st["losses"], "day_pct": a.day_pct(), "blocked": a.blocked(),
            "acct_error": a.error, "cash": a.usdt_free if a.live else st["cash"], "earn": st.get("earn_son", 0.0) if a.live else 0.0, "fee": C.FEE,
            "positions": positions, "pending": [{**p, "sym": s, "price_now": bid.get(s)} for s, p in st["pending"].items()],
            "open_pnl": sum(p["pnl"] or 0 for p in positions),
            "curve": curve[::step] + curve[-1:] + [[int(time.time()), round(eq, 2), bid.get(C.BTC)]],
            "locks": locks, "pause_until": st["pause_until"],
            "notes": notes, "trades_recent": copy.deepcopy(st["trades"][-6:]), "payoff": payoff(st["trades"])})
    if admin:
        bt = (b.brain or {}).get("backtest")
        out.update(backtest=bt, model=(b.brain or {}).get("report") or "Model hazırlanıyor...")
    return out


def screener():
    b = ENGINE.bot
    if not b:
        return []
    with b.lock:
        rows = copy.deepcopy(list(b.market.values()))
    return sorted(rows, key=lambda r: -(r.get("qvol") or 0))


def opportunities():
    import dersler as L
    b = ENGINE.bot
    if not b:
        return {"rows": []}
    with b.lock:
        rows, ctx = b.rows(), b.ctx()
        held = {s for a in b.accounts.values() for s in a.st["positions"].keys() | a.st["pending"].keys()}
        rej = {r["sym"]: r["why"] for r in b.ag["rejects"] if time.time() - r["t"] < 6 * 3600}
    out = []
    for o in b.tech.opps:
        sg = L.signals(rows.get(o["sym"], o), ctx)
        veto, mult, edge = L.check(sg, ctx.get("regime"))
        out.append({**{k: v for k, v in o.items() if k != "rt"}, "sigs": [L.label(s) for s in sorted(sg)], "keys": sorted(sg),
                    "edge": edge, "veto": veto, "held": o["sym"] in held, "reject": rej.get(o["sym"])})
    return {"rows": out, "t": b.tech.t_opps, "error": b.tech.error, "n_all": b.tech.n_all,
            "n_liquid": b.tech.n_liquid, "min_vol": C.RADAR_MIN_VOL, "regime": b.regime}


def macro():
    from makro import Makro
    snap = Makro.shared().snapshot()
    b = ENGINE.bot
    if b:
        with b.lock:
            snap["paxg"] = dict(b.market.get("PAXG/USDT") or {})
    return snap


def lessons(sess):
    import dersler as L
    admin = sess["role"] == "admin"
    L.init()
    out = L.summary(admin)
    out["mc"] = __import__("montecarlo").summary()
    b = ENGINE.bot
    out["memo"] = b.ag.get("memo", "") if b and admin else ""
    return out


def montecarlo(q, mode):
    import data as D
    import gecmis as G
    import montecarlo as MC
    b = ENGINE.bot
    s = str(q.get("sym") or C.BTC).upper().replace("-", "/")
    s = s if "/" in s else s[:-4] + "/USDT"
    ex = b.ex if b else D.exchange()
    d = MC.bars(ex, s)
    px = (b.bid.get(s) if b else None) or d["price"]
    atr = d["atr_now"] * px
    acct = b and b.accounts.get(mode)
    pos = acct and acct.st["positions"].get(s)
    regime = b.regime if b else None
    vsp = (b.tech.radar_map.get(s) or {}).get("vspike") if b else None
    if pos:
        exit_p, stop, tp = pos.get("exit") or {}, pos["sl"], pos["tp"]
        hours = max(1, pos.get("max_hours", 24) - (time.time() - pos["t"]) / 3600)
        src = "açık pozisyon (şimdiki fiyattan, kendi stop/hedef ve çıkış kurallarıyla)"
    else:
        exit_p = G.exit_params("trend")
        stop = float(q.get("stop") or 0) or px - 2.5 * atr
        tp = float(q.get("tp") or 0) or px + 4.5 * atr
        hours = float(q.get("hours") or exit_p.get("max_h") or 24)
        src = "plan" if q.get("stop") else "varsayılan plan (2,5 ATR stop, 4,5 ATR hedef)"
    if not 0 < stop < px:
        return {"error": "Stop anlık fiyatın altında olmalı."}
    mc = MC.run(ex, s, px, stop, tp, exit_p, hours, regime, vsp, True, partial_at=pos.get("partial_at") if pos else None)
    return {**mc, "sym": s, "price": px, "stop_px": stop, "tp_px": tp, "src": src, "line": MC.line(mc), "cal": MC.CAL}


def history():
    import dersler as L
    import gecmis as G
    if not G.RES:
        G.load()
    r = G.RES
    rules = r.get("rules") or []
    return {"state": {k: G.STATE[k] for k in ("phase", "done", "total", "t", "error", "running")},
            **{k: r.get(k) for k in ("t", "n", "n_syms", "base", "span", "cut", "portfolio", "curve", "curve_old")},
            "kinds": {k: {kk: v.get(kk) for kk in ("name", "taker", "params", "promoted", "default", "train", "test",
                                                   "by_regime", "exits")} for k, v in (r.get("kinds") or {}).items()},
            "rules": sorted((x for x in rules if x.get("kind") != "bilgi"), key=lambda x: x["avg"])[:40],
            "signals": sorted((x for x in rules if len(x["sigs"]) == 1), key=lambda x: -x["avg"]),
            "labels": {k: L.label(k) for k in G.SIGS}}


def decisions(sess):
    b = ENGINE.bot
    if not b:
        return []
    with b.lock:
        ds = list(b.ag["decisions"][-60:])
    ds = copy.deepcopy(ds)
    if sess["role"] != "admin":
        for d in ds:
            d.pop("usd", None)
            for r in d["actions"]:
                r["result"] = hide_paper(r.get("result", ""))
                if (r.get("card") or {}).get("veto"):
                    r["card"]["veto"] = hide_paper(r["card"]["veto"])
    return ds[::-1]


def _klines(sym, tf):
    r = requests.get("https://api.binance.com/api/v3/klines", params={"symbol": sym.replace("/", ""), "interval": tf, "limit": 500},
                     timeout=10)
    r.raise_for_status()
    return [[int(x[0]), *map(float, x[1:6])] for x in r.json()]


def candles(sym, tf, mode, son=False):
    from stream import Stream
    rows = cached(f"c:{sym}:{tf}", 15, lambda: _klines(sym, tf))
    cs = [{"time": r[0] // 1000, "open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]} for r in rows]
    st = Stream.shared()
    st.watch_kline(sym, tf)
    k = st.kline.get((sym, tf))
    if k and cs and time.time() - k.get("rt", 0) < 10:
        k = {x: v for x, v in k.items() if x != "rt"}
        if k["time"] == cs[-1]["time"]:
            cs[-1] = k
        elif k["time"] > cs[-1]["time"]:
            cs.append(k)
    out = {"candles": cs[-2:] if son else cs, "levels": [], "markers": []}
    from zincir import Zincir
    z = Zincir.shared()
    lm = z.liq.get(sym)
    if not lm or time.time() - lm["t"] > 600:
        cached(f"liq:{sym}", 60, lambda: arkada(f"liq:{sym}", lambda: z.liq_map(sym)))
    if lm and not lm.get("none"):
        out["liq"] = {"long": lm["long"], "short": lm["short"]}
        out["levels"] += [["Long likidasyon", p, "rgba(255,92,108,%.2f)" % (0.35 + 0.6 * w)] for p, w, _ in lm["long"][:3]]
        out["levels"] += [["Short likidasyon", p, "rgba(34,195,142,%.2f)" % (0.35 + 0.6 * w)] for p, w, _ in lm["short"][:3]]
    out["whale"] = z.whale.get(sym)
    b = ENGINE.bot
    a = b and b.accounts.get(mode)
    if a:
        with b.lock:
            p, q, trades = copy.deepcopy(a.st["positions"].get(sym)), a.st["pending"].get(sym), list(a.st["trades"])
        if p:
            mk = " 🔒" if p.get("manual") else ""
            out["levels"] += [["Giris", p["entry"], "#5b8cff"], ["Stop" + mk, p["sl"], "#ff5c6c"]] + (
                [["Hedef" + mk, p["tp"], "#22c38e"]] if p["tp"] < p["entry"] * 50 else [])
            out["pos"] = {"entry": p["entry"], "sl": p["sl"], "tp": p["tp"] if p["tp"] < p["entry"] * 50 else None,
                          "manual": bool(p.get("manual")), "qty": p["qty"], "be": p.get("be"), "kind": p.get("kind"),
                          "ai": p.get("ai"), "ai_not": p.get("ai_not"), "sure_t": p.get("sure_t")}
            if not p.get("part") and p.get("partial_at"):
                out["levels"].append(["Kismi kar", p["partial_at"], "#9ad0b3"])
        if q:
            out["levels"].append(["Alis emri", q["price"], "#f5b041"])
        for t in trades:
            if t["sym"] == sym:
                out["markers"] += [{"time": int(t.get("t_open", t["t"])), "position": "belowBar", "color": "#5b8cff",
                                    "shape": "arrowUp", "text": "AL"},
                                   {"time": int(t["t"]), "position": "aboveBar",
                                    "color": "#22c38e" if t["pnl"] > 0 else "#ff5c6c", "shape": "arrowDown",
                                    "text": f"{t['reason']} {t['pct']:+.1f}%"}]
    return out


def hazirlik_rapor():
    import hazirlik
    b = ENGINE.bot
    return hazirlik.rapor(b, b.ag.get("hazirlik")) if b else {"maddeler": [], "motor": False}


def get_settings(sess):
    keys = C.EDITABLE if sess["role"] == "admin" else [k for k in C.EDITABLE if k in C.USER_EDITABLE]
    out = {k: ("" if k in C.SECRETS else getattr(C, k)) for k in keys}
    out["_set"] = {k: bool(getattr(C, k)) for k in C.SECRETS if k in keys}
    b = ENGINE.bot
    if sess["role"] == "admin" and b:
        p = b.accounts["PAPER"].st
        tr, cv = p["trades"], p["curve"]
        btc = (cv[-1][2] / cv[0][2] - 1) * 100 if len(cv) > 1 and cv[0][2] and cv[-1][2] else None
        ret = (b.accounts["PAPER"].equity() / p["start_equity"] - 1) * 100 if p["start_equity"] else None
        out["_paper"] = {"n": len(tr), "wins": sum(t["pnl"] > 0 for t in tr), "pnl": p["pnl"], "ret": ret, "btc": btc,
                         "since": cv[0][0] if cv else None}
    if sess["role"] == "admin":
        import agent as A
        d = A.cli_diagnose()
        out["_brain"] = {"brain": A.brain(), "cli_found": bool(d["path"] or d["candidates"]), "cli_ready": d["ready"],
                         "why": d["why"], "diag": d, "desktop": WINDOW is not None}
    return out


def save_settings(sess, body):
    allowed = C.EDITABLE if sess["role"] == "admin" else C.USER_EDITABLE
    new = {k: v for k, v in body.items() if k in allowed}
    if not new:
        return {"ok": False, "msg": "Bu ayarları değiştirme yetkiniz yok."}
    if str(new.get("LIVE_ALLOWED")).lower() in ("true", "1", "on") and not C.LIVE_ALLOWED:
        if body.get("confirm") != "EVET":
            return {"ok": False, "msg": "Gerçek işlemi açmak için onay kutusuna EVET yazın."}
        if not ((new.get("API_KEY") or C.API_KEY) and (new.get("API_SECRET") or C.API_SECRET)):
            return {"ok": False, "msg": "Önce Binance API anahtarı ve secret girilmeli (kullanıcı veya admin)."}
    engine_keys = {"API_KEY", "API_SECRET", "UNIVERSE_SIZE"}
    if "SERVER_MODE" in new and bool(new["SERVER_MODE"]) != C.SERVER_MODE:
        C.save_settings({"SERVER_MODE": new.pop("SERVER_MODE")})
        if not new:
            return {"ok": True, "msg": "Kaydedildi. Sunucu modu uygulama yeniden başlayınca etkin olur ('Yeniden başlat')."}

    def changed(k):
        cur = getattr(C, k)
        try:
            return new[k] != "" and type(cur)(new[k]) != cur
        except (TypeError, ValueError):
            return True
    restart = any(k in engine_keys and changed(k) for k in new)
    if "CLAUDE_EFFORT" in new and new["CLAUDE_EFFORT"] not in ("low", "medium", "high"):
        new.pop("CLAUDE_EFFORT")
    C.save_settings(new)
    CACHE.pop("claude", None)
    CACHE.pop("brain", None)
    if restart:
        threading.Thread(target=ENGINE.restart, daemon=True).start()
    return {"ok": True, "msg": "Kaydedildi" + (", motor yeniden başlatılıyor." if restart else ".")}


AVATAR_DIR = os.path.join(C.APP_DIR, "profil")
IMG = {b"\x89PNG": ("png", "image/png"), b"\xff\xd8": ("jpg", "image/jpeg"), b"RIFF": ("webp", "image/webp"),
       b"GIF8": ("gif", "image/gif")}


MAX_BODY = 2_000_000


def avatar_file(user):
    key = re.sub(r"[^a-z0-9_.-]", "_", (user or "").lower())
    for ext in ("webp", "jpg", "png", "gif"):
        p = os.path.join(AVATAR_DIR, f"{key}.{ext}")
        if os.path.exists(p):
            return p
    return None


def save_avatar(user, data_url):
    import base64
    old = avatar_file(user)
    if old:
        os.remove(old)
    if not data_url:
        return None
    try:
        raw = base64.b64decode(str(data_url).split(",", 1)[-1], validate=True)
    except ValueError:
        return "Resim okunamadı."
    kind = next((v for k, v in IMG.items() if raw.startswith(k)), None)
    if not kind or len(raw) > 600_000:
        return "Desteklenmeyen veya çok büyük resim (png, jpg, webp)."
    os.makedirs(AVATAR_DIR, exist_ok=True)
    key = re.sub(r"[^a-z0-9_.-]", "_", user.lower())
    with open(os.path.join(AVATAR_DIR, f"{key}.{kind[0]}"), "wb") as fh:
        fh.write(raw)
    return None


def portfoy():
    import portfoy as PF
    b = ENGINE.bot
    out = {"backtest": PF.BACKTEST, "on": C.PORTFOY, "live": C.PORTFOY_LIVE, "core": C.PORTFOY_CORE,
           "training": PF.STATE["training"], "model_t": PF.STATE["model_t"], "kontrol": __import__("kontrolor").card()}
    if not b:
        return out
    with b.lock:
        pf = copy.deepcopy({k: v for k, v in (b.ag.get("pf") or {}).items() if k != "age"})
        st, bid = b.accounts["PAPER"].st, dict(b.bid)
        held = {s: p["qty"] * bid.get(s, p["entry"]) for s, p in st["positions"].items() if p.get("kind") == PF.KIND}
        base = st["cash"] + sum(held.values())
        track = PF.track(st["trades"], st["curve"], bid.get(C.BTC))
    out.update(pf=pf, base=round(base, 2), track=track,
               weights={s: round(v / base, 4) for s, v in held.items()} if base > 0 else {})
    return out


def history_file(user):
    return os.path.join(C.APP_DIR, f"sohbet_u_{re.sub(r'[^a-z0-9_.-]', '_', user.lower())}.json")


CHAT_GATE = {}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json", cookie=None):
        data = body if isinstance(body, bytes) else json.dumps(clean(body), ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        if cookie is not None:
            self.send_header("Set-Cookie", f"kqb={cookie}; HttpOnly; SameSite=Strict; Path=/"
                             + (f"; Max-Age={30 * 86400}" if cookie else "; Max-Age=0")
                             + ("; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""))
        self.end_headers()
        self.wfile.write(data)

    def _guard(self, post):
        import sunucu
        host = self.headers.get("Host") or ""
        loop = re.fullmatch(r"(127\.0\.0\.1|localhost)(:\d+)?", host)
        ok = host in HOSTS or bool(loop) or C.SERVER_MODE or host == sunucu.tunnel_host() or \
            host in C.PUBLIC_HOST
        return ok and (not post or self.headers.get("X-KQB") == "1")

    def _local(self):
        return self.client_address[0] in ("127.0.0.1", "::1") and self.headers.get("Host") in HOSTS

    def _token(self):
        c = SimpleCookie(self.headers.get("Cookie") or "")
        return c["kqb"].value if "kqb" in c else None

    def do_GET(self):
        if not self._guard(False):
            return self._send(403, {"error": "forbidden"})
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path in ("/", "/lw.js"):
                name, ctype = ("ui.html", "text/html") if u.path == "/" else ("lw.js", "application/javascript")
                with open(os.path.join(C.BASE_DIR, name), "rb") as fh:
                    return self._send(200, fh.read(), ctype)
            if u.path == "/api/app":
                return self._send(200, {"desktop": WINDOW is not None, "max": MAXED})
            if u.path == "/api/health":
                if not self._local():
                    return self._send(403, {"ok": False})
                b = ENGINE.bot
                age = round(time.time() - b.updated) if b else None
                return self._send(200, {"ok": bool(b) and age < 180, "age": age, "status": b.status if b else "baslatiliyor",
                                        **(b.health() if b else {})})
            if u.path == "/api/avatar":
                f = avatar_file(q.get("u"))
                if not f:
                    return self._send(404, {"error": "yok"})
                with open(f, "rb") as fh:
                    return self._send(200, fh.read(), next(v[1] for v in IMG.values() if f.endswith("." + v[0])))
            sess = auth.session(self._token())
            if u.path == "/api/auth/me":
                f = sess and avatar_file(sess["user"])
                return self._send(200, {"user": sess and sess["user"], "role": sess and sess["role"],
                                        "setup": not auth.has_users(), "avatar": int(os.path.getmtime(f)) if f else 0,
                                        "gelistir": bool(sess and sess["role"] == "admin" and os.environ.get("KQB_GELISTIRME") == "1"
                                                         and __import__("gelistir").etkin())})
            if not sess:
                return self._send(401, {"error": "giris gerekli"})
            admin = sess["role"] == "admin"
            b = ENGINE.bot
            mode = mode_for(sess, q)
            acct = b and b.accounts.get(mode)
            routes = {"/api/state": lambda: state(sess, q), "/api/screener": screener, "/api/opps": opportunities,
                      "/api/decisions": lambda: decisions(sess), "/api/settings": lambda: get_settings(sess),
                      "/api/chat/history": lambda: __import__("chat").history_load(history_file(sess["user"])),
                      "/api/trades": lambda: copy.deepcopy(acct.st["trades"]) if acct else [],
                      "/api/notes": lambda: [dict(n, text=n["text"] if admin else hide_paper(n["text"]))
                                             for n in (b.notes if b else []) if admin or n.get("acct") in (None, "LIVE")],
                      "/api/candles": lambda: candles(q.get("sym", C.BTC), q.get("tf", "1h"), mode, q.get("son") == "1"),
                      "/api/macro": macro, "/api/whale": lambda: __import__("zincir").Zincir.shared().snapshot(), "/api/lessons": lambda: lessons(sess), "/api/portfoy": portfoy,
                      "/api/news": lambda: __import__("haberler").view(admin, hidden=q.get("hepsi") == "1"),
                      "/api/telegram": lambda: __import__("telegram_bot").status(sess["user"]),
                      "/api/mc": lambda: montecarlo(q, mode)}
            if admin:
                routes["/api/users"] = auth.users
                routes["/api/update"] = lambda: __import__("guncelle").status()
                routes["/api/server"] = lambda: __import__("sunucu").status()
                routes["/api/history"] = history
                routes["/api/hazirlik"] = hazirlik_rapor
                if os.environ.get("KQB_GELISTIRME") == "1":
                    routes["/api/gelistir"] = lambda: __import__("gelistir").durum()
                routes["/api/memory"] = lambda: {"text": __import__("hafiza").tail(60000)}
            if u.path in routes:
                return self._send(200, routes[u.path]())
            self._send(404, {"error": "yok"})
        except Exception as e:
            log.exception("API")
            self._send(500, {"error": str(e)})

    def do_POST(self):
        if not self._guard(True):
            return self._send(403, {"error": "forbidden"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if not 0 <= n <= MAX_BODY:
                return self._send(413, {"error": "istek cok buyuk"})
            body = json.loads(self.rfile.read(n) or b"{}")
        except ValueError:
            return self._send(400, {"error": "gecersiz istek"})
        p = self.path
        if p in ("/api/window", "/api/show") and not self._local():
            return self._send(403, {"ok": False})
        if p == "/api/window":
            win_action(body.get("a"))
            return self._send(200, {"ok": True, "max": MAXED})
        if p == "/api/show":
            if WINDOW:
                goster()
            return self._send(200, {"ok": True})
        if p == "/api/auth/setup":
            err = auth.create(str(body.get("user", "")).strip(), str(body.get("password", "")), "admin", first=True)
            if err:
                return self._send(200, {"ok": False, "msg": err})
            body["role"] = "admin"
            p = "/api/auth/login"
        if p == "/api/auth/login":
            tok, role, err = auth.login(str(body.get("user", "")).strip(), str(body.get("password", "")))
            if err:
                return self._send(200, {"ok": False, "msg": err})
            return self._send(200, {"ok": True, "role": role}, cookie=tok)
        if p == "/api/auth/logout":
            auth.logout(self._token())
            return self._send(200, {"ok": True}, cookie="")
        sess = auth.session(self._token())
        if not sess:
            return self._send(401, {"ok": False, "msg": "Oturum kapandı, tekrar giriş yapın."})
        admin, b = sess["role"] == "admin", ENGINE.bot
        mode = mode_for(sess, body)
        if p.startswith("/api/gelistir/"):
            if not admin or os.environ.get("KQB_GELISTIRME") != "1" or not __import__("gelistir").etkin():
                return self._send(403, {"ok": False, "msg": "Geliştirme bu botta kapalı ya da yetkiniz yok."})
            import gelistir as G
            if not G.etkin():
                return self._send(403, {"ok": False, "msg": "Geliştirme bu botta kapalı ya da yetkiniz yok."})
            islem = {"/api/gelistir/baslat": lambda: G.baslat(body.get("istek"), sess["user"]),
                     "/api/gelistir/uygula": lambda: G.uygula(body.get("kod"), quit_app),
                     "/api/gelistir/vazgec": G.vazgec,
                     "/api/gelistir/geri_al": lambda: G.geri_al(str(body.get("sha", "")), quit_app)}.get(p)
            if not islem:
                return self._send(404, {"ok": False, "msg": "yok"})
            err = islem()
            return self._send(200, {"ok": not err, "msg": err or "Tamam."})
        if p == "/api/hazirlik/binance":
            if not admin:
                return self._send(403, {"ok": False, "msg": "Sadece admin."})
            lv = b and b.accounts.get("LIVE")
            if not lv:
                return self._send(200, {"ok": False, "msg": "Gerçek hesap anahtarı girilmedi."})
            import hazirlik
            b.ag["hazirlik"] = hazirlik.binance_kontrol(lv, dict(lv.bid))
            return self._send(200, {"ok": True})
        if p == "/api/chat":
            import chat
            gate = CHAT_GATE.setdefault(sess["user"].lower(), threading.Semaphore(1))
            if not gate.acquire(blocking=False):
                return self._send(429, b"Bir cevap zaten yaziliyor, bitmesini bekleyin.", "text/plain")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                gen = chat.ask_stream(str(body.get("q", ""))[:2000], b, sess["role"], history_file(sess["user"])) if b \
                    else iter(["Motor henüz başlamadı, birkaç saniye sonra tekrar deneyin."])
                try:
                    for part in gen:
                        self.wfile.write(part.encode())
                        self.wfile.flush()
                finally:
                    if hasattr(gen, "close"):
                        gen.close()
            finally:
                gate.release()
            return
        if p == "/api/chat/clear":
            import chat
            chat.history_save(history_file(sess["user"]), [])
            return self._send(200, {"ok": True})
        if p == "/api/auth/password":
            ok = auth.login(sess["user"], str(body.get("old", "")))[0]
            if not ok:
                return self._send(200, {"ok": False, "msg": "Mevcut şifre hatalı."})
            auth.logout(ok)
            err = auth.set_password(sess["user"], str(body.get("new", "")))
            if err:
                return self._send(200, {"ok": False, "msg": err})
            tok = auth.login(sess["user"], str(body.get("new", "")))[0]
            return self._send(200, {"ok": True, "msg": "Şifre değişti; diğer oturumlar kapatıldı."}, cookie=tok)
        if p == "/api/settings":
            return self._send(200, save_settings(sess, body))
        if p == "/api/avatar":
            err = save_avatar(sess["user"], body.get("data"))
            return self._send(200, {"ok": not err, "msg": err or ("Profil fotoğrafı kaydedildi." if body.get("data")
                                                                   else "Profil fotoğrafı kaldırıldı.")})
        if p.startswith("/api/telegram/"):
            import telegram_bot as T
            act = p.rsplit("/", 1)[1]
            if act == "link":
                err = T.link(sess["user"], int(body.get("chat_id") or 0))
                return self._send(200, {"ok": not err, "msg": err or "Telegram eşleşti; telefonunuza menü gönderildi."})
            if act == "unlink":
                T.unlink(sess["user"])
                return self._send(200, {"ok": True, "msg": "Telegram bağlantısı kaldırıldı."})
            if act == "demo":
                lk = T.links().get(sess["user"].lower())
                if not lk:
                    return self._send(200, {"ok": False, "msg": "Önce Telegram'ı eşleştirin."})
                threading.Thread(target=T.send_demo, args=(lk["chat_id"],), daemon=True).start()
                return self._send(200, {"ok": True, "msg": "Örnek bildirimler telefonunuza gönderiliyor."})
        if p == "/api/pause":
            set_pause(mode, bool(body.get("on")))
            return self._send(200, {"ok": True})
        if p in ("/api/position/levels", "/api/position/auto", "/api/position/timer", "/api/manual/buy"):
            a = b and b.accounts.get(mode)
            if not a:
                return self._send(200, {"ok": False, "msg": "Hesap bağlı değil."})
            num = lambda k: float(body[k]) if body.get(k) not in (None, "") else None
            sym = B_norm(body.get("sym"))
            try:
                if p == "/api/manual/buy":
                    cikis = body.get("cikis", "suresiz")
                    err = b.manual_buy(mode, sym, num("usdt"), num("stop") or 0, num("tp"), body.get("entry", "taker"), sess["user"], cikis, num("saat"))
                    msg = "Elle alış emri verildi (yapay zekaya bırakıldı)." if cikis == "ai" else "Elle alış emri verildi (Manuel Kilit açık)."
                elif p == "/api/position/timer":
                    with b.lock:
                        err = a.set_timer(sym, num("saat"), sess["user"])
                    msg = "Otomatik satış süresi kaydedildi." if num("saat") else "Otomatik satış iptal edildi."
                else:
                    with b.lock:
                        err = a.set_levels(sym, num("sl"), num("tp"), sess["user"]) if p.endswith("levels") \
                            else a.set_auto(sym, sess["user"])
                    msg = "Stop/hedef kaydedildi; Manuel Kilit açık." if p.endswith("levels") else "Kontrol yapay zekaya devredildi."
            except (ValueError, TypeError):
                err = "Geçersiz fiyat."
            except Exception as e:
                err = f"Hata: {str(e)[:160]}"
            return self._send(200, {"ok": not err, "msg": err or msg})
        if p == "/api/close":
            a = b and b.accounts.get(mode)
            if not a:
                return self._send(200, {"ok": False, "msg": "Hesap yok."})
            import ccxt
            with b.lock:
                busy = lambda: a.st["positions"].keys() | a.st["pending"].keys()
                syms = list(busy()) if body.get("all") else [str(body.get("sym"))]
                errs = []
                for s in syms:
                    try:
                        a.close(s, "ELLE_KAPATILDI")
                    except ccxt.BaseError as e:
                        errs.append(f"{s}: {str(e)[:100]}")
                done = len([s for s in syms if s not in busy()])
            b.say(f"{done} pozisyon/emir elle kapatıldı ({sess['user']}).", kind="islem", acct=mode)
            return self._send(200, {"ok": not errs, "msg": f"{done} kapatıldı." + (" Hata: " + "; ".join(errs) if errs else "")})
        if not admin:
            return self._send(403, {"ok": False, "msg": "Bu işlem için admin yetkisi gerekli."})
        if p == "/api/retrain":
            if b:
                b.force_retrain = True
            return self._send(200, {"ok": True})
        if p in ("/api/cli/install", "/api/cli/pick", "/api/cli/clear"):
            import agent as A
            CACHE.pop("claude", None)
            CACHE.pop("brain", None)
            if p == "/api/cli/install":
                A.cli_install()
                return self._send(200, {"ok": True, "msg": "Resmi Claude Code kurulumu açılan pencerede başladı. Bitince "
                                                           "'Claude oturumu aç', sonra 'Kontrol et'e basın."})
            if p == "/api/cli/clear":
                C.save_settings({"CLAUDE_CLI_PATH": ""})
                return self._send(200, {"ok": True, "msg": "Elle seçilen yol kaldırıldı; otomatik bulucu kullanılacak."})
            path = str(body.get("path") or "").strip().strip('"')
            if not path and WINDOW:
                import webview
                sel = WINDOW.create_file_dialog(webview.OPEN_DIALOG, file_types=("Claude CLI (claude*.exe;claude*.cmd)",
                                                                                   "Tüm dosyalar (*.*)"))
                path = sel[0] if sel else ""
            if not path:
                return self._send(200, {"ok": False, "msg": "Dosya seçilmedi (claude.exe veya claude.cmd)."})
            if not os.path.isfile(path) or not os.path.basename(path).lower().startswith("claude"):
                return self._send(200, {"ok": False, "msg": "Geçerli bir claude.exe / claude.cmd dosyası değil."})
            C.save_settings({"CLAUDE_CLI_PATH": path})
            d = A.cli_diagnose()
            return self._send(200, {"ok": d["path"] == path, "msg": d["why"] if d["path"] == path else
                                    "Seçilen dosya çalışmadı (--version cevap vermedi)."})
        if p in ("/api/cli/login", "/api/cli/check"):
            import agent as A
            err = A.cli_login() if p == "/api/cli/login" else None
            if p == "/api/cli/check":
                A.CLI.update(ok=None, down_until=0)
            CACHE.pop("claude", None)
            CACHE.pop("brain", None)
            ready = A.cli_ready() if p == "/api/cli/check" else None
            return self._send(200, {"ok": not err, "msg": err or (
                "Açılan pencerede tarayıcıdan Claude hesabınızla giriş yapın, sonra 'Kontrol et'e basın."
                if ready is None else ("Claude CLI köprüsü hazır." if ready else A.CLI["why"]))})
        if p == "/api/history/run":
            import gecmis as G
            if G.STATE["running"]:
                return self._send(200, {"ok": False, "msg": "Geçmiş Okulu zaten çalışıyor."})
            say = (lambda t: b.say(t, kind="oz")) if b else (lambda t: None)
            threading.Thread(target=G.run, args=(say,), name="gecmis", daemon=True).start()
            return self._send(200, {"ok": True, "msg": "Geçmiş Okulu başladı: veri güncelleniyor, tüm coinler test "
                                                       "ediliyor (15-40 dk). Sonuç Ders Defteri sayfasında."})
        if p == "/api/news/unblock":
            __import__("haberler").unblock()
            return self._send(200, {"ok": True, "msg": "Engel kaldırıldı; hedefler sonraki dengelemede geri alınır."})
        if p == "/api/portfoy/run":
            started = b and b.run_portfolio()
            return self._send(200, {"ok": bool(started), "msg": "Portföy yeniden dengeleniyor (1 dk sürebilir)..."
                                    if started else "Dengeleme zaten sürüyor, birazdan tekrar bakın."})
        if p == "/api/decide":
            started = b and b._background("Elle karar", lambda: b.decide("elle"))
            return self._send(200, {"ok": bool(started), "msg": "Karar veriliyor..." if started else
                                    "Motor meşgul, birazdan tekrar deneyin."})
        if p == "/api/paper/reset":
            if b:
                with b.lock:
                    pa = b.accounts["PAPER"]
                    pa.st.update(cash=C.PAPER_BALANCE, start_equity=None, positions={}, pending={}, trades=[], pnl=0.0,
                                 wins=0, losses=0, eq_hist=[], pause_until=0, curve=[], day={"d": "", "eq": 0.0},
                                 cooldown={}, stops=[], principal=None, peak=0.0)
                b.say(f"Sanal kasa sıfırlandı: {C.PAPER_BALANCE:g} USDT.", acct="PAPER")
            return self._send(200, {"ok": True, "msg": "Sanal kasa sıfırlandı."})
        if p.startswith("/api/update/") or p in ("/api/app/restart", "/api/server/tunnel"):
            import guncelle
            import sunucu
            if p == "/api/update/check":
                return self._send(200, {"ok": True, **guncelle.check()})
            if p == "/api/update/apply":
                st = guncelle.status()
                if not st["frozen"]:
                    return self._send(200, {"ok": False, "msg": "Geliştirici modunda güncelleme uygulanmaz; exe sürümünde çalışır."})
                res = {}
                t = threading.Thread(target=lambda: res.update(err=guncelle.apply(quit_app)), daemon=True)
                t.start()
                t.join(3)
                if res.get("err"):
                    return self._send(200, {"ok": False, "msg": res["err"]})
                return self._send(200, {"ok": True, "msg": "Güncelleme indiriliyor; bitince uygulama kendini yeniden başlatacak "
                                                           "(veri klasörü korunur)."})
            if p == "/api/app/restart":
                self._send(200, {"ok": True, "msg": "Uygulama yeniden başlatılıyor..."})
                threading.Thread(target=lambda: (time.sleep(0.5), guncelle.restart(quit_app)), daemon=True).start()
                return
            if body.get("on"):
                err = sunucu.tunnel_start()
                return self._send(200, {"ok": not err, "msg": err or f"Tünel açık: {sunucu.TUN['url']}"})
            sunucu.tunnel_stop()
            return self._send(200, {"ok": True, "msg": "Tünel kapatıldı."})
        if p == "/api/principal":
            a = b and b.accounts.get(mode)
            if not a:
                return self._send(200, {"ok": False, "msg": "Hesap yok."})
            with b.lock:
                eq = a.equity()
                a.st.update(principal=eq, peak=eq)
            b.say(f"Anapara {eq:.2f} USDT olarak güncellendi ({sess['user']}).", acct=mode)
            return self._send(200, {"ok": True, "msg": f"Anapara {eq:.2f} USDT olarak ayarlandı."})
        if p == "/api/users":
            act, name = body.get("action"), str(body.get("name", "")).strip()
            if act == "create":
                err = auth.create(name, str(body.get("password", "")), body.get("role", "user"))
            elif act == "delete":
                err = "Kendi hesabınızı silemezsiniz." if name.lower() == sess["user"].lower() else auth.delete(name)
                if not err:
                    if os.path.exists(history_file(name)):
                        os.remove(history_file(name))
                    save_avatar(name, None)
                    __import__("telegram_bot").unlink(name)
            elif act == "reset":
                err = auth.set_password(name, str(body.get("password", "")))
            else:
                err = "Geçersiz işlem."
            return self._send(200, {"ok": not err, "msg": err or "Tamam."})
        if p == "/api/quit":
            self._send(200, {"ok": True})
            threading.Thread(target=quit_app, daemon=True).start()
            return
        self._send(404, {"error": "yok"})


def set_pause(mode, on):
    f = C.pause_flag(mode)
    if on:
        open(f, "w").close()
    elif os.path.exists(f):
        os.remove(f)


WINDOW, TRAY, MAXED, CLIENT = None, None, True, False


def work_area():
    try:
        import ctypes
        from ctypes import wintypes
        r = wintypes.RECT()
        if ctypes.windll.user32.SystemParametersInfoW(0x30, 0, ctypes.byref(r), 0):
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except (AttributeError, OSError):
        pass
    s = __import__("webview").screens[0]
    return 0, 0, s.width, s.height


YEREL = None
YEREL_HAZIR = threading.Event()
KENAR = {"tasi": 2, "l": 10, "r": 11, "t": 12, "tl": 13, "tr": 14, "b": 15, "bl": 16, "br": 17}


def yerel_cerceve():
    global YEREL, _wndproc
    import ctypes
    from ctypes import wintypes
    from webview.platforms.winforms import BrowserView
    u = ctypes.windll.user32
    L = ctypes.c_ssize_t
    WNDPROC = ctypes.WINFUNCTYPE(L, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, L)
    u.SetWindowLongPtrW.restype = L
    u.SetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int, L)
    u.GetWindowLongPtrW.restype = L
    u.GetWindowLongPtrW.argtypes = (wintypes.HWND, ctypes.c_int)
    u.CallWindowProcW.restype = L
    u.CallWindowProcW.argtypes = (L, wintypes.HWND, wintypes.UINT, ctypes.c_size_t, L)
    hwnd = int(BrowserView.instances[WINDOW.uid].Handle.ToInt64())
    eski = u.GetWindowLongPtrW(hwnd, -4)

    def proc(h, msg, wp, lp):
        if msg == 0x83 and wp:
            if u.IsZoomed(h):
                r = wintypes.RECT.from_address(lp)
                pad = u.GetSystemMetrics(92)
                x, y = u.GetSystemMetrics(32) + pad, u.GetSystemMetrics(33) + pad
                r.left, r.top, r.right, r.bottom = r.left + x, r.top + y, r.right - x, r.bottom - y
            return 0
        return u.CallWindowProcW(eski, h, msg, wp, lp)
    _wndproc = WNDPROC(proc)
    u.SetWindowLongPtrW(hwnd, -4, ctypes.cast(_wndproc, ctypes.c_void_p).value)
    u.SetWindowLongPtrW(hwnd, -16, u.GetWindowLongPtrW(hwnd, -16) | 0xC00000 | 0x40000 | 0x20000 | 0x10000 | 0x80000)
    u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x27)
    u.ShowWindow(hwnd, 3)
    YEREL = hwnd


def yerel_kur():
    try:
        yerel_cerceve()
    except Exception:
        logging.exception("yerel pencere cercevesi kurulamadi")
    YEREL_HAZIR.set()


def goster():
    WINDOW.show()
    if YEREL:
        import ctypes
        u = ctypes.windll.user32
        if u.IsIconic(YEREL):
            u.ShowWindow(YEREL, 9)
        u.SetForegroundWindow(YEREL)
    else:
        WINDOW.restore()


def win_action(act):
    global MAXED
    if not WINDOW or act not in ("minimize", "max", "close"):
        return
    if act == "minimize":
        WINDOW.minimize()
    elif act == "max" and YEREL:
        import ctypes
        u = ctypes.windll.user32
        u.ShowWindow(YEREL, 9 if u.IsZoomed(YEREL) else 3)
    elif act == "max":
        x, y, w, h = work_area()
        if MAXED:
            w2, h2 = int(w * 0.8), int(h * 0.8)
            WINDOW.resize(w2, h2)
            WINDOW.move(x + (w - w2) // 2, y + (h - h2) // 2)
        else:
            WINDOW.resize(w, h)
            WINDOW.move(x, y)
        MAXED = not MAXED
    elif CLIENT:
        os._exit(0)
    else:
        WINDOW.hide()


class JsApi:
    def win(self, act):
        win_action(act)
        return {"max": MAXED}

    def pencere(self, act):
        if act == "yerel":
            YEREL_HAZIR.wait(10)
        if act == "yerel" or not YEREL:
            return bool(YEREL)
        import ctypes
        u = ctypes.windll.user32
        if u.GetAsyncKeyState(0x01) & 0x8000 and act in KENAR:
            from System import Func, Type
            from webview.platforms.winforms import BrowserView

            def basla():
                u.ReleaseCapture()
                u.SendMessageW(YEREL, 0xA1, KENAR[act], 0)
            BrowserView.instances[WINDOW.uid].BeginInvoke(Func[Type](basla))
        return True


def client_url():
    for f in (C.SUNUCU_URL_FILE, os.path.join(C.BASE_DIR, "sunucu_adresi.txt")):
        try:
            with open(f, encoding="utf-8") as fh:
                u = fh.read().strip()
            if u.startswith(("http://", "https://")):
                return u
        except OSError:
            pass
    return None


def run_client(url):
    global WINDOW, CLIENT
    import webview
    CLIENT = True
    if not os.path.exists(ICON):
        make_icon()
    x, y, w, h = work_area()
    WINDOW = webview.create_window("KriptoQuantBot", url, x=x + w // 10, y=y + h // 10, width=w * 4 // 5, height=h * 4 // 5, min_size=(800, 560),
                                   background_color="#0b0e14", frameless=True, easy_drag=False, js_api=JsApi())
    WINDOW.events.shown += yerel_kur
    webview.start(icon=ICON, private_mode=False, storage_path=os.path.join(C.APP_DIR, "pencere"))


def quit_app():
    __import__("sunucu").tunnel_stop()
    ENGINE.stop()
    if TRAY:
        TRAY.stop()
    os._exit(0)


def make_icon(path=ICON):
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8, 8, 248, 248], 56, fill=(23, 27, 35, 255))
    for x, (lo, o, c, hi), col in zip((60, 112, 164), ((170, 160, 120, 110), (150, 130, 90, 80), (110, 100, 55, 40)),
                                     ((34, 195, 142),) * 3):
        d.line([x + 16, hi, x + 16, lo], fill=col, width=6)
        d.rectangle([x, c, x + 32, o], fill=col)
    d.line([40, 200, 110, 150, 150, 170, 220, 70], fill=(91, 140, 255), width=10, joint="curve")
    img.save(path, sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])
    return img


def tray():
    import pystray
    from PIL import Image
    global TRAY

    def show(*_):
        if WINDOW:
            goster()
    paused = lambda: all(os.path.exists(C.pause_flag(m)) for m in ("PAPER", "LIVE"))
    menu = pystray.Menu(
        pystray.MenuItem("Paneli ac", show, default=True),
        pystray.MenuItem(lambda _: "Devam et" if paused() else "Yeni islemleri duraklat",
                         lambda *_: [set_pause(m, not paused()) for m in ("PAPER", "LIVE")]),
        pystray.MenuItem("Cikis (botu kapat)", lambda *_: threading.Thread(target=quit_app, daemon=True).start()))
    TRAY = pystray.Icon("KriptoQuantBot", Image.open(ICON), "KriptoQuantBot", menu)
    TRAY.run_detached()


def update_watch():
    import guncelle
    time.sleep(60)
    while True:
        if C.AUTO_UPDATE_CHECK and (C.UPDATE_REPO or guncelle.GIT):
            st = guncelle.check()
            v = (st.get("latest") or {}).get("version")
            if st["newer"] and v != guncelle.STATE["notified"] and ENGINE.bot:
                guncelle.STATE["notified"] = v
                ENGINE.bot.say(f"Yeni güncelleme var ({v}). Ayarlar → Güncelleme → Tek tıkla güncelle. Hesaplar, kasa ve "
                               "Telegram bağlantısı korunur.", True, "uyari")
        time.sleep(3600 if guncelle.GIT else 6 * 3600)


def migrate_old_files():
    if not getattr(sys, "frozen", False):
        for name in ("data", "model.pkl", "state.json", "settings.json", "tahmin_defteri.csv"):
            src, dst = os.path.join(C.BASE_DIR, name), os.path.join(C.APP_DIR, name)
            if os.path.exists(src) and not os.path.exists(dst):
                shutil.move(src, dst)
    old = os.path.join(C.APP_DIR, "durdur.flag")
    if os.path.exists(old):
        os.replace(old, C.pause_flag("PAPER"))
    for f in ("sohbet.json", "bot.log.1"):
        if os.path.exists(os.path.join(C.APP_DIR, f)):
            os.remove(os.path.join(C.APP_DIR, f))
    C.load_settings()


def main():
    global WINDOW
    url = None if "--sunucu" in sys.argv else client_url()
    if url:
        return run_client(url)
    migrate_old_files()
    logging.basicConfig(filename=C.LOG_FILE, level=logging.INFO, encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    for h in logging.getLogger().handlers:
        h.addFilter(C.Maske())
    if not os.path.exists(ICON):
        make_icon()
    ThreadingHTTPServer.allow_reuse_address = False
    try:
        server = ThreadingHTTPServer(("0.0.0.0" if C.SERVER_MODE else "127.0.0.1", C.PORT), Handler)
        C.SERVER_ACTIVE = C.SERVER_MODE
    except OSError:
        import urllib.request
        try:
            urllib.request.urlopen(urllib.request.Request(URL + "/api/show", b"{}", {"X-KQB": "1"}), timeout=5)
        except OSError:
            pass
        return
    ENGINE.start()
    import telegram_bot
    threading.Thread(target=telegram_bot.poll_forever, name="telegram", daemon=True).start()
    threading.Thread(target=update_watch, name="guncelleme", daemon=True).start()
    if "--sunucu" in sys.argv:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            try:
                ENGINE.bot and ENGINE.bot.save()
            finally:
                os._exit(0)
        return
    threading.Thread(target=server.serve_forever, daemon=True).start()
    import webview
    x, y, w, h = work_area()
    WINDOW = webview.create_window("KriptoQuantBot", URL, x=x + w // 10, y=y + h // 10, width=w * 4 // 5, height=h * 4 // 5, min_size=(800, 560),
                                   background_color="#0b0e14", frameless=True, easy_drag=False, js_api=JsApi())
    WINDOW.events.shown += yerel_kur

    def on_closing():
        threading.Thread(target=WINDOW.hide, daemon=True).start()
        return False
    WINDOW.events.closing += on_closing
    tray()
    webview.start(icon=ICON)


if __name__ == "__main__":
    main()
