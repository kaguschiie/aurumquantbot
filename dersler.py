"""Ders Defteri: otopsi + kendi kendine ogrenen sinyal agirliklari (tamamen yerel, ucretsiz, SQLite).
- Her kapanan islem otopsi edilir: giristeki sinyaller, cikistaki teknik durum, BTC hareketi -> ders.
- Her kararda adaylar da 'ornek' olarak kaydedilir; 24 saat sonra botun cikis politikasiyla (stop 1,5 ATR, hedef
  3,2 ATR, basabas + kar kilitleyen iz suren stop) sonucu Binance mumlarindan hesaplanir. Alinmayip sert yukselen
  aday = kacan firsat.
- Sinyal agirliklari bu orneklerin gercek sonucundan (R) guncellenir; tutarli zarar ettiren kombinasyonlar 'tuzak'
  olur ve yerel risk yoneticisi onlari engeller (Claude dahil hicbir beyin ayni tuzaga tekrar giremez)."""
import itertools
import json
import logging
import sqlite3
import threading
import time
from contextlib import contextmanager

import ccxt

import config as C

log = logging.getLogger("bot")
HORIZON_H = 24         # radar ornegi bu kadar saat sonra sonuclandirilir
WINDOW_D = 60          # ogrenme son 60 gunun orneklerine bakar (piyasa karakteri degisir)
SHRINK = 20            # az ornekli sinyalin agirligi sifira cekilir: w = (ort - taban) * n / (n + 20)
REAL_W = 2.0           # gercek islem otopsisi radar orneginden 2 kat agir sayilir
SAMPLE_GAP_H = 2       # ayni coin icin iki radar ornegi arasi en az sure
# tekli/ikili kombinasyon icin en az ornek ve ort R; kazanma orani olcut degil (cikis kuralinin urunu). Guven siniri:
# tuzakta ust sinir < 0, altinda alt sinir > 0 (tek tuk sansli ornek kural olmasin)
TRAP = {"n1": 12, "n2": 8, "avg": -0.15}
GOLD = {"n1": 12, "n2": 8, "avg": 0.15}
Z = 1.28  # tek yonlu %90 guven
CONTEXT = {"rejim_dusus", "rejim_trend", "makro_riskoff", "altin"}  # tek basina tuzak olamaz (her seyi engellerdi)
REG_TR = {"trend": "yükseliş trendi", "yatay": "yatay", "dusus": "sert düşüş"}
REASON_TR = {"HEDEF": "hedef", "STOP": "stop", "IZ_STOP": "iz süren stop", "BASABAS": "başabaş stopu",
             "SURE_DOLDU": "süre doldu", "CLAUDE_KAPATTI": "karar beyni kapattı", "ELLE_KAPATILDI": "elle kapatıldı",
             "ZAMAN_STOPU": "zaman stopu (ilerlemedi)", "BALINA_SATIS": "balina satışı refleksi", "PORTFOY_CIKIS": "portföy dengeleme",
             "GUNLUK_CIKIS": "10 günlük dip kapanışı (trend bitti)"}
_LOCK = threading.RLock()
STATE = {"weights": {}, "traps": [], "golden": [], "real": [], "n": 0, "base": 0.0, "t": 0.0, "dirty": True,
         "regstats": {}, "wreg": {}}


# ---------------- sinyaller ----------------
def _n(r, k):
    v = r.get(k)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v == v else None


def _t(r, tf):
    v = (r.get("tech") or {}).get(tf)
    return v if isinstance(v, (int, float)) and v == v else None


def _ge(v, x):
    return v is not None and v >= x


def _lt(v, x):
    return v is not None and v < x


