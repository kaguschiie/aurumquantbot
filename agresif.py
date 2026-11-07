"""Agresif deney botu: SADECE sanal kasa. Ayri giris noktasi: python agresif.py --sunucu
Melik ve fatih bu dosyayi hic yuklemez; app/config/bot icinde agresif kosulu yoktur, kapi burada kosulsuzdur.
Kurallar gecmis testte zarar etti (bkz. UYARI): amac kar degil olcum. Gercek emir yolu yok."""
import collections
import copy
import logging
import logging.handlers
import os
import sys
import threading
import time
from urllib.parse import parse_qs, urlparse

CIKIS = 77  # sunucu birimi: RestartPreventExitStatus (yanlis kurulumda yeniden baslamasin)


def _dur(msg):
    sys.stderr.write(f"agresif: {msg}\n")
    sys.exit(CIKIS)


# ---------------- kapi: config yuklenmeden once ----------------
if hasattr(os, "geteuid") and os.geteuid() == 0:
    _dur("root ile calismaz.")
_veri = os.environ.get("KQB_APP_DIR") or ""
if not _veri:
    _dur("KQB_APP_DIR zorunlu.")
if os.path.basename(os.path.normpath(os.path.realpath(_veri))).lower() in ("veri", "veri-fatih"):
    _dur("melik/fatih veri klasorunde calismaz.")
if os.path.exists(os.path.join(_veri, "settings.json")):  # config import aninda okur; anahtar icerebilir
    _dur("veri klasorunde settings.json olmamali.")
if (os.environ.get("KQB_PORT") or "8765") in ("8765", "8766"):
    _dur("KQB_PORT ayri olmali (8765/8766 melik ve fatih).")

import config as C

C.__dict__.update(
    API_KEY="", API_SECRET="", TELEGRAM_TOKEN="", TELEGRAM_CHAT_ID="", CLAUDE_API_KEY="", CLAUDE_CLI_PATH="", WHALE_ALERT_KEY="",
    CRYPTOPANIC_KEY="", UPDATE_REPO="", UPDATE_TOKEN="", LIVE_ALLOWED=False, PORTFOY=False, PORTFOY_LIVE=False, EARN_USDT=False,
    HABER=False, HABER_ISLEM=False, HABER_LIVE=False, OTO_SAATLIK=False, SERVER_MODE=False, AUTO_UPDATE_CHECK=False,
    GUNLUK_TREND=False, QUOTA_SAVER=False, DAILY_LOSS_PCT=100.0, DD_PAUSE_PCT=100.0, KILL_DD_PCT=100.0, MAX_POSITIONS=8,
    COOLDOWN_MIN=240, PAPER_BALANCE=1000.0,
    load_settings=lambda: None, save_settings=lambda new: None)

import numpy as np
import pandas as pd

import agent as A
import app
import auth
import bot as B
import guncelle
import portfoy as PF
import teknik as T

A.brain = lambda: "local"
A.cli_ready = lambda: False
A.status = lambda: (True, "Yerel kurallar (Claude yok, sadece sanal)")
guncelle.GIT = False
PF.maybe_train = lambda say: None
PF.load_model = lambda: None
app.mode_for = lambda sess, q: "PAPER"
app.migrate_old_files = lambda: None  # kod klasorundeki dosyalar veri klasorune tasinmasin

log = logging.getLogger("bot")
KURAL = {"agr_radar": {"ad": "Radar", "slot": 4, "test": -99.8, "saat": 12,
                       "kural": "Taze GÜÇLÜ AL (1s); 1 saat sonra kapanış girişin %0,5 üstündeyse sat, stop -%3, en çok 12 sa"},
         "agr_senaryo": {"ad": "Senaryo", "slot": 4, "test": -82.1, "saat": 48, "kural": "Tüm likit coinlerde 24 sa zirve kırılımı + hacim, geri test; BTC 100 gün üstü; hedef 2R, en çok 48 sa"}}
UYARI = ("Ölçüm deneyi, kâr hedefi değil. 47 kuralın hepsi geçmiş testte zarar etti (hızlı kâr alanlar dahil, komisyonsuz da): en iyisi -%82, "
         "aynı dönemde BTC +%13,3. Kârda hemen satmak kazanma oranını yükseltir ama küçük kazanç / büyük kayıp kasayı eritir. "
         "Karar 100+ işlemden sonra. Sadece sanal kasa, gerçek para yok.")