SIGNALS = {  # ad: (Turkce aciklama, kosul(satir, baglam))
    "trend_1s": ("1s teknik güçlü al", lambda r, c: _ge(_t(r, "1h"), 0.5)),
    "trend_4s": ("4s teknik al", lambda r, c: _ge(_t(r, "4h"), 0.3)),
    "zayif_4s": ("4s trend zayıf", lambda r, c: _lt(_t(r, "4h"), 0.1)),
    "gunluk_asagi": ("günlük trend aşağı", lambda r, c: _lt(_t(r, "1d"), -0.1)),
    "ivme_15dk": ("15dk ivme güçlü", lambda r, c: _ge(_t(r, "15m"), 0.3)),
    "hacim_patlamasi": ("hacim patlaması (3x+)", lambda r, c: _ge(_n(r, "vspike"), 3)),
    "btc_ayrisma": ("BTC'den pozitif ayrışma", lambda r, c: _ge(_n(r, "rs24"), 5)),
    "kirilim": ("24s zirvesini kırdı", lambda r, c: bool(r.get("breakout"))),
    "rsi_asiri": ("aşırı alım (RSI 70+)", lambda r, c: _ge(_n(r, "rsi"), 70)),
    "rsi_dusuk": ("RSI 40 altı", lambda r, c: _lt(_n(r, "rsi"), 40)),
    "pompa": ("24s'te %15+ yükselmiş", lambda r, c: _ge(_n(r, "chg24"), 15)),
    "defter_alici": ("emir defteri alıcı", lambda r, c: _ge(_n(r, "ob"), 0.6)),
    "defter_satici": ("emir defteri satıcı", lambda r, c: _lt(_n(r, "ob"), 0.45)),
    "fonlama_yuksek": ("fonlama yüksek (kalabalık long)", lambda r, c: _ge(_n(r, "funding"), 0.0004)),
    "balina_alim": ("balinalar alıyor", lambda r, c: _ge(_n(r, "whale"), 0.3)),
    "balina_satis": ("balinalar satıyor", lambda r, c: _lt(_n(r, "whale"), -0.3)),
    "alici_akisi": ("agresif alıcı akışı", lambda r, c: _ge(_n(r, "flow3"), 0.05)),
    "rejim_dusus": ("kripto sert düşüşte", lambda r, c: c.get("regime") == "dusus"),
    "rejim_trend": ("kripto yükseliş trendinde", lambda r, c: c.get("regime") == "trend"),
    "makro_riskoff": ("küresel piyasa risk-off", lambda r, c: c.get("macro") == "riskoff"),
    "altin": ("altın (PAXG)", lambda r, c: str(r.get("sym", "")).split("/")[0] in ("PAXG", "XAUT")),
}


def label(k):
    return SIGNALS.get(k, (k,))[0]


def labels(ks):
    return " + ".join(label(k) for k in ks)


def signals(r, ctx):
    return {k for k, (_, f) in SIGNALS.items() if f(r, ctx)}


def feats(r, ctx):
    out = {k: _n(r, k) for k in ("price", "chg24", "rs24", "vspike", "rsi", "ob", "funding", "atr_pct", "score", "whale",
                                 "flow3", "rs4", "r1h")}
    out["breakout"] = bool(r.get("breakout"))
    out.update({f"t_{tf}": _t(r, tf) for tf in ("15m", "1h", "4h", "1d")})
    out.update(regime=ctx.get("regime"), macro=ctx.get("macro"), vol=ctx.get("vol"))
    return out


# ---------------- sifir zarar kalkani (canli bot ve geriye donuk simulasyon ayni kurali kullanir) ----------------
def be_price(entry, atr=None):
    """Basabas stopu: giris + iki yon komisyon + stop kaymasi + tampon. Tampon oynak coinde buyur (ATR'nin %20'si,
    en fazla %2): bir saniyelik fiyat boslugunda bile cikis zararsiz kalsin."""
    lock = max(C.BE_LOCK_PCT / 100, min(0.2 * atr / entry, 0.02) if atr else 0)
    return entry * (1 + 2 * C.FEE + C.SLIPPAGE + lock)


def be_trigger(entry, r0, be_r=None, atr=None):
    """Stopun basabasa cekilmesi icin gereken kar: ilk riskin BE_TRIGGER_R kati, en az basabas seviyesi + %0,2."""
    return max((C.BE_TRIGGER_R if be_r is None else be_r) * r0, be_price(entry, atr) - entry + entry * 0.002)


def trail(entry, r0, hi, atr, trail_atr=None):
    """Kar kilidi: zirveden TRAIL_ATR geride; 1R yolu gecince kazanilan yolun en az LOCK_FRAC'i kilitli."""
    lock = hi - (C.TRAIL_ATR if trail_atr is None else trail_atr) * atr
    if hi - entry >= r0:
        lock = max(lock, entry + C.LOCK_FRAC * (hi - entry))
    return lock


def simulate(entry, stop, tp, atr, bars, be_r=None, trail_atr=None):
    """Botun cikis politikasi. bars: [(yuksek, dusuk, kapanis)]. Donus: (net R, net getiri, en yuksek yukselis).
    Komisyon (iki yon) ve kayma dusulur. Ayni mumda stop ve hedef varsa once stop varsayilir (temkinli)."""
    r0 = entry - stop
    if r0 <= 0 or not bars:
        return 0.0, 0.0, 0.0
    sl, hi, be, out = stop, entry, False, None
    for h, low, c in bars:
        if low <= sl:
            out = sl
            break
        if h >= tp:
            out = tp
            break
        hi = max(hi, h)
        if not be and hi - entry >= be_trigger(entry, r0, be_r, atr):
            sl, be = max(sl, be_price(entry, atr)), True
        if be:
            sl = max(sl, min(trail(entry, r0, hi, atr, trail_atr), c * 0.998))
    out = bars[-1][2] if out is None else out
    ret = out / entry - 1 - 2 * C.FEE - C.SLIPPAGE
    return ret * entry / r0, ret, max(h for h, _, _ in bars) / entry - 1