KAR_ESIK, RADAR_STOP = 0.005, 0.03
GET_IZIN = {"/", "/lw.js", "/api/app", "/api/health", "/api/auth/me", "/api/state", "/api/screener", "/api/opps", "/api/trades",
            "/api/notes", "/api/decisions", "/api/candles", "/api/agresif"}
POST_IZIN = {"/api/auth/login", "/api/auth/logout", "/api/pause", "/api/close", "/api/paper/reset"}
TF_IZIN = {"15m", "1h", "4h", "1d"}
GOVDE_MAX = 16384


class Sinir:
    """Kayan pencere: istemci (kim) basina pencerede en cok n; en cok maks istemci, en eski kullanilan atilir."""

    def __init__(self, n, sn, maks=5000):
        self.n, self.sn, self.maks, self.q, self.k = n, sn, maks, collections.OrderedDict(), threading.Lock()

    def izin(self, kim=""):
        now = time.time()
        with self.k:
            q = self.q.pop(kim, None) or collections.deque()
            while q and now - q[0] >= self.sn:
                q.popleft()
            ok = len(q) < self.n
            if ok:
                q.append(now)
            self.q[kim] = q
            while len(self.q) > self.maks:
                self.q.popitem(last=False)
            return ok


GENEL, GIRIS, MUM = Sinir(20, 1), Sinir(10, 60), Sinir(30, 60)


# ---------------- kurallar: sadece kapanmis mumlardan, durumsuz ----------------
def radar_taze(kapali):
    """Kapanmis son mumda rec > 0.5, bir oncekinde <= 0.5 ise son mumun teknik ozeti, degilse None."""
    if len(kapali) < 61:
        return None
    simdi = T.rating(kapali)
    return simdi if simdi["rec"] > .5 and T.rating(kapali.iloc[:-1])["rec"] <= .5 else None


def senaryo_tara(kapali):
    """agresif_test.py retest(24) dongusu: kirilim + hacim, 12 mum icinde geri test. Donus (sinyal, izleme); sinyal sadece geri test son mumdaysa."""
    n = len(kapali)
    if n < 80:
        return None, None
    c, h, lo, v = (kapali[k].values.astype(float) for k in ("close", "high", "low", "volume"))
    S = pd.Series
    hh = S(h).shift(1).rolling(24).max().values
    vs = np.nan_to_num((S(v).rolling(3).mean() / S(v).shift(3).rolling(48).median()).values)
    liq = S(v * c).rolling(24).sum().values >= C.RADAR_MIN_VOL
    atr = np.clip(np.nan_to_num(T.rating_series(kapali)["atr_pct"].values, nan=0.02), 0.004, 0.06)  # oran (0.02 = %2)
    with np.errstate(invalid="ignore"):
        kirilim = np.flatnonzero((c > hh) & (vs >= 2) & liq)
    nxt, sinyal, izleme = -1, None, None
    for i in kirilim:
        if i < nxt:
            continue
        lv, at = float(hh[i]), float(atr[i])
        for j in range(i + 1, min(i + 13, n)):
            if c[j] < lv * (1 - 0.5 * at):  # seviye kirildi: senaryo bozuldu
                nxt = j
                break
            if lo[j] <= lv * (1 + 0.25 * at) and c[j] > lv and liq[j]:
                nxt = j + 12
                if j == n - 1:
                    sinyal = {"L": lv, "at": at, "low": float(lo[j]), "gecersiz": lv * (1 - 0.5 * at), "vspike": round(float(vs[i]), 2),
                              "once": int(n - 1 - i), "stop": min(float(lo[j]), lv * (1 - 0.5 * at)) * 0.999}
                break
        else:
            nxt = i + 12
            if i + 12 >= n:  # pencere bitmedi: geri test bekleniyor
                izleme = {"L": lv, "gecersiz": lv * (1 - 0.5 * at), "once": int(n - 1 - i), "kalan": int(i + 12 - (n - 1)), "vspike": round(float(vs[i]), 2)}
    return sinyal, izleme