# ---------------- veritabani ----------------
@contextmanager
def _db():
    con = sqlite3.connect(C.LESSON_DB, timeout=15)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def init():
    with _LOCK, _db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS ornek (id INTEGER PRIMARY KEY, t REAL, sym TEXT, src TEXT, acct TEXT, price REAL,
            stop REAL, tp REAL, atr REAL, sigs TEXT, feats TEXT, acted INTEGER, why TEXT, done INTEGER DEFAULT 0,
            r REAL, ret REAL, maxup REAL);
        CREATE INDEX IF NOT EXISTS ornek_due ON ornek(done, t);
        CREATE INDEX IF NOT EXISTS ornek_sym ON ornek(sym, t);
        CREATE TABLE IF NOT EXISTS ders (id INTEGER PRIMARY KEY, t REAL, kind TEXT, sym TEXT, acct TEXT, title TEXT,
            text TEXT, sigs TEXT, r REAL);
        CREATE TABLE IF NOT EXISTS kural (key TEXT PRIMARY KEY, kind TEXT, first_t REAL, last_t REAL, active INTEGER);
        """)
        if "bars" not in {r[1] for r in con.execute("PRAGMA table_info(ornek)")}:  # kontrollu deney icin fiyat yolu
            con.execute("ALTER TABLE ornek ADD COLUMN bars TEXT")


def add_lesson(kind, sym, acct, title, text, sigs=(), r=None):
    with _LOCK, _db() as con:
        con.execute("INSERT INTO ders (t,kind,sym,acct,title,text,sigs,r) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), kind, sym, acct, title, text, ",".join(sigs), r))


def log_samples(rows, ctx, acted=(), why=None):
    """Karar anindaki adaylari ogrenme ornegi olarak kaydeder (ayni coin en fazla 2 saatte bir)."""
    why, now, n = why or {}, time.time(), 0
    with _LOCK, _db() as con:
        recent = {r[0] for r in con.execute("SELECT DISTINCT sym FROM ornek WHERE src='radar' AND t>?",
                                            (now - SAMPLE_GAP_H * 3600,))}
        for r in rows:
            s, px = r.get("sym"), _n(r, "price")
            if not s or not px or s in recent:
                continue
            a = min(max(_n(r, "atr_pct") or 0.012, 0.004), 0.06) * px
            con.execute("INSERT INTO ornek (t,sym,src,price,stop,tp,atr,sigs,feats,acted,why) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                        (now, s, "radar", px, px - 1.5 * a, px + 3.2 * a, a, ",".join(sorted(signals(r, ctx))),
                         json.dumps(feats(r, ctx)), int(s in acted), str(why.get(s, ""))[:200]))
            n += 1
    return n


# ---------------- otopsi ----------------
def autopsy(acct, sym, pos, exit_px, reason, pnl, pct, now_row=None, btc_move=None):
    """Kapanan gercek/sanal islemin otopsisi: ornek olarak kaydedilir (2 kat agirlik) ve ders yazilir."""
    e, sl0 = pos["entry"], pos.get("sl0") or pos["sl"]
    risk = pos.get("qty0", pos["qty"]) * (e - sl0)
    r = pnl / risk if risk > 0 else 0.0
    sg, f0 = sorted(pos.get("sigs") or []), pos.get("feats") or {}
    hours = (time.time() - pos["t"]) / 3600
    with _LOCK, _db() as con:
        con.execute("INSERT INTO ornek (t,sym,src,acct,price,stop,tp,atr,sigs,feats,acted,why,done,r,ret,maxup) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,1,?,1,?,?,?)",
                    (pos["t"], sym, "islem", acct, e, sl0, pos.get("tp"), pos.get("atr"), ",".join(sg), json.dumps(f0),
                     reason, r, pct / 100, pos.get("hi", e) / e - 1))
    STATE["dirty"] = True
    num = lambda v, f="+.2f": "-" if v is None else format(v, f)
    ent = (f"Girişte teknik 15dk/1s/4s/1g {num(f0.get('t_15m'))}/{num(f0.get('t_1h'))}/{num(f0.get('t_4h'))}/"
           f"{num(f0.get('t_1d'))}" + (f", RSI {f0['rsi']:.0f}" if f0.get("rsi") is not None else "")
           + (f", defter alıcı %{f0['ob'] * 100:.0f}" if f0.get("ob") is not None else "")
           + f", kripto {REG_TR.get(f0.get('regime'), '-')}" + (f", makro {f0['macro']}" if f0.get("macro") else "") + ".")
    te = (now_row or {}).get("tech") or {}
    ext = f"Çıkışta 1s teknik {num(te.get('1h'))}" + (f"; BTC işlem süresince %{btc_move:+.1f}" if btc_move is not None else "") + "."
    head = (f"{sym} {'kârla' if pnl > 0 else 'zararla'} kapandı: %{pct:+.2f} ({REASON_TR.get(reason, reason)}, "
            f"{hours:.1f} sa, {r:+.2f}R).")
    W = STATE["weights"]
    if pnl > 0 and reason != "BASABAS":
        kind = "kar"
        lesson = f"Ders: çalışan kurulum ({labels(sg) or 'teknik uyum'}); bu sinyallerin ağırlığı artırılıyor."
        if reason == "IZ_STOP":
            lesson += " Kâr kilidi (iz süren stop) kazancı korudu."
    else:
        kind = "basabas" if reason == "BASABAS" else "zarar"
        causes = []
        if btc_move is not None and btc_move <= -1.5:
            causes.append(f"BTC işlem boyunca %{btc_move:.1f} düştü ve coini aşağı çekti")
        if "rsi_asiri" in sg:
            causes.append("aşırı alım bölgesinde girildi")
        if "pompa" in sg:
            causes.append("24 saatte sert yükselmiş bir coin kovalandı")
        if "zayif_4s" in sg or "gunluk_asagi" in sg:
            causes.append("üst zaman dilimi trendi desteklemiyordu")
        if "defter_satici" in sg:
            causes.append("emir defterinde satıcı baskındı")
        if "makro_riskoff" in sg:
            causes.append("küresel piyasalar risk-off'tu")
        if reason == "STOP" and hours < 1.5:
            causes.append("girişten hemen sonra ters döndü (giriş zamanlaması erken veya stop dar)")
        if reason in ("SURE_DOLDU", "ZAMAN_STOPU"):
            causes.append("beklenen hareket süresi içinde gelmedi")
        if te.get("1h") is not None and te["1h"] <= -0.3:
            causes.append("çıkışta 1s teknik özet satışa dönmüştü (trend kırıldı)")
        bad = sorted((s for s in sg if W.get(s, {}).get("n", 0) >= 5 and W[s]["w"] < -0.03), key=lambda s: W[s]["w"])[:2]
        lesson = "Sebep: " + ("; ".join(causes) if causes else "belirgin tek sebep yok (piyasa gürültüsü)") + "."
        if bad:
            lesson += " Geçmişte zayıf çıkan sinyaller: " + ", ".join(
                f"{label(s)} ({W[s]['avg']:+.2f}R, {W[s]['n']} örnek)" for s in bad) + "."
        lesson += (" Kurulum hedefe gitmeden döndü; başabaş kalkanı zararı önledi." if kind == "basabas" else
                   " Ders: benzer kurulumda risk yarıya iner; aynı kombinasyon tekrar zarar ettirirse alım engellenir.")
    text = f"{head} {ent} {ext} {lesson}"
    add_lesson(kind, sym, acct, head, text, sg, r)
    return text


def _missed_lesson(o, r, mx):
    sg = [s for s in (o["sigs"] or "").split(",") if s]
    with _LOCK, _db() as con:
        if con.execute("SELECT 1 FROM ders WHERE kind='kacan' AND sym=? AND t>?", (o["sym"], time.time() - 86400)).fetchone():
            return None
    text = (f"{o['sym']} {time.strftime('%d.%m %H:%M', time.localtime(o['t']))} itibarıyla adaydı ama alınmadı; sonraki "
            f"24 saatte en fazla %{mx * 100:+.1f} yükseldi (botun planıyla sonuç {r:+.1f}R). O anki sinyaller: "
            f"{labels(sg) or '-'}. Alınmama nedeni: {o['why'] or 'aday listesinde öne çıkmadı'}. Ders: bu sinyal "
            f"kombinasyonunun ağırlığı artırıldı; aynı kurulum tekrar gelirse öncelik verilecek.")
    add_lesson("kacan", o["sym"], None, f"Kaçan fırsat: {o['sym']} %{mx * 100:+.0f}", text, sg, r)
    return text


_EX = {}


def _ex():
    if "ex" not in _EX:
        _EX["ex"] = ccxt.binance({"enableRateLimit": True})
    return _EX["ex"]


def evaluate(limit=200):
    """Suresi dolan radar orneklerini 15 dk'lik Binance mumlariyla sonuclandirir. Donus: yeni ders metinleri."""
    now = time.time()
    with _LOCK, _db() as con:
        due = [dict(r) for r in con.execute("SELECT * FROM ornek WHERE done=0 AND t<? ORDER BY t LIMIT ?",
                                            (now - HORIZON_H * 3600, limit))]
    res = []
    for o in due:
        try:
            kl = _ex().public_get_klines({"symbol": o["sym"].replace("/", ""), "interval": "15m",
                                          "startTime": int(o["t"] * 1000), "limit": HORIZON_H * 4})
            bars = [(float(k[2]), float(k[3]), float(k[4])) for k in kl]
        except (ccxt.BaseError, ValueError, KeyError, IndexError) as e:
            log.debug("ders defteri ornek %s: %s", o["sym"], e)
            bars = None
        if not bars or len(bars) < HORIZON_H * 3:
            if now - o["t"] > 4 * 86400:  # islemden kalkmis vb.: vazgec
                res.append((o, None))
            continue
        o["bars"] = json.dumps([[round(x / o["price"], 5) for x in b] for b in bars])  # girise gore goreli fiyat yolu
        res.append((o, simulate(o["price"], o["stop"], o["tp"], o["atr"], bars)))
    with _LOCK, _db() as con:
        for o, sim in res:
            if sim is None:
                con.execute("UPDATE ornek SET done=2 WHERE id=?", (o["id"],))
            else:
                con.execute("UPDATE ornek SET done=1, r=?, ret=?, maxup=?, bars=? WHERE id=?", (*sim, o["bars"], o["id"]))
        con.execute("DELETE FROM ornek WHERE src='radar' AND t<?", (now - 120 * 86400,))
    if res:
        STATE["dirty"] = True
    out = []
    missed = [(o, s) for o, s in res if s and not o["acted"] and s[0] >= 1.0 and s[2] >= max(0.08, 3 * o["atr"] / o["price"])]
    for o, (r, _, mx) in sorted(missed, key=lambda x: -x[1][2])[:5]:
        t = _missed_lesson(o, r, mx)
        if t:
            out.append(t)
    return out


# ---------------- ogrenme ----------------
def recompute():
    """Agirliklar ve kurallar son 60 gunun sonuclanmis orneklerinden yeniden hesaplanir. Donus: yeni kural dersleri."""
    since = time.time() - WINDOW_D * 86400
    with _LOCK, _db() as con:
        rows = con.execute("SELECT src, sigs, r, feats FROM ornek WHERE done=1 AND r IS NOT NULL AND t>?", (since,)).fetchall()
        old = {r["key"]: r["active"] for r in con.execute("SELECT key, active FROM kural")}
    data = [(frozenset(x for x in (r["sigs"] or "").split(",") if x), max(min(r["r"], 3.0), -1.5),
             REAL_W if r["src"] == "islem" else 1.0, r["src"]) for r in rows]
    tw = sum(w for _, _, w, _ in data)
    base = sum(r * w for _, r, w, _ in data) / tw if tw else 0.0
    # adaptif katman icin: rejim x volatilite basarisi ve rejime ozel sinyal agirliklari
    regstats, racc = {}, {}
    for (sg, r, w, _), row in zip(data, rows):
        try:
            f = json.loads(row["feats"] or "{}")
        except ValueError:
            f = {}
        reg = f.get("regime") or "-"
        x = regstats.setdefault(f"{reg}|{f.get('vol') or '-'}", [0.0, 0.0, 0])
        x[0], x[1], x[2] = x[0] + w, x[1] + r * w, x[2] + 1
        for s in sg:
            a = racc.setdefault(reg, {}).setdefault(s, [0.0, 0.0, 0])
            a[0], a[1], a[2] = a[0] + w, a[1] + r * w, a[2] + 1
    STATE["regstats"] = {k: {"n": n, "avg": round(sr / w, 3)} for k, (w, sr, n) in regstats.items()}
    # agirlik MUTLAK beklentidir (tabana gore degil): zarar ettiren sinyal, taban daha kotu diye pozitif agirlik almaz
    STATE["wreg"] = {reg: {s: round(sr / w * n / (n + SHRINK), 3) for s, (w, sr, n) in d.items()}
                     for reg, d in racc.items()}
    acc = {}
    for sg, r, w, _ in data:
        for key in [(s,) for s in sg] + list(itertools.combinations(sorted(sg), 2)):
            a = acc.setdefault(key, [0.0, 0.0, 0, 0.0, 0.0])
            a[0], a[1], a[2], a[3], a[4] = a[0] + w, a[1] + r * w, a[2] + 1, a[3] + w * (r > 0), a[4] + r * r * w
    weights, traps, golden = {}, [], []
    for key, (w, sr, n, wins, sq) in acc.items():
        avg, win = sr / w, wins / w
        se = max(sq / w - avg * avg, 0.0) ** 0.5 / max(n, 1) ** 0.5
        rule = {"sigs": list(key), "n": n, "avg": round(avg, 3), "win": round(win, 3)}
        if len(key) == 1:
            weights[key[0]] = {"n": n, "avg": round(avg, 3), "win": round(win, 3),
                               "w": round(avg * n / (n + SHRINK), 3)}
        single = len(key) == 1
        if not (single and key[0] in CONTEXT):
            if n >= TRAP["n1" if single else "n2"] and avg <= TRAP["avg"] and avg + Z * se < 0:
                traps.append(dict(rule, kind="tuzak"))
            if n >= GOLD["n1" if single else "n2"] and avg >= GOLD["avg"] and avg - Z * se > 0:
                golden.append(dict(rule, kind="altin"))
    traps = sorted(traps, key=lambda x: x["avg"])[:12]
    golden = sorted(golden, key=lambda x: -x["avg"])[:12]
    real = [(sg, r) for sg, r, _, src in data if src == "islem"]
    STATE.update(weights=weights, traps=traps, golden=golden, real=real[-300:], n=len(data), base=round(base, 3),
                 t=time.time(), dirty=False)
    new, now = [], time.time()
    with _LOCK, _db() as con:
        cur = set()
        for rl in traps + golden:
            key = rl["kind"] + ":" + "+".join(rl["sigs"])
            cur.add(key)
            con.execute("INSERT INTO kural (key,kind,first_t,last_t,active) VALUES (?,?,?,?,1) "
                        "ON CONFLICT(key) DO UPDATE SET last_t=excluded.last_t, active=1", (key, rl["kind"], now, now))
            if not old.get(key):
                if rl["kind"] == "tuzak":
                    t = (f"TUZAK kuralı öğrenildi: {labels(rl['sigs'])} birlikteyken {rl['n']} örnekte kazanma "
                         f"%{rl['win'] * 100:.0f}, ortalama {rl['avg']:+.2f}R. Bu kombinasyonda yeni alım artık risk "
                         f"yöneticisi tarafından engelleniyor.")
                else:
                    t = (f"ALTIN kural öğrenildi: {labels(rl['sigs'])} birlikteyken {rl['n']} örnekte kazanma "
                         f"%{rl['win'] * 100:.0f}, ortalama {rl['avg']:+.2f}R. Bu kurulum öne alınıyor.")
                con.execute("INSERT INTO ders (t,kind,sym,acct,title,text,sigs,r) VALUES (?,?,?,?,?,?,?,?)",
                            (now, "kural", None, None, ("Tuzak: " if rl["kind"] == "tuzak" else "Altın kural: ")
                             + labels(rl["sigs"]), t, ",".join(rl["sigs"]), rl["avg"]))
                new.append(t)
        for key, act in old.items():
            if act and key not in cur:
                con.execute("UPDATE kural SET active=0 WHERE key=?", (key,))
    return new