def btc_rejim(gunluk, saat):
    """Testle ayni: sinyal mumunun gunu icin DUNKU kapanis > 100 gunluk ortalama. gunluk: son satir olusan gun; saat: su anki saat sayaci (epoch/3600)."""
    d = gunluk.close.iloc[:-1]
    if saat % 24 == 0:  # son kapanan 1s mum dunun 23:00 mumu: onun 'dunu' bir gun oncesi
        d = d.iloc[:-1]
    return bool(d.iloc[-1] > d.rolling(100).mean().iloc[-1])


def _f(x):
    return f"{x:.6g}"


class AgresifBot(B.Bot):
    def __init__(self):
        super().__init__()
        if "LIVE" in self.accounts:  # motor kendi is parcaciginda: sys.exit sureci durdurmazdi
            os._exit(CIKIS)
        self.ag.setdefault("agr", {"senaryolar": [], "izleme": [], "son": 0, "saat": 0, "hata": ""})

    def ensure_brain(self):
        pass

    def scan(self, startup=False):
        pass

    def hourly(self):
        pass

    def pf_watch(self, now=None):
        pass

    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        super().say(msg.replace(" | Manuel Kilit (stop/hedef kullanıcıda)", ""), notify, kind, acct, tg)

    def pf_tick(self):
        """Ana dongu saniyede cagirir, ag yapmaz: yeni saatin radari gelince kurallar bir kez."""
        saat = int(time.time() // 3600)
        if self.ag["agr"].get("saat") == saat or self.tech.t_opps <= saat * 3600 + 30:
            return False
        if time.time() - saat * 3600 > 900:  # acilis/kesinti saat ortasina denk geldi: test girisi mum acilisinda, bu saat atlanir
            self.ag["agr"]["saat"] = saat
            return False
        if self._background("Agresif kurallar", lambda: self.agr_saat(saat)):
            self.ag["agr"]["saat"] = saat
            return True
        return False

    def _hisse(self, s):
        """Adi B ile biten ve Bybit vadelisi olmayan parite tokenize hisse sayilir (BNB, ARB, SHIB gibi gercek coinlerin vadelisi var).
        Bybit listesi gunde bir okunur; okunamazsa sadece bilinen hisse listesi (PF.STOCK) uygulanir."""
        base = s.split("/")[0]
        if base.startswith(PF.STOCK):
            return True
        if time.time() - getattr(self, "_perps_t", 0) > 86400:
            self._perps_t = time.time()
            try:
                self._perps = __import__("data").bybit_perps()
            except Exception as e:
                log.warning("bybit vadeli listesi: %s", e)
        return base.endswith("B") and bool(getattr(self, "_perps", None)) and base + "USDT" not in self._perps

    def _temizle(self):
        """scan kapali: islemi biten coinler izlemeden dussun (akis listesi buyumesin)."""
        with self.lock:
            busy = {s for a in self.accounts.values() for s in a.st["positions"].keys() | a.st["pending"].keys()}
            self.extra = {s for s in self.extra if s in busy} | set(C.EXTRA_SYMBOLS)
            self.market = {s: r for s, r in self.market.items() if s in self.symbols or s in busy or s in self.extra}

    def agr_saat(self, saat=None):
        saat = saat or int(time.time() // 3600)
        self.status = "Agresif kurallar taranıyor"
        a, hata, mum = self.accounts["PAPER"], [], {}

        def kapali(s):
            if s not in mum:
                mum[s] = self.tech._klines(s, "1h").iloc[:-1]
            return mum[s]
        self._temizle()
        with self.lock:
            radar_poz = [s for s, p in a.st["positions"].items() if p.get("kind") == "agr_radar"]
        for s in radar_poz:  # hizli kar: en az bir kapanmis mum tutuldu ve son kapanis girisin %0,5 ustunde (test: k_1s_kar0.5_sl3_12s)
            try:
                d = kapali(s)
                with self.lock:
                    p = a.st["positions"].get(s)
                    if p and len(d) and time.time() - p["t"] >= 3000 and float(d["close"].iloc[-1]) >= p["entry"] * (1 + KAR_ESIK):
                        a.close(s, "AGRESIF_KAR")
            except Exception as e:
                hata.append(f"{s}: {str(e)[:80]}")
        rows = sorted((r for r in self.tech.opps if (r.get("qvol") or 0) >= C.RADAR_MIN_VOL), key=lambda r: -(r.get("score") or 0))
        radar_sym = {r["sym"] for r in rows}
        # senaryo testte (h_24_2R_btc) radar sarti olmadan tum likit coinlerde olculdu: radar disi likitler hacim sirasiyla sona
        rows += sorted((r for r in self.tech.liquid if r["sym"] not in radar_sym and (r.get("qvol") or 0) >= C.RADAR_MIN_VOL), key=lambda r: -r["qvol"])
        rows = [r for r in rows if not self._hisse(r["sym"])]  # tokenize hisse/ETF (SOXSB gibi) kripto degil: test evreninde de yoktu
        try:
            btc_ust = btc_rejim(self.tech._klines(C.BTC, "1d", limit=103), saat)
        except Exception as e:  # rejim bilinmiyorsa senaryo acilmaz
            btc_ust = False
            hata.append(f"BTC rejim: {str(e)[:80]}")
        radar, senaryo, izleme = [], [], []
        for r in rows:
            s = r["sym"]
            try:
                d = kapali(s)
                rt = radar_taze(d) if s in radar_sym else None  # radar kurali testte radar coinlerinde
                if rt:
                    radar.append((r, rt))
                sg, iz = senaryo_tara(d)
                if sg and btc_ust:
                    senaryo.append((r, sg))
                if iz:
                    izleme.append({"sym": s, **iz, "t": (saat - 1 - iz["once"]) * 3600})
            except Exception as e:
                hata.append(f"{s}: {str(e)[:80]}")
        for r, rt in radar:
            self._ac(a, "agr_radar", r["sym"], self._radar_plan, rt, hata)
        for r, sg in senaryo:
            self._ac(a, "agr_senaryo", r["sym"], self._senaryo_plan, sg, hata)
        with self.lock:
            self.ag["agr"].update(son=time.time(), hata="; ".join(hata)[:400], izleme=izleme[:30], btc_ust=btc_ust, evren=len(rows), radar_n=len(radar_sym))
        if hata:
            log.warning("agresif kurallar: %s", hata[:5])

    @staticmethod
    def _radar_plan(s, ask, rt):
        at = float(np.clip(rt.get("atr_pct") or 0.02, 0.004, 0.06))
        stop, hedef = ask * (1 - RADAR_STOP), ask * 1.5  # testte sabit hedef yok; +%50 sadece ust sinir
        return stop, hedef, at, (f"SENARYO (Radar): {s} 1 saatlik teknik özet bu mumda GÜÇLÜ AL'a döndü (puan {rt['rec']:+.2f}), önceki mumda değildi. "
                                 f"Giriş {_f(ask)}, stop {_f(stop)} (-%3). Saat başı kapanış girişin %0,5 üstündeyse kâr alınır "
                                 f"(en erken 1 saat sonra), en çok 12 saat tutulur.")

    @staticmethod
    def _senaryo_plan(s, ask, sg):
        stop = sg["stop"]
        hedef = ask + 2 * (ask - stop)
        return stop, hedef, sg["at"], (f"SENARYO (Senaryo): BTC 100 günlük ortalama üstünde. {s} grafiğinde 24 saatlik zirve {_f(sg['L'])} hacimle kırıldı "
                                       f"({sg['once']} mum önce, hacim {sg['vspike']:g}x); fiyat seviyeye döndü (dip {_f(sg['low'])}) ve üstünde kapandı. "
                                       f"Giriş {_f(ask)}, stop {_f(stop)}, hedef {_f(hedef)} (2R), en çok 48 saat. Geçersiz: stop altında fiyat.")

    def _ac(self, a, kind, s, plan_fn, sinyal, hata):
        def kayit(durum, metin=""):
            with self.lock:
                ag = self.ag["agr"]
                ag["senaryolar"] = (ag["senaryolar"] + [{"t": time.time(), "sym": s, "kind": kind, "metin": metin, "durum": durum}])[-30:]
        with self.lock:
            st = a.st
            acik = s in st["positions"] or s in st["pending"]
            dolu = sum(p.get("kind") == kind for p in [*st["positions"].values(), *st["pending"].values()]) >= KURAL[kind]["slot"]
            neden = "zaten pozisyon var" if acik else "slot dolu" if dolu else a.blocked() or a.locked(s)
        if neden:
            return kayit(f"atlandı: {neden}")
        try:
            m = self.ex.markets.get(s)
            if not m or not m.get("active") or m.get("quote") != "USDT":
                return kayit("atlandı: aktif parite değil")
            t = self.ex.fetch_ticker(s)
            if not t.get("ask") or not t.get("bid"):
                return kayit("atlandı: fiyat yok")
            with self.lock:  # manual_buy ile ayni hazirlik
                self.bid[s], self.ask[s] = t["bid"], t["ask"]
                self.market.setdefault(s, {"sym": s}).update(price=t["last"], chg24=t.get("percentage"), qvol=t.get("quoteVolume"), px_t=time.time())
                self.extra.add(s)
            stop, hedef, at, metin = plan_fn(s, t["ask"], sinyal)
            en_az = (m.get("limits", {}).get("cost", {}).get("min") or 5) * 1.05
            plan = {"stop": stop, "tp1": 0.0, "tp2": hedef, "risk_pct": C.RISK_PCT, "max_hours": KURAL[kind]["saat"], "entry": "taker", "kind": kind, "manual": True,
                    "sure_h": KURAL[kind]["saat"], "atr_pct": at, "conviction": None, "p": None, "reason": metin}
            with self.lock:
                plan["usdt"] = max(a.equity() / 8, en_az)
                err = a.enter(s, plan)
        except Exception as e:
            hata.append(f"{s}: {str(e)[:80]}")
            return kayit(f"hata: {str(e)[:120]}")
        kayit(f"reddedildi: {err}" if err else "açıldı", metin)
        if not err:
            self.say(metin, kind="karar", acct="PAPER")


# ---------------- skor tablosu ----------------
def karne():
    out = {"uyari": UYARI, "hazir": False, "kurallar": {}, "acik": [], "senaryolar": [], "izleme": []}
    b = app.ENGINE.bot
    if not b:
        return out
    with b.lock:
        a = b.accounts["PAPER"]
        st, eq = a.st, a.equity()
        tr, curve = copy.deepcopy(st["trades"]), list(st["curve"])
        poz = copy.deepcopy(st["positions"])
        bid, agr = dict(b.bid), copy.deepcopy(b.ag.get("agr") or {})
        bas, durdu = st["start_equity"], a.blocked()
    btc0 = next((c[2] for c in curve if c[2]), None)
    btc1 = bid.get(C.BTC) or (curve[-1][2] if curve else None)
    for k, v in KURAL.items():
        t = [x for x in tr if x.get("kind") == k]
        kaz = sum(x["pnl"] > 0 for x in t)
        out["kurallar"][k] = {**v, "n": len(t), "kazanan": kaz, "kazanma": kaz / len(t) * 100 if t else None, "net": sum(x["pnl"] for x in t),
                              "ort_pct": sum(x["pct"] for x in t) / len(t) if t else None, "elle": sum(x["reason"] == "ELLE_KAPATILDI" for x in t),
                              "cikis": dict(collections.Counter(x["reason"] for x in t)), "acik": sum(p.get("kind") == k for p in poz.values())}
    for s, p in poz.items():
        px = bid.get(s) or p["entry"]
        net = p.get("realized", 0) + p["qty"] * (px - p["entry"]) - p["fee"] - p["qty"] * px * C.FEE
        out["acik"].append({"sym": s, "kind": p.get("kind"), "kural": (KURAL.get(p.get("kind")) or {}).get("ad", "-"), "giris": p["entry"], "fiyat": px, "stop": p["sl"],
                            "hedef": p["tp"], "net": net, "net_pct": net / (p["qty0"] * p["entry"]) * 100, "kalan": max(p["sure_t"] - time.time(), 0) if p.get("sure_t") else None,
                            "why": p.get("why", "")})
    kom = sum(x.get("qty", 0) * (x.get("entry", 0) + x.get("exit", 0)) * C.FEE for x in tr) + sum(p["fee"] for p in poz.values())  # Binance spot: her yon %0,1
    out.update(komisyon=kom, fee=C.FEE * 100, kayma=C.SLIPPAGE * 100, hazir=True, kasa=eq, baslangic=bas, kasa_pct=(eq / bas - 1) * 100 if bas else None, btc_pct=(btc1 / btc0 - 1) * 100 if btc0 and btc1 else None,
               donem=curve[0][0] if curve else None, n=len(tr), engel=durdu, senaryolar=agr.get("senaryolar", [])[::-1], izleme=agr.get("izleme", []),
               son=agr.get("son"), hata=agr.get("hata"), btc_ust=agr.get("btc_ust"), evren=agr.get("evren"), radar_n=agr.get("radar_n"))
    return out


# ---------------- HTTP kapisi: varsayilan red ----------------
class Gated(app.Handler):
    def _red(self, code, msg):
        self._send(code, {"ok": False, "msg": msg, "error": msg})

    def _kim(self):
        """Istemci anahtari. Bot sadece 127.0.0.1'i dinler; Caddy (trusted_proxies yok) X-Forwarded-For'u gercek uzak IP ile yazar."""
        return (self.headers.get("X-Forwarded-For") or "").split(",")[-1].strip()[:64] or self.client_address[0]

    def _genel(self):
        """Istemci basina saniyede 20; gecerli oturumu olan sahip sayilmaz (yabanci trafik onu disarida birakamasin)."""
        return GENEL.izin(self._kim()) or bool(auth.session(self._token()))

    def _mum_kapisi(self, q):
        """Hata cevabi verildiyse True. Yeni canli mum aboneligi (WebSocket mesaji) dakikada 30."""
        b = app.ENGINE.bot
        if not b:
            self._red(503, "Motor hazır değil.")
            return True
        s, tf = q.get("sym", C.BTC), q.get("tf", "1h")
        m = b.ex.markets.get(s)
        if tf not in TF_IZIN or not m or not m.get("active") or m.get("quote") != "USDT":
            self._red(400, "Geçersiz coin veya zaman dilimi.")
            return True
        if (s, tf) not in b.stream.klines and not MUM.izin():
            self._red(429, "Çok hızlı grafik değişimi, biraz bekleyin.")
            return True
        return False

    def do_GET(self):
        u = urlparse(self.path)
        if u.path != "/api/health" and not self._genel():
            return self._red(429, "Çok fazla istek.")
        if u.path not in GET_IZIN:
            return self._red(403, "Agresif hesapta kapalı.")
        if not self._guard(False):
            return self._send(403, {"error": "forbidden"})
        try:
            if u.path == "/":
                with open(os.path.join(C.BASE_DIR, "ui.html"), "rb") as fh:
                    return self._send(200, fh.read().replace(b"<head>", b'<head>\n<script>window.KISILIK="agresif"</script>', 1), "text/html")
            if u.path in ("/api/agresif", "/api/candles") and not auth.session(self._token()):
                return self._send(401, {"error": "giris gerekli"})
            if u.path == "/api/agresif":
                return self._send(200, karne())
            if u.path == "/api/candles" and self._mum_kapisi({k: v[0] for k, v in parse_qs(u.query).items()}):
                return None
        except Exception as e:
            log.exception("agresif API")
            return self._send(500, {"error": str(e)})
        return super().do_GET()

    def do_POST(self):
        p = self.path  # app ile ayni karsilastirma: ham yol
        if not self._genel():
            return self._red(429, "Çok fazla istek.")
        if p not in POST_IZIN:
            return self._red(403, "Agresif hesapta kapalı.")
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._red(400, "Geçersiz istek.")
        if n < 0 or n > GOVDE_MAX:
            return self._red(413, "İstek çok büyük.")
        if p == "/api/auth/login" and not GIRIS.izin(self._kim()):
            return self._red(429, "Çok fazla giriş denemesi, bir dakika sonra deneyin.")
        super().do_POST()
        if p == "/api/paper/reset" and app.ENGINE.bot and auth.session(self._token()):
            b = app.ENGINE.bot
            with b.lock:
                b.ag["agr"].update(senaryolar=[], izleme=[], hata="")
        return None


def log_kur():
    """app.main icindeki basicConfig kok gunlukte isleyici varsa bir sey yapmaz."""
    h = logging.handlers.RotatingFileHandler(C.LOG_FILE, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.addHandler(h)
    root.setLevel(logging.INFO)


B.Bot = AgresifBot
app.Handler = Gated


def main():
    if "--sunucu" not in sys.argv:
        _dur("kullanim: python agresif.py --sunucu")
    log_kur()
    app.main()


if __name__ == "__main__":
    main()