def edge(sigs, regime=None):
    """Ogrenilen avantaj (R): aktif sinyallerin agirliklari (o rejimde yeterli ornek varsa rejime ozel agirlik agirlikli)
    + altin kural bonusu."""
    W, R = STATE["weights"], (STATE.get("wreg") or {}).get(regime) or {}
    e = 0.0
    for s in sigs:  # rejime ozel agirlik zaten az ornekte sifira cekilmis (shrink): genel agirlikla ortalanir
        g = W.get(s, {}).get("w", 0.0)
        e += (g + R[s]) / 2 if s in R else g
    e += 0.15 * sum(1 for g in STATE["golden"] if set(g["sigs"]) <= set(sigs))
    import gecmis  # 2 yillik onsel: binlerce ornekte sinyalin mutlak beklentisi (yari agirlikla)
    for s in sigs:
        st = gecmis.sig_stat((s,))
        if st and s not in CONTEXT:
            e += 0.5 * st["avg"]
    e += 0.15 * sum(1 for g in gecmis.golden() if set(g["sigs"]) <= set(sigs))
    return max(min(e, 1.0), -1.0)


def check(sigs, regime=None):
    """Risk yoneticisi icin: (red sebebi veya None, risk carpani, ogrenilen avantaj R)."""
    sigs, e = set(sigs), edge(sigs, regime)
    for t in STATE["traps"]:
        if set(t["sigs"]) <= sigs:
            return (f"Ders Defteri tuzağı: {labels(t['sigs'])} ({t['n']} örnekte kazanma %{t['win'] * 100:.0f}, "
                    f"ort {t['avg']:+.2f}R)"), 0.0, e
    import gecmis  # 2 yillik Gecmis Okulu: egitim ve testte tutarli zarar ettiren kombinasyonlar
    for t in gecmis.traps():
        if set(t["sigs"]) <= sigs:
            return (f"Geçmiş Okulu tuzağı: {labels(t['sigs'])} (2 yılda {t['n']} örnek, test dönemi ort "
                    f"{t['test']['avg']:+.2f}R, kazanma %{t['win'] * 100:.0f})"), 0.0, e
    sim = [r for sg, r in STATE["real"] if sg and len(sg & sigs) / len(sg | sigs) >= 0.75][-3:]
    if len(sim) >= 2 and all(r < 0 for r in sim):
        return f"Ders Defteri: benzer şartlardaki son {len(sim)} işlem zararla kapandı", 0.0, e
    mult = 0.5 if sim and sum(r < 0 for r in sim) > len(sim) / 2 else 1.0
    if STATE["n"] >= 40:  # kanitlanmis mutlak beklenti olmadan risk 1 katin ustune cikmaz
        mult *= max(min(1 + e, 1.0), 0.5)
    return None, round(mult, 2), round(e, 3)


def brief_lines(admin=True):
    """Claude'a giden Ders Defteri ozeti (normal kullanicinin sohbetinde sanal hesap otopsileri yok)."""
    W, L = STATE["weights"], []
    if W:
        top = sorted(W.items(), key=lambda kv: -abs(kv[1]["w"]))[:10]
        L.append(f"OGRENILEN SINYAL AGIRLIKLARI (son {WINDOW_D} gun, {STATE['n']} sonuclanmis ornek; R = risk birimi, "
                 "+ kazandiriyor, - kaybettiriyor): " + "; ".join(
                     f"{k} {v['w']:+.2f} (n {v['n']}, kazanma %{v['win'] * 100:.0f})" for k, v in top))
    if STATE["traps"]:
        L.append("TUZAK KOMBINASYONLAR (risk yoneticisi bu sinyaller birlikteyken alimi reddeder): " + "; ".join(
            f"{'+'.join(t['sigs'])} (n {t['n']}, ort {t['avg']:+.2f}R)" for t in STATE["traps"]))
    if STATE["golden"]:
        L.append("ALTIN KURALLAR (gercekte kazandiran): " + "; ".join(
            f"{'+'.join(g['sigs'])} (n {g['n']}, ort {g['avg']:+.2f}R)" for g in STATE["golden"]))
    with _LOCK, _db() as con:
        ls = con.execute("SELECT text FROM ders WHERE ? OR acct IS NULL OR acct != 'PAPER' ORDER BY t DESC LIMIT 6",
                         (int(admin),)).fetchall()
    if ls:
        L.append("SON DERSLER (otopsi):\n" + "\n".join(f"- {r['text'][:260]}" for r in ls))
    return L


def summary(admin=True):
    with _LOCK, _db() as con:
        ls = [dict(r) for r in con.execute("SELECT * FROM ders ORDER BY t DESC LIMIT 300")]
        cnt = {(r[0], r[1]): r[2] for r in con.execute("SELECT src, done, COUNT(*) FROM ornek GROUP BY src, done")}
    if not admin:  # normal kullanici sanal hesap otopsilerini gormez
        ls = [dict(x, acct=None) for x in ls if x.get("acct") != "PAPER"]
    for x in ls:
        x["labels"] = [label(s) for s in (x.get("sigs") or "").split(",") if s]
    weights = sorted(({"sig": k, "label": label(k), **v} for k, v in STATE["weights"].items()), key=lambda x: -x["w"])
    lab = lambda rs: [dict(x, label=labels(x["sigs"])) for x in rs]
    return {"lessons": ls, "weights": weights, "traps": lab(STATE["traps"]), "golden": lab(STATE["golden"]),
            "counts": {"radar_done": cnt.get(("radar", 1), 0), "radar_wait": cnt.get(("radar", 0), 0),
                       "real": cnt.get(("islem", 1), 0)}, "n": STATE["n"], "base": STATE["base"], "t": STATE["t"],
            "signals": {k: v[0] for k, v in SIGNALS.items()}}


def loop(stop, say=lambda msg: None):
    """Arka plan: 20 dk'da bir ornekleri sonuclandir, degisiklik olunca agirliklari yeniden hesapla."""
    last_eval = 0.0
    while not stop.is_set():
        try:
            new = []
            if time.time() - last_eval > 1200:
                last_eval = time.time()
                new += evaluate()
            if STATE["dirty"]:
                new += recompute()
            for t in new:
                say(t)
        except Exception as e:  # ag/disk hatasi: bir sonraki turda tekrar
            log.warning("ders defteri: %s", e)
        stop.wait(30)
