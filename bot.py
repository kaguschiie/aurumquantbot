"""KriptoQuantBot motoru.
Canli veri (Binance/Bybit WebSocket + TradingView) -> Claude karar verir -> yerel risk yoneticisi denetler ->
iki hesap uygular: PAPER (sanal kasa) ve LIVE (gercek Binance Spot). Saniyelik pozisyon yonetimi yereldir."""
import copy
import json
import logging
import os
import re
import sys
import threading
import time

import ccxt
import joblib
import pandas as pd
import requests

import agent as A
import config as C
import data as D
import dersler as L
import gecmis as G
import gunluk as GT
import hafiza as HF
import komite as K
import kontrolor as KT
import model as M
import montecarlo as MC
import portfoy as PF
import quant as Q
from makro import Makro
from stream import Stream
from teknik import Teknik
from zincir import Zincir

log = logging.getLogger("bot")
ACCTS = ("PAPER", "LIVE")
RADAR_KEYS = ("rs24", "rs4", "vspike", "r1h", "r4h", "breakout", "score", "full", "flow3")


def norm(sym):
    """'solusdt', 'SOL-USDT', 'SOL/USDT' -> 'SOL/USDT'"""
    s = str(sym or "").upper().replace("-", "/").replace(" ", "")
    return s[:-4] + "/USDT" if "/" not in s and s.endswith("USDT") else s


def today():
    return time.strftime("%Y-%m-%d", time.gmtime())


class Account:
    """Tek portfoy: emir verme, saniyelik pozisyon yonetimi ve hesap korumalari."""

    def __init__(self, bot, mode, saved):
        self.b, self.mode, self.live = bot, mode, mode == "LIVE"
        self.st = {"cash": C.PAPER_BALANCE, "start_equity": None, "positions": {}, "pending": {}, "trades": [],
                   "pnl": 0.0, "wins": 0, "losses": 0, "eq_hist": [], "pause_until": 0, "curve": [],
                   "day": {"d": "", "eq": 0.0}, "cooldown": {}, "stops": [], "kill_reset": 0, "principal": None,
                   "peak": 0.0}
        self.st.update({k: v for k, v in saved.items() if k in self.st})
        self.ex = D.exchange(auth=True) if self.live else bot.ex  # sanal hesap ortak (anahtarsiz) baglantiyi kullanir
        self.usdt_total, self.usdt_free, self.synced, self.error = 0.0, 0.0, not self.live, ""

    @property
    def bid(self):
        return self.b.bid

    @property
    def ask(self):
        return self.b.ask

    def say(self, msg, notify=False, kind="islem"):
        # PAPER islem bildirimleri, gercek hesap varken masaustune dusmez (iki kat bildirim olmasin)
        self.b.say(msg, notify and (self.live or "LIVE" not in self.b.accounts), kind, acct=self.mode)

    # ---------------- bakiye ----------------
    def balance(self, asset):
        b = self.ex.fetch_balance()
        return b.get(asset, {}).get("free") or 0.0, b.get(asset, {}).get("total") or 0.0

    def sync(self):
        """LIVE: borsadaki USDT bakiyesi."""
        try:
            self.usdt_free, self.usdt_total = self.balance("USDT")
            self.synced, self.error = True, ""
        except ccxt.AuthenticationError as e:
            self.error = f"Binance anahtarı reddedildi: {str(e)[:120]}"
        except (ccxt.NetworkError, ccxt.ExchangeError) as e:
            self.error = f"Binance bakiyesi alınamadı: {str(e)[:120]}"

    def equity(self):
        """Net kasa: nakit + pozisyonlarin bugun satilsa eline gececek tutari (Binance satis komisyonu dusulmus;
        alis komisyonu alista dusuldu)."""
        held = sum(p["qty"] * self.bid.get(s, p["entry"]) for s, p in self.st["positions"].items())
        return (self.usdt_total if self.live else self.st["cash"]) + held * (1 - C.FEE)

    def free_cash(self):
        if self.live:
            return self.balance("USDT")[0]
        return self.st["cash"] - sum(p["qty"] * p["price"] * (1 + C.FEE) for p in self.st["pending"].values())

    def mark(self):
        """Saniyelik: baslangic/gun basi kasasi ve 15 dk'da bir kasa egrisi noktasi."""
        if not self.synced:
            return
        st, eq = self.st, self.equity()
        if st["start_equity"] is None:
            st["start_equity"] = eq
        if not st.get("principal"):  # kartopu: anapara (admin Kasa kartindan simdiki kasaya esitleyebilir)
            st["principal"] = st["start_equity"] or eq
        st["peak"] = max(st.get("peak") or 0.0, eq)
        if st["day"]["d"] != today():
            st["day"] = {"d": today(), "eq": eq}
        if not st["curve"] or time.time() - st["curve"][-1][0] > 900:
            st["curve"] = (st["curve"] + [[int(time.time()), round(eq, 2), self.bid.get(C.BTC)]])[-35040:]

    def day_pct(self):
        d = self.st["day"]
        return (self.equity() / d["eq"] - 1) * 100 if d.get("eq") else 0.0

    def risk_mult(self):
        """Kartopu kasa: risk her zaman guncel kasanin yuzdesidir (kar buyudukce pozisyon buyur). Zirveden dususte,
        anaparanin altinda ve ust uste kayiplarda risk kuculur; kar tamponu varken en fazla 1,2 kat. Donus: (carpan, sebep)."""
        st, eq = self.st, self.equity()
        if eq <= 0:
            return 1.0, ""
        pk, pr = max(st.get("peak") or eq, eq), st.get("principal") or eq
        dd, m, why = 1 - eq / pk, 1.0, []
        for lim, k in ((0.07, 0.35), (0.04, 0.55), (0.02, 0.75)):
            if dd >= lim:
                m = k
                why.append(f"zirveden %{dd * 100:.1f} düşüş")
                break
        if eq < pr:
            m *= 0.7
            why.append("kasa anaparanın altında")
        last = [t["pnl"] for t in st["trades"][-3:]]
        if len(last) == 3 and all(p <= 0 for p in last):
            m *= 0.7
            why.append("son 3 işlem kayıp")
        if not why and eq > pr * 1.02:
            m = min(1.2, 1 + (eq / pr - 1) * 2)
            why.append(f"kâr tamponu %{(eq / pr - 1) * 100:.1f}")
        return round(m, 2), ", ".join(why) or "normal"

    # ---------------- korumalar ----------------
    def blocked(self, portfolio=False):
        """Yeni islem acilamiyorsa sebebi, acilabiliyorsa None. portfolio: portfoy stratejisi (pozisyon limiti yok)."""
        st = self.st
        if self.live and not C.LIVE_ALLOWED:
            return "admin gerçek işlem iznini henüz açmadı"
        if self.live and (self.error or not self.synced):
            return self.error or "Binance bakiyesi bekleniyor"
        if os.path.exists(C.pause_flag(self.mode)):
            return "duraklatıldı"
        if time.time() < st["pause_until"]:
            return f"koruma molası ({int((st['pause_until'] - time.time()) / 60)} dk)"
        if self.day_pct() <= -C.DAILY_LOSS_PCT:
            return f"günlük zarar limiti %{C.DAILY_LOSS_PCT:g} doldu (yarın UTC sıfırlanır)"
        if not portfolio and len([p for p in st["positions"].values() if p.get("kind") != PF.KIND]) + \
                len(st["pending"]) >= C.MAX_POSITIONS:
            return "pozisyon limiti dolu"
        return None

    def locked(self, s):
        """Coin bazli kilit: cikistan sonra bekleme + son 48 saatte 3+ islemde net zarar (24 saat kilit)."""
        now = time.time()
        if now - self.st["cooldown"].get(s, 0) < C.COOLDOWN_MIN * 60:
            return f"çıkıştan sonra {C.COOLDOWN_MIN} dk bekleme"
        tr = [t for t in self.st["trades"][-50:] if t["sym"] == s and now - t["t"] < 48 * 3600]
        if len(tr) >= 3 and sum(t["pct"] for t in tr) < -1 and now - tr[-1]["t"] < 24 * 3600:
            return "son 48 saatte bu coinde 3+ işlem zararla kapandı (24 sa kilit)"
        return None

    def report(self):
        """Saatlik: 24 saatlik dusus molasi + 7 gunluk zirveden dusus salteri (elle sifirlanir)."""
        st, eq = self.st, self.equity()
        if not self.synced or eq <= 0:
            return
        st["eq_hist"] = (st["eq_hist"] + [eq])[-24:]
        if eq < max(st["eq_hist"]) * (1 - C.DD_PAUSE_PCT / 100) and time.time() > st["pause_until"]:
            st["pause_until"] = time.time() + 12 * 3600
            st["eq_hist"] = [eq]
            self.say(f"Kasa 24 saatte %{C.DD_PAUSE_PCT:.0f}+ düştü: 12 saat yeni işlem açılmayacak.", True, "uyari")
        week = [c[1] for c in st["curve"] if c[0] > time.time() - 7 * 86400 and c[0] > st.get("kill_reset", 0)]
        peak = max(week + [eq])
        if eq < peak * (1 - C.KILL_DD_PCT / 100) and not os.path.exists(C.pause_flag(self.mode)):
            open(C.pause_flag(self.mode), "w").close()
            st["kill_reset"] = time.time()  # devam edilince eski zirve tekrar tetiklemesin
            self.say(f"ACİL DURDURMA: kasa 7 günlük zirvesinden %{(1 - eq / peak) * 100:.1f} düştü. Yeni işlemler "
                     f"durduruldu; açık pozisyonlar korunuyor. Devam için 'Devam et'e basın.", True, "uyari")

    # ---------------- emirler ----------------
    def enter(self, s, plan):
        """Dogrulanmis plani uygular. Hata metni veya None dondurur."""
        taker = plan["entry"] == "taker"
        price = float(self.ex.price_to_precision(s, self.ask[s] if taker else self.bid[s]))
        if price <= plan["stop"]:
            return "fiyat stop seviyesine indi"
        eq, (mult, _) = self.equity(), self.risk_mult()
        risk = min(plan["risk_pct"] * mult, C.RISK_PCT)  # kartopu: guncel kasanin yuzdesi, ust sinir her zaman RISK_PCT
        qvol = (self.b.market.get(s) or {}).get("qvol") or 0
        qty = min(eq * risk / 100 / (price - plan["stop"]), eq * min(C.MAX_POS_PCT, plan.get("max_pos_pct") or 100) / 100 / price,
                  self.free_cash() * 0.98 / price,
                  (qvol * C.MAX_VOL_SHARE / price) if qvol else 1e18)  # yuksek tutar: likiditenin %0,2'sinden buyuk girme
        if plan.get("usdt"):  # elle alim: kullanicinin girdigi tutar (bos nakitle sinirli)
            qty = min(plan["usdt"] / price, self.free_cash() * 0.98 / price)
        qty = float(self.ex.amount_to_precision(s, max(qty, 0)))
        if qty * price < (self.ex.market(s)["limits"]["cost"]["min"] or 5):
            return f"tutar borsa alt sınırının altında ({qty * price:.2f} USDT)"
        p = {"id": None, "qty": qty, "price": price, "t": time.time(), **plan, "risk_pct": round(risk, 3)}
        if self.live:
            try:
                if taker:
                    o = self.ex.create_order(s, "market", "buy", qty)
                    self.open(s, p, o["filled"] or qty, o["average"] or price)
                    return None
                p["id"] = self.ex.create_order(s, "limit", "buy", qty, price, {"postOnly": True})["id"]
            except ccxt.BaseError as err:
                return f"borsa reddetti: {str(err)[:150]}"
        elif taker:
            self.open(s, p, qty, price * (1 + C.SLIPPAGE))
            return None
        self.st["pending"][s] = p
        self.say(f"{s} ALIŞ EMRİ (pasif limit) {qty:g} @ {price:g}", True)
        return None

    def manage_pending(self):
        for s, p in list(self.st["pending"].items()):
            expired = time.time() - p["t"] > C.ENTRY_TIMEOUT_MIN * 60
            if self.live:
                try:  # bir coindeki borsa hatasi digerlerini durdurmasin
                    self.finish_pending(s, p, cancel=expired)
                except ccxt.BaseError as e:
                    log.warning("%s bekleyen emir kontrolu: %s", s, e)
            elif self.ask.get(s) and self.ask[s] <= p["price"]:
                del self.st["pending"][s]
                self.open(s, p, p["qty"], p["price"])
            elif expired:
                del self.st["pending"][s]
                self.say(f"{s} alış emri {C.ENTRY_TIMEOUT_MIN} dk içinde dolmadı, iptal.")

    def finish_pending(self, s, p, cancel):
        if not self.live:
            self.st["pending"].pop(s, None)
            return
        o = self.ex.fetch_order(p["id"], s)
        if cancel and o["status"] == "open":
            try:
                self.ex.cancel_order(p["id"], s)
            except ccxt.OrderNotFound:
                pass
            o = self.ex.fetch_order(p["id"], s)
        if o["status"] == "open":
            return
        if o["filled"] and o["filled"] * o["average"] >= (self.ex.market(s)["limits"]["cost"]["min"] or 5):
            self.open(s, p, o["filled"], o["average"])
        else:
            self.say(f"{s} alış emri dolmadı, iptal.")
        self.st["pending"].pop(s, None)  # ancak pozisyon kaydedildikten sonra (dolan emir kaybolmasin)

    def open(self, s, p, qty, price):
        fee = qty * price * C.FEE
        if self.live:
            try:
                free = self.balance(s.split("/")[0])[0]
            except ccxt.BaseError:  # dolum oldu; bakiye okunamazsa komisyon dusulmus miktarla devam
                free = qty * (1 - C.FEE)
            qty = float(self.ex.amount_to_precision(s, min(qty * (1 - C.FEE), free)))
        else:
            self.st["cash"] -= qty * price + fee
        stop = p.get("stop") or price * 0.98  # eski surumden kalan emirlerde plan alanlari olmayabilir
        sl = min(stop, price * 0.998)
        atr = (p.get("atr_pct") or 0) * price or (price - sl) / 1.5  # ATR bilinmiyorsa stop 1,5 ATR varsayimi
        tp2 = p.get("tp2") or price + 2.5 * (price - sl)
        tp1 = p["tp1"] if price < (p.get("tp1") or 0) < tp2 else None
        hours = p.get("max_hours") or C.MAX_BARS
        ex = {k: v for k, v in (p.get("exit") or {}).items() if k in ("be_r", "trail_atr", "part_r", "part_frac",
                                                                       "ts_h", "ts_r", "max_h")}
        if ex:  # Gecmis Okulu sampiyon cikisi (kurulum turune gore): kismi kar seviyesi, sure ve kalkan pozisyona yazilir
            tp1 = price + ex["part_r"] * (price - sl) if ex.get("part_frac") and price + ex["part_r"] * (price - sl) < tp2 \
                else None
            hours = ex.get("max_h") or hours
        pos = {"qty": qty, "qty0": qty, "entry": price, "atr": atr, "sl": sl, "sl0": sl, "tp": tp2,
               "partial_at": tp1, "part": False, "hi": price, "fee": fee, "realized": 0.0, "t": time.time(),
               "stop_id": None, "max_hours": hours, "risk_pct": p.get("risk_pct"),
               "conviction": p.get("conviction"), "why": p.get("reason", ""), "p": p.get("p"), "be": False,
               "sigs": p.get("sigs") or [], "feats": p.get("feats") or {}, "edge": p.get("edge"),
               "btc0": self.bid.get(C.BTC), "manual": bool(p.get("manual")), "kind": p.get("kind"), "exit": ex,
               "seen": time.time(), "mc": p.get("mc")}
        self.st["positions"][s] = pos
        if self.live:
            pos["stop_id"] = self.place_stop(s, pos)
            self.sync()  # kasa hesabi dolumdan hemen sonra dogru olsun
        if pos["kind"] == PF.KIND:
            return self.say(f"{s} ALINDI {qty:g} @ {price:g} ({qty * price:.2f} USDT) | portföy stratejisi: "
                            f"{p.get('reason', '')} | felaket stopu {sl:.6g}", True)
        self.say(f"{s} ALINDI {qty:g} @ {price:g} | stop {sl:.6g}"
                 + (f", kısmi kâr {tp1:.6g}" if tp1 else "")
                 + (" | Manuel Kilit (stop/hedef kullanıcıda)" if pos["manual"] else f", hedef {tp2:.6g}, en fazla {hours:g} sa")
                 + ("\n" + MC.line(pos["mc"]) if pos.get("mc") else ""), True)

    def manage_positions(self):
        for s, pos in list(self.st["positions"].items()):
            try:  # bir coindeki borsa hatasi diger pozisyonlarin korunmasini durdurmasin
                self._manage(s, pos)
            except ccxt.BaseError as e:
                log.warning("%s pozisyon yonetimi: %s", s, e)

    def _fill_part(self, pos, filled, avg):
        """Borsa stopu kismen doldu: o kismi gerceklesmis kar/zarar olarak dus."""
        if filled and avg:
            pos["realized"] += filled * (avg - pos["entry"]) - filled * avg * C.FEE
            pos["qty"] = max(pos["qty"] - filled, 0.0)

    def replay_gap(self, s, pos):
        """PAPER: uygulama kapaliyken gecen sureyi 1 dk mumlarla oynatir (stop/hedef/kismi/kalkan sirasiyla). Sanal
        hesapta borsa stopu yok: kesinti sonucu bozmasin. Donus: kapandiysa True."""
        if "seen" not in pos:  # eski surumden kalan pozisyon: son gorulme bilinmiyor, oynatma yok
            pos["seen"] = time.time()
            return False
        since, now = int(pos["seen"] * 1000), int(time.time() * 1000)
        if self.live or now - since < 120_000:
            return False
        bars = []
        while since < now and len(bars) < 4320:  # en fazla 3 gun
            b = self.ex.fetch_ohlcv(s, "1m", since=since, limit=1000)
            if not b:
                break
            bars += b
            since = b[-1][0] + 60_000
        for t, o, h, low, c, _ in bars:
            if low <= pos["sl"]:
                px = min(o, pos["sl"]) * (1 - C.SLIPPAGE)
                self.say(f"{s}: uygulama kapalıyken stop tetiklendi ({time.strftime('%H:%M', time.localtime(t / 1000))}).")
                self.book(s, pos, px, self._stop_reason(pos))
                return True
            if h >= pos["tp"]:
                self.book(s, pos, pos["tp"], "HEDEF")
                return True
            if not pos.get("manual"):
                if not pos["part"] and pos.get("partial_at") and h >= pos["partial_at"]:
                    self.bid[s] = pos["partial_at"]
                    self.partial(s, pos, pos["partial_at"])
                self._shield(s, pos, c)
                if (t / 1000 - pos["t"]) / 3600 >= pos.get("max_hours", C.MAX_BARS):
                    self.book(s, pos, c, "SURE_DOLDU")
                    return True
        pos["seen"] = time.time()
        return False

    def _manage(self, s, pos):
        bid = self.bid.get(s)
        if not bid:
            return
        pos["seen"] = time.time()
        if self.live and time.time() - pos.get("stop_chk", 0) > 15:
            pos["stop_chk"] = time.time()
            if not pos.get("stop_id") and time.time() - pos.get("stop_try", 0) > 60:
                pos["stop_try"] = time.time()  # borsa stopu kurulamamisti: dakikada bir yeniden dene
                pos["stop_id"] = self.place_stop(s, pos, quiet=True)
            elif pos.get("stop_id"):
                o = self.ex.fetch_order(pos["stop_id"], s)
                if o["status"] == "closed":
                    return self.book(s, pos, o["average"], self._stop_reason(pos))
                if o["status"] in ("canceled", "expired", "rejected"):  # borsa stopu dustu: yeniden kur
                    self._fill_part(pos, o.get("filled"), o.get("average"))
                    pos["stop_id"], pos["stop_try"] = None, 0
        hours = (time.time() - pos["t"]) / 3600
        if bid > pos["sl"]:
            pos.pop("stop_wait", None)
        if bid <= pos["sl"]:
            exch = self.live and pos.get("stop_id") and (pos.get("stop_px") or 0) >= pos["sl"] * 0.999
            if exch and bid >= pos["sl"] * 0.995 and time.time() - pos.setdefault("stop_wait", time.time()) < 20:
                pos["stop_chk"] = 0  # borsadaki stop-limit bant icinde stop fiyatindan doluyor: piyasa satisi yapma,
                return               # bir sonraki saniye dolumu kaydet (20 sn dolmazsa piyasadan cik)
            self.exit(s, pos, self._stop_reason(pos), stop=True)
        elif bid >= pos["tp"]:
            self.exit(s, pos, "HEDEF")
        else:
            if pos.get("manual") or pos.get("kind") == PF.KIND:  # Manuel Kilit / portfoy: kademeli kar, kalkan, sure yok
                return
            if not pos["part"] and pos.get("partial_at") and bid >= pos["partial_at"]:
                self.partial(s, pos, bid)
                if s not in self.st["positions"]:
                    return
            self._shield(s, pos, bid)
            ex = pos.get("exit") or {}
            if ex.get("ts_h") and hours >= ex["ts_h"] and not pos.get("ts_done") and s in self.st["positions"]:
                pos["ts_done"] = True  # zaman stopu: N saatte hedefin yonunde ilerlemeyen pozisyon (2 yillik testle)
                if pos["hi"] - pos["entry"] < ex.get("ts_r", 0.3) * (pos["entry"] - pos["sl0"]):
                    return self.exit(s, pos, "ZAMAN_STOPU")
            if hours >= pos.get("max_hours", C.MAX_BARS) and s in self.st["positions"]:
                self.exit(s, pos, "SURE_DOLDU")

    def _shield(self, s, pos, bid):
        """Sifir zarar kalkani: kar tetigi gecilince stop komisyon dahil basabasin ustune, sonra zirveyi izleyerek
        kazanilan yolu kilitler. Stop sadece yukari gider; borsa tarafi stop en gec 20 sn'de esitlenir."""
        e = pos["entry"]
        r0 = e - (pos.get("sl0") or pos["sl"])
        pos["hi"] = max(pos.get("hi") or e, bid)
        if r0 <= 0 or s not in self.st["positions"]:
            return
        new, ex = pos["sl"], pos.get("exit") or {}
        bep = L.be_price(e, pos.get("atr"))
        if not pos.get("be") and ex.get("be_r", 1) != 0 and pos["hi"] - e >= L.be_trigger(e, r0, ex.get("be_r"), pos.get("atr")):
            new, pos["be"] = max(new, bep), True
            self.say(f"{s} kâra geçti: stop başabaşın üstüne ({bep:.6g}, girişe göre %{(bep / e - 1) * 100:+.2f}) çekildi. Bu işlem artık zararla "
                     f"kapanmaz (ani fiyat boşluğu hariç); kâr ilerledikçe stop arkadan kilitleyerek izleyecek.", kind="kalkan")
        if pos.get("be"):
            new = max(new, min(L.trail(e, r0, pos["hi"], pos["atr"], ex.get("trail_atr")), bid * 0.998))
        pos["sl"] = max(pos["sl"], new)
        noted = pos.get("lock_px") or bep
        if pos.get("be") and pos["sl"] >= noted + max(e * 0.01, 0.5 * r0) and time.time() - pos.get("lock_note", 0) > 900:
            pos["lock_px"], pos["lock_note"] = pos["sl"], time.time()  # kar kilidi belirgin yukseldi (15 dk'da en fazla bir)
            self.say(f"{s} kâr kilidi yükseldi: stop {pos['sl']:.6g}, girişe göre %{(pos['sl'] / e - 1) * 100:+.2f} "
                     f"kâr kilitli.", kind="kalkan")
        if self.live and pos.get("stop_id") and pos["sl"] > (pos.get("stop_px") or 0) * 1.0005 \
                and time.time() - pos.get("mv_t", 0) > 20:
            pos["mv_t"] = time.time()
            self._replace_stop(s, pos)

    @staticmethod
    def _stop_reason(pos):
        return "IZ_STOP" if pos["sl"] > L.be_price(pos["entry"], pos.get("atr")) * 1.001 else (
            "BASABAS" if pos["sl"] >= pos["entry"] else "STOP")

    def _sellable(self, s, qty):
        """LIVE: elde gercekten serbest olan miktar (stop kismen dolmus olabilir), borsa adimina yuvarlanmis."""
        try:
            return float(self.ex.amount_to_precision(s, min(qty, self.balance(s.split("/")[0])[0])))
        except ccxt.InvalidOrder:  # en kucuk miktarin altinda: satilacak bir sey yok
            return 0.0

    def partial(self, s, pos, bid):
        """Kademeli kar: pozisyonun bir kismini sat, stopu komisyon dahil basabasa cek."""
        frac = (pos.get("exit") or {}).get("part_frac") or C.PARTIAL_FRAC
        qty = float(self.ex.amount_to_precision(s, pos["qty0"] * frac))
        if qty * bid < (self.ex.market(s)["limits"]["cost"]["min"] or 5) or qty >= pos["qty"]:
            pos["part"] = True  # cok kucuk: kismi satis yapmadan sadece stopu cek
        else:
            if self.live:
                filled = self._cancel_stop(s, pos)
                if filled:  # borsa stopu tam bu sirada doldu
                    return self.book(s, pos, filled, self._stop_reason(pos))
                qty = self._sellable(s, qty)
                if qty <= 0:
                    return self.book(s, pos, bid, self._stop_reason(pos))
                pos["part"] = True  # satis hata verip tekrar denenirse ikinci kez satilmasin
                o = self.ex.create_order(s, "market", "sell", qty)
                bid = o["average"] or bid
            else:
                self.st["cash"] += qty * bid * (1 - C.FEE)
            pos["realized"] += qty * (bid - pos["entry"]) - qty * bid * C.FEE
            pos["qty"] = float(self.ex.amount_to_precision(s, round(pos["qty"] - qty, 12)))
            pos["part"] = True
        pos["sl"], pos["be"] = max(pos["sl"], L.be_price(pos["entry"], pos.get("atr"))), True
        pos["hi"] = max(pos.get("hi") or bid, bid)
        if self.live:
            pos["stop_id"] = self.place_stop(s, pos)
            self.sync()
        self.say(f"{s} kademeli kâr: %{frac * 100:.0f} satıldı @ {bid:g}, stop başabaşa çekildi, "
                 f"kalan kısım iz süren stopla takipte.", True)

    def exit(self, s, pos, reason, stop=False):
        bid = self.bid[s]
        if not self.live:
            if stop:  # gercek hesaptaki borsa STOP_LOSS_LIMIT emri (limit = stop x 0,995) ile ayni dolum mantigi:
                bid = pos["sl"] if bid >= pos["sl"] * 0.995 else bid  # bant icinde stoptan dolar, bosluk buyukse fiyattan
            return self.book(s, pos, bid * (1 - C.SLIPPAGE) if stop else bid, reason)
        filled = self._cancel_stop(s, pos)  # stop iptal edilemezse hata firlatir; sonraki saniye tekrar denenir
        if filled:
            return self.book(s, pos, filled, self._stop_reason(pos))
        qty = self._sellable(s, pos["qty"])
        if qty * bid < (self.ex.market(s)["limits"]["cost"]["min"] or 5):  # satilamayacak kadar az kaldi
            return self.book(s, pos, bid, reason)
        o = self.ex.create_order(s, "market", "sell", qty)
        self.book(s, pos, o["average"] or bid, reason)

    def close(self, s, reason):
        """Elle veya Claude karariyla kapat: bekleyen emir iptal, pozisyon piyasa fiyatindan satilir."""
        if s in self.st["pending"]:
            p = self.st["pending"][s]
            if self.live:
                self.finish_pending(s, p, cancel=True)
            else:
                del self.st["pending"][s]
            self.say(f"{s} bekleyen alış emri iptal edildi ({reason}).")
        pos = self.st["positions"].get(s)
        if pos and self.bid.get(s):
            self.exit(s, pos, reason)
            return True
        return False

    # ---------------- portfoy stratejisi (portfoy.py): hedef agirliga gore al / azalt ----------------
    def pf_buy(self, s, usdt, why):
        """Hedef agirliga eksik kismi al: pozisyon yoksa ac, varsa ortalama girisle buyut. Hata metni veya None."""
        pos, price = self.st["positions"].get(s), self.ask.get(s)
        if not price:
            return "fiyat yok"
        if not pos:
            if s in self.st["pending"]:
                return "bekleyen emir var"
            return self.enter(s, {"type": "buy", "symbol": s, "kind": PF.KIND, "entry": "taker", "usdt": usdt,
                                  "stop": price * PF.STOP_FRAC, "tp2": price * 1000, "tp1": 0, "risk_pct": 0,
                                  "max_hours": 1e6, "reason": why, "conviction": None})
        if pos.get("kind") != PF.KIND:
            return "coinde strateji dışı pozisyon var"
        qty = float(self.ex.amount_to_precision(s, max(min(usdt, self.free_cash() * 0.98), 0) / price))
        if qty * price < (self.ex.market(s)["limits"]["cost"]["min"] or 5):
            return f"tutar borsa alt sınırının altında ({qty * price:.2f} USDT)"
        if self.live:
            filled = self._cancel_stop(s, pos)
            if filled:
                self.book(s, pos, filled, self._stop_reason(pos))
                return "felaket stopu doldu"
            o = self.ex.create_order(s, "market", "buy", qty)
            price = o["average"] or price
            qty = float(self.ex.amount_to_precision(s, (o["filled"] or qty) * (1 - C.FEE)))
        else:
            price *= 1 + C.SLIPPAGE
            self.st["cash"] -= qty * price * (1 + C.FEE)
        tot = pos["qty"] + qty
        pos.update(entry=(pos["qty"] * pos["entry"] + qty * price) / tot, qty=tot, qty0=pos["qty0"] + qty,
                   fee=pos["fee"] + qty * price * C.FEE)
        pos["sl"] = pos["sl0"] = pos["entry"] * PF.STOP_FRAC
        if self.live:
            pos["stop_id"] = self.place_stop(s, pos)
            self.sync()
        self.say(f"{s} portföy ARTIRILDI +{qty:g} @ {price:g} ({qty * price:.2f} USDT) | {why}", True)
        return None

    def pf_sell(self, s, usdt, why):
        """Hedef agirligin ustundeki kismi sat; usdt None veya pozisyonun tamami: tam cikis."""
        pos, bid = self.st["positions"].get(s), self.bid.get(s)
        if not pos or not bid:
            return "pozisyon yok"
        qty = pos["qty"] if usdt is None else float(self.ex.amount_to_precision(s, min(usdt / bid, pos["qty"])))
        if qty >= pos["qty"] * 0.97 or (pos["qty"] - qty) * bid < (self.ex.market(s)["limits"]["cost"]["min"] or 5):
            self.exit(s, pos, "PORTFOY_CIKIS")
            return None
        if qty * bid < (self.ex.market(s)["limits"]["cost"]["min"] or 5):
            return "tutar borsa alt sınırının altında"
        if self.live:
            filled = self._cancel_stop(s, pos)
            if filled:
                self.book(s, pos, filled, self._stop_reason(pos))
                return None
            qty = self._sellable(s, qty)
            o = self.ex.create_order(s, "market", "sell", qty)
            px = o["average"] or bid
        else:
            px = bid * (1 - C.SLIPPAGE)
            self.st["cash"] += qty * px * (1 - C.FEE)
        pos["realized"] += qty * (px - pos["entry"]) - qty * px * C.FEE
        pos["qty"] = float(self.ex.amount_to_precision(s, round(pos["qty"] - qty, 12)))
        if self.live:
            pos["stop_id"] = self.place_stop(s, pos)
            self.sync()
        self.say(f"{s} portföy AZALTILDI -{qty:g} @ {px:g} ({qty * px:.2f} USDT) | {why}", True)
        return None

    def rebalance(self, tg, ml_day):
        """Hedef agirliklar {sym: 0..1} (portfoy tabani = nakit + strateji pozisyonlari). Once satislar, sonra alislar."""
        with self.b.lock:
            held = {s: p for s, p in self.st["positions"].items() if p.get("kind") == PF.KIND}
            px = self.bid
            base = (self.usdt_total if self.live else self.st["cash"]) + \
                sum(p["qty"] * px[s] for s, p in held.items() if px.get(s))
        if base <= 0:
            return ["kasa yok"]
        band, sells, buys, out = PF.BAND_ML if ml_day else PF.BAND, [], [], []
        for s in sorted(set(tg) | set(held)):
            if not px.get(s):
                continue
            cur = held[s]["qty"] * px[s] / base if s in held else 0.0
            t = tg.get(s, 0.0)
            d = t - cur
            if s in held and t < 0.005:
                sells.append((s, None, f"hedef ağırlık 0 (şu an %{cur * 100:.1f})"))
            elif abs(d) >= band or (s not in held and t >= 0.01):
                (buys if d > 0 else sells).append((s, abs(d) * base, f"ağırlık %{cur * 100:.1f} → %{t * 100:.1f}"))
        for s, usd, why in sells:
            with self.b.lock:
                r = self.pf_sell(s, usd, why)
            out.append(f"{s} sat: {r or why}")
        stop = self.blocked(portfolio=True)
        if buys and stop:
            return out + [f"alışlar yapılmadı: {stop}"]
        for s, usd, why in buys:
            if self.b.stop.is_set():  # uygulama kapaniyor: yeni emir verme
                break
            with self.b.lock:
                if s in self.st["positions"] and self.st["positions"][s].get("kind") != PF.KIND:
                    r = "coinde elle/strateji dışı pozisyon var"
                else:
                    r = self.pf_buy(s, usd, why)
            out.append(f"{s} al: {r or why}")
        return out or ["değişiklik yok (ağırlıklar hedefe yakın)"]

    def set_levels(self, s, sl=None, tp=None, who="kullanıcı"):
        """Kullanicinin elle stop/hedefi. Manuel Kilit acilir: Claude, yerel motor ve sifir zarar kalkani bu seviyelere
        dokunmaz. Gercek hesapta borsadaki stop emri hemen yeni fiyata tasinir. Hata metni veya None."""
        pos, bid = self.st["positions"].get(s), self.bid.get(s)
        if not pos or not bid:
            return "Bu coinde açık pozisyon yok."
        if sl is not None and not 0 < sl < bid * 0.999:
            return f"Stop anlık fiyatın ({bid:g}) altında olmalı."
        if tp is not None and tp <= bid * 1.001:
            return f"Hedef anlık fiyatın ({bid:g}) üstünde olmalı."
        old = (pos["sl"], pos["tp"])
        if sl is not None:
            pos["sl"] = sl
        if tp is not None:
            pos["tp"] = tp
        pos["manual"], pos["partial_at"] = True, None
        if self.live and sl is not None:
            self._replace_stop(s, pos)
        self.say(f"{s} elle güncellendi ({who}): stop {old[0]:.6g} → {pos['sl']:.6g}, hedef {old[1]:.6g} → "
                 f"{pos['tp']:.6g}. Manuel Kilit açık: yapay zeka ve kalkan bu seviyelere dokunmayacak.", kind="kalkan")
        return None

    def set_auto(self, s, who="kullanıcı"):
        """Manuel Kilidi kaldir: kontrol tekrar yapay zekaya ve sifir zarar kalkanina gecer."""
        pos = self.st["positions"].get(s)
        if not pos:
            return "Bu coinde açık pozisyon yok."
        pos["manual"] = False
        self.say(f"{s} kontrolü yapay zekaya devredildi ({who}): sıfır zarar kalkanı ve karar beyni yeniden yönetiyor.",
                 kind="kalkan")
        return None

    def adjust(self, s, stop, tp2):
        """Claude'un koruma guncellemesi: stop sadece yukari, fiyatin altinda kalacak sekilde."""
        pos, bid = self.st["positions"].get(s), self.bid.get(s)
        if not pos or not bid:
            return "pozisyon yok"
        if pos.get("manual"):
            return "Manuel Kilit: kullanıcı yönetiyor, değiştirilmedi"
        if pos.get("kind") in (GT.KIND, PF.KIND):  # test edilen kural: kendi gunluk kuraliyla yonetilir
            return "strateji pozisyonu kendi kuralıyla yönetiliyor, değiştirilmedi"
        out = []
        if stop and pos["sl"] < stop < bid * 0.998:
            pos["sl"] = stop
            out.append(f"stop {stop:.6g}")
        if tp2 and tp2 > bid * 1.002:
            pos["tp"] = tp2
            out.append(f"hedef {tp2:.6g}")
        if not out:
            return "geçerli değişiklik yok (stop sadece yukarı ve fiyatın altında olabilir)"
        if self.live and "stop" in out[0]:
            self._replace_stop(s, pos)
        self.say(f"{s} koruma güncellendi: {', '.join(out)}.")
        return None

    def place_stop(self, s, pos, quiet=False):
        """LIVE: bot kapali olsa da koruyan borsa tarafi stop emri."""
        try:
            sp = float(self.ex.price_to_precision(s, pos["sl"]))
            oid = self.ex.create_order(s, "STOP_LOSS_LIMIT", "sell", float(self.ex.amount_to_precision(s, pos["qty"])),
                                       float(self.ex.price_to_precision(s, sp * 0.995)),
                                       {"stopPrice": sp, "timeInForce": "GTC"})["id"]
            pos["stop_px"] = sp
            return oid
        except ccxt.BaseError as e:
            if not quiet:
                self.say(f"{s} borsa stop emri kurulamadı; bot saniyelik izliyor ve dakikada bir yeniden deniyor: {e}",
                         True, "uyari")
            return None

    def _cancel_stop(self, s, pos):
        """Borsa stopunu iptal eder. Tamamen dolduysa dolum fiyatini dondurur; kismen dolduysa o kismi duser.
        Iptal edilemiyor ve emir hala aciksa hata firlatir (coinler kilitli: satis denenmez)."""
        sid = pos.get("stop_id")
        if not sid:
            return None
        try:
            o = self.ex.cancel_order(sid, s)
        except ccxt.BaseError:
            o = self.ex.fetch_order(sid, s)
            if o["status"] == "open":
                raise
        pos["stop_id"] = None
        if o.get("status") == "closed":
            return o["average"]
        self._fill_part(pos, o.get("filled"), o.get("average"))
        return None

    def _replace_stop(self, s, pos):
        filled = self._cancel_stop(s, pos)
        if filled:
            return self.book(s, pos, filled, self._stop_reason(pos))
        pos["stop_id"] = self.place_stop(s, pos)

    def book(self, s, pos, price, reason):
        st, now = self.st, time.time()
        exit_fee = pos["qty"] * price * C.FEE
        pnl = pos["realized"] + pos["qty"] * (price - pos["entry"]) - pos["fee"] - exit_fee
        pct = pnl / (pos["qty0"] * pos["entry"]) * 100
        if not self.live:
            st["cash"] += pos["qty"] * price - exit_fee
        st["positions"].pop(s, None)
        st["pnl"] += pnl
        st["wins" if pnl > 0 else "losses"] += 1
        st["cooldown"][s] = now
        e0, sl0 = pos["entry"], pos.get("sl0") or pos["sl"]
        risk = pos["qty0"] * (e0 - sl0)
        r_mult = pnl / risk if risk > 0 else 0.0  # gercek olcu: risk birimi (R); kazanma orani tek basina yaniltir
        st["trades"] = (st["trades"] + [{"t": now, "t_open": pos["t"], "sym": s, "entry": pos["entry"], "exit": price,
                                         "qty": pos["qty0"], "pnl": pnl, "pct": pct, "reason": reason,
                                         "partial": pos["part"], "p": pos.get("p"), "why": pos.get("why", ""),
                                         "conviction": pos.get("conviction"), "sl0": sl0, "R": round(r_mult, 3),
                                         "mfeR": round((pos.get("hi", e0) - e0) / (e0 - sl0), 3) if e0 > sl0 else None,
                                         "kind": pos.get("kind"), "manual": bool(pos.get("manual"))}])[-1000:]
        self.say(f"{s} SATILDI ({reason}) @ {price:g} | net {pnl:+.2f} USDT ({pct:+.2f}%, {r_mult:+.2f}R)", True)
        try:  # otopsi: Ders Defteri'ne yaz + trading_memory.md (hata islem kaydini engellemesin)
            btc0, btc = pos.get("btc0"), self.bid.get(C.BTC)
            txt = L.autopsy(self.mode, s, pos, price, reason, pnl, pct, self.b.market.get(s),
                            (btc / btc0 - 1) * 100 if btc and btc0 else None)
            mct = MC.record(self.mode, s, pos.get("mc"), reason, r_mult)  # Monte Carlo tahmini vs gerceklesen
            if mct:
                txt += " " + mct
            self.say("Otopsi: " + txt, kind="oz")
            HF.record_trade(self.mode, s, pos, price, reason, pnl, r_mult, txt)
        except Exception as e:
            log.warning("otopsi %s: %s", s, e)
        if self.live:
            self.sync()  # satis geliri kasaya hemen yansisin (yanlis dusus korumasi tetiklenmesin)
        if reason == "STOP":  # stop serisi korumasi
            n, hrs, rest = C.STOP_GUARD
            st["stops"] = [t for t in st["stops"] if now - t < hrs * 3600] + [now]
            if len(st["stops"]) >= n:
                st["pause_until"], st["stops"] = max(st["pause_until"], now + rest * 3600), []
                self.say(f"{hrs} saatte {n} stop oldu: {rest} saat yeni işlem açılmayacak.", True, "uyari")


class Bot:
    def __init__(self):
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.status, self.force_retrain, self.busy, self.on_notify = "Baslatiliyor", False, None, None
        self.bid, self.ask, self.brain, self.symbols = {}, {}, None, list(C.SYMBOLS)
        self.market, self.regime, self.last_scan, self.extra = {}, "yatay", {}, set()
        self.stream, self.tech, self.macro, self._last_rest = Stream.shared(), Teknik.shared(), Makro.shared(), 0.0
        self._tv_prev, self._ref_px, self._btc_hist, self._brief_px, self.updated = {}, {}, [], {}, time.time()
        self._radar_prev, self.chain = set(), Zincir.shared()
        self.file = {}
        if os.path.exists(C.STATE_FILE):
            with open(C.STATE_FILE, encoding="utf-8") as fh:
                self.file = json.load(fh)
        old = [dict(n, acct=m) for m in ACCTS for n in (self.file.get(m) or {}).get("notes", [])]  # eski surum
        self.notes = self.file.get("notes") or sorted(old, key=lambda n: n["t"])[-500:]
        self.ag = {"day": "", "spent": 0.0, "calls": 0, "memo": "", "last": 0, "last_event": 0, "error": "",
                   "decisions": [], "rejects": [], "last_review": 0, "total": 0.0}
        self.ag.update(self.file.get("agent", {}))
        self.ex = D.exchange()
        L.init()  # Ders Defteri veritabani (otopsiler bu andan itibaren yazilabilir)
        self.accounts = {"PAPER": Account(self, "PAPER", self.file.get("PAPER", {}))}
        self.live_error = ""
        if C.API_KEY and C.API_SECRET:
            self.accounts["LIVE"] = Account(self, "LIVE", self.file.get("LIVE", {}))

    # ---------------- yardimcilar ----------------
    def say(self, msg, notify=False, kind="sistem", acct=None, tg=True):
        """Karar gunlugune not. kind: karar/bekleme/islem/oz/egitim/uyari/sistem. acct: PAPER/LIVE/None (ortak)."""
        with self.lock:
            self.notes = (self.notes + [{"t": time.time(), "kind": kind, "text": msg, "acct": acct}])[-800:]
        log.info("[%s] %s", acct or "-", msg)
        if notify and self.on_notify:  # masaustu bildirimi (app.py baglar)
            try:
                self.on_notify(msg)
            except Exception:
                pass
        if tg and C.TELEGRAM_TOKEN and (notify or kind in ("karar", "kalkan", "radar", "oz")):  # telefona kart
            import telegram_bot
            threading.Thread(target=telegram_bot.notify, args=(msg, kind, acct), daemon=True).start()

    def save(self):
        with self.lock:
            self.file.update({m: a.st for m, a in self.accounts.items()})
            self.file.update(notes=self.notes, agent=self.ag)
            data = json.dumps(self.file)
        tmp = C.STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(data)
        os.replace(tmp, C.STATE_FILE)

    def add_cost(self, usd, chat=False):
        """Sohbet dahil her Claude cagrisinin maliyeti gunluk butceye yazilir."""
        with self.lock:
            if self.ag["day"] != today():
                self.ag.update(day=today(), spent=0.0, calls=0, chat_spent=0.0)
            self.ag.update(spent=self.ag["spent"] + usd, total=self.ag.get("total", 0) + usd)
            if chat:
                self.ag["chat_spent"] = self.ag.get("chat_spent", 0) + usd

    def ref(self):
        """Claude'un karar verirken baktigi hesap: gercek islem acikken LIVE, degilse PAPER."""
        lv = self.accounts.get("LIVE")
        return lv if lv and C.LIVE_ALLOWED and lv.synced else self.accounts["PAPER"]

    def ctx(self):
        """Ders Defteri sinyalleri icin piyasa baglami."""
        a = (self.market.get(C.BTC) or {}).get("atr_pct")  # piyasa volatilitesi: BTC 1 saatlik ATR yuzdesi
        return {"regime": self.regime, "macro": self.macro.risk()[0],
                "vol": "normal" if a is None else "dusuk" if a < 0.0035 else "yuksek" if a > 0.009 else "normal"}

    def rows(self):
        """Karar evreni (kilit icinde cagrilir): izlenen coinler + tum piyasa radarinin bulgulari, ayni bicimde."""
        out = {s: dict(r) for s, r in self.market.items() if r.get("price")}
        for o in self.tech.opps:
            if o["sym"] in out:
                r = out[o["sym"]]
                r.update({k: o.get(k) for k in RADAR_KEYS})
                for k, v in o.items():  # radar coini market'e girince (alim/pozisyon) teknik ozet, RSI, ATR kaybolmasin
                    if k != "rt" and r.get(k) in (None, {}):
                        r[k] = v
            else:
                out[o["sym"]] = {k: v for k, v in o.items() if k != "rt"}
        bchg = (self.market.get(C.BTC) or {}).get("chg24")
        for s, r in out.items():
            if r.get("rs24") is None and r.get("chg24") is not None and bchg is not None:
                r["rs24"] = round(r["chg24"] - bchg, 2)
            w = self.chain.whale.get(s)
            if w and w["n"] >= 3 and time.time() - w["t"] < 600:  # en az 3 buyuk islem: anlamli balina akisi
                r["whale"] = w["ratio"]
        return out

    def snapshot(self, mode=None, notes=None):
        """Arayuz ve sohbet icin tutarli kopya. notes: gorulebilecek hesaplar (None = hepsi)."""
        with self.lock:
            a = self.accounts.get(mode) or self.ref()
            ns = [n for n in self.notes if notes is None or n.get("acct") in (None, *notes)]
            return copy.deepcopy({"mode": a.mode, "st": a.st, "market": self.market, "bid": self.bid, "notes": ns,
                                  "agent": self.ag, "regime": self.regime, "scan": self.last_scan})

    def ensure_brain(self):
        fresh = lambda b: (b and "net_model" in b and not self.force_retrain  # eski model: maliyet sonrasi model yok
                           and time.time() - b["trained"] < C.RETRAIN_DAYS * 86400)
        if not fresh(self.brain) and os.path.exists(C.MODEL_FILE):
            self.brain = joblib.load(C.MODEL_FILE)
            self.symbols = self.brain.get("symbols", self.symbols)
        if fresh(self.brain):
            return
        self.force_retrain = False
        self.say("ML modeli eğitiliyor: veri güncelleniyor, geçmiş test yapılıyor.", kind="egitim")

        def progress(m):
            self.status = m
            if not m.startswith(("Veri", "Gecmis test donemi", "Canli model")):
                self.say(m, kind="egitim")
        brain = M.train(self.ex, progress)
        self.brain, self.symbols = brain, brain["symbols"]
        self.say(f"Yeni ML modeli hazır ({len(self.symbols)} coin). Claude'a ek sinyal olarak verilir.", kind="egitim")

    def _background(self, name, fn):
        """Uzun isler (tarama, Claude karari, egitim) canli akisi durdurmadan arka planda calisir."""
        if self.busy and self.busy.is_alive():
            return False

        def run():
            try:
                fn()
            except (ccxt.NetworkError, requests.RequestException) as e:
                log.warning("%s ag hatasi: %s", name, e)
            except Exception as e:
                log.exception(name)
                self.say(f"{name} hatası: {e}", kind="uyari")
        self.busy = threading.Thread(target=run, daemon=True)
        self.busy.start()
        return True

    # ---------------- ana dongu ----------------
    def run(self):
        if os.path.exists(C.MODEL_FILE):  # izlenecek coin listesini hemen bil (tarayici bos kalmasin)
            self.symbols = joblib.load(C.MODEL_FILE).get("symbols", self.symbols)
        self.extra |= set(C.EXTRA_SYMBOLS)  # altin (PAXG) vb. her zaman izlenir ve islem gorebilir
        held = {s for a in self.accounts.values() for s in a.st["positions"]}
        self.stream.start(set(self.symbols) | held | self.extra)
        self.tech.start(lambda: list(dict.fromkeys(list(self.symbols) + C.EXTRA_SYMBOLS + [  # pozisyonlar da: tez takibi
            s for a in self.accounts.values() for s in a.st["positions"]])))
        self.macro.start()
        self.chain.start(lambda: [C.BTC, "ETH/USDT"] + [s for a in self.accounts.values() for s in a.st["positions"]]
                         + [o["sym"] for o in self.tech.opps[:10]])  # balina akisi: ana coinler + pozisyonlar + radar
        G.load()  # Gecmis Okulu dersleri (2 yillik test sonucu)
        PF.load_model()  # portfoy stratejisi ML sepeti (yoksa / 90 gunden eskiyse arka planda egitilir)
        PF.maybe_train(lambda t: self.say(t, kind="oz"))
        try:
            MC.calibrate()  # Monte Carlo: gecmis tahmin/gerceklesen karnesinden duzeltme carpani
        except Exception as e:
            log.warning("monte carlo kalibrasyon: %s", e)
        threading.Thread(target=L.loop, args=(self.stop, lambda t: self.say(t, kind="oz")), name="ders", daemon=True).start()
        self.refresh_prices()
        for a in self.accounts.values():
            if a.live:
                a.sync()
            with self.lock:
                for s, pos in list(a.st["positions"].items()):  # kapaliyken gecen sure (sanal hesap)
                    try:
                        a.replay_gap(s, pos)
                    except Exception as e:
                        log.warning("%s kesinti oynatma: %s", s, e)
                for s, p in list(a.st["pending"].items()):  # onceki oturumdan kalan alis emirleri
                    try:
                        a.finish_pending(s, p, cancel=True)
                    except Exception as e:  # dongude tekrar denenir; motor baslangicta cokmesin
                        log.warning("%s baslangic emir kontrolu: %s", s, e)
                a.mark()
        lv = self.accounts.get("LIVE")
        self.say("Bot başladı.")
        self.say(f"Sanal kasa {self.accounts['PAPER'].equity():.2f} USDT.", acct="PAPER")
        if lv:
            self.say(f"Gerçek hesap {lv.equity():.2f} USDT "
                     f"({'işlem izni açık' if C.LIVE_ALLOWED else 'admin izni bekleniyor'}).", acct="LIVE")
        first = C.OTO_SAATLIK and (C.QUOTA_SAVER or time.time() - self.ag["last"] > 1800)  # yerel karar ucretsiz
        self._background("Baslangic", lambda: (self.ensure_brain(), self.scan(startup=True),
                                               first and self.decide("baslangic")))
        last_hour = last_rep = int(time.time() // 3600)
        last_save, last_acc, last_trig = 0, time.time(), time.time()
        while not self.stop.is_set():
            try:
                if time.time() - last_acc > 30:
                    last_acc = time.time()
                    for a in self.accounts.values():
                        if a.live:
                            a.sync()
                self.refresh_prices()
                with self.lock:
                    for a in self.accounts.values():
                        a.manage_pending()
                        a.manage_positions()
                        a.mark()
                hour = int(time.time() // 3600)
                if hour != last_rep and time.time() % 3600 > 15:  # risk raporu, analizden bagimsiz her saat
                    last_rep = hour
                    for a in self.accounts.values():
                        if a.live:
                            a.sync()
                        with self.lock:
                            a.report()
                    try:
                        self.observe()
                    except Exception as e:  # gozlem gunlugu motoru durdurmasin
                        log.warning("gozlem gunlugu: %s", e)
                idle = not (self.busy and self.busy.is_alive())
                day = time.strftime("%Y-%m-%d", time.gmtime())
                if C.PORTFOY and self.ag.get("pf_d") != day and time.time() % 86400 > 300 and self.run_portfolio():
                    self.ag["pf_d"] = day  # gun kapanisindan 5 dk sonra (UTC), gunde bir; hata olsa da tekrar denemez
                if ((hour != last_hour and time.time() % 3600 > 15) or self.force_retrain) and idle:
                    last_hour = hour
                    self._background("Saatlik analiz", self.hourly)
                elif C.GUNLUK_TREND and idle and self.ag.get("daily_d") != day and time.time() % 86400 > 300:
                    self.ag["daily_d"] = day  # hata olsa da gunde bir (tekrar tekrar ag cagrisi olmasin)
                    self._background("Günlük trend", self.daily_trend)
                elif C.OTO_SAATLIK and time.time() - last_trig > 60 and idle:
                    last_trig = time.time()
                    ev = self.triggers()
                    if ev:
                        self._background("Olay karari", lambda: self.decide("olay", ev))
                    elif C.QUOTA_SAVER and time.time() - self.ag["last"] > min(C.LOCAL_EVERY_MIN, 5) * 60:
                        self._background("Tarama karari", lambda: self.decide("tarama"))  # ucretsiz 7/24 tarama
                if not (self.busy and self.busy.is_alive()):
                    self.status = "Çalışıyor"
                self.updated = time.time()
                if time.time() - last_save > 30:
                    self.save()
                    last_save = time.time()
            except (ccxt.NetworkError, requests.RequestException) as e:
                self.status = "Ağ hatası, tekrar denenecek"
                log.warning("Ag hatasi: %s", e)
            except Exception as e:
                log.exception("Dongu hatasi")
                self.say(f"HATA: {e}", kind="uyari")
            self.stop.wait(C.TICK_SEC)
        if self.busy and self.busy.is_alive():  # suren karar bitsin; durduktan sonra emir verilmesin
            self.busy.join(120)
        with self.lock:
            for a in self.accounts.values():
                for s, p in list(a.st["pending"].items()):
                    try:
                        a.finish_pending(s, p, cancel=True)
                    except Exception as e:
                        log.warning("%s kapanis emir iptali: %s", s, e)
        self.save()

    def hourly(self):
        self.ensure_brain()
        self.scan()
        if C.OTO_SAATLIK and not G.STATE["running"] and time.time() - (G.RES.get("t") or 0) > 7 * 86400:  # Gecmis Okulu (saatlik sistem icin) haftalik
            threading.Thread(target=G.run, args=(lambda t: self.say(t, kind="oz"),), name="gecmis", daemon=True).start()
        if not C.OTO_SAATLIK:  # saatlik otomatik al-sat kapali (testte avantajsiz): radar/ML guncel, emir yok
            return
        strat = C.QUOTA_SAVER and A.brain() != "local" and \
            time.time() - self.ag.get("last_strategy", 0) >= C.REVIEW_HOURS * 3600
        if strat:  # kota koruma: Claude sadece seyrek strateji incelemesinde (REVIEW_MODEL)
            self.ag["last_strategy"] = time.time()
        self.decide("strateji" if strat else "saatlik")
        self.self_review()

    def refresh_prices(self):
        """Canli WebSocket verisi (saniyelik); akis koptuysa REST ile yedek."""
        busy = {s for a in self.accounts.values() for s in a.st["positions"].keys() | a.st["pending"].keys()}
        watch = set(self.symbols) | busy | self.extra
        self.stream.set_symbols(watch)
        now, fallback = time.time(), {}
        # pozisyondaki bir coinin defteri 10 sn'den eskiyse (tek akis koptu) ya da akis tamamen sessizse REST yedegi
        stale = [s for s in busy if now - self.stream.snapshot(s)[1].get("t", 0) > 10]
        if not self.stream.live() or now - self._last_rest > 60 or (stale and now - self._last_rest > 10):
            self._last_rest = now  # hata olsa da saniyede bir tekrar denenmesin (Binance IP yasagi riski)
            try:
                fallback = self.ex.fetch_tickers(sorted(watch))
            except ccxt.BaseError as e:  # WebSocket verisiyle devam
                log.warning("REST fiyat yedegi: %s", e)
        with self.lock:
            for s in watch:
                tk, bk, dv = self.stream.snapshot(s)
                row = self.market.setdefault(s, {"sym": s})
                if bk and now - bk["t"] < 10:
                    self.bid[s], self.ask[s] = bk["bid"], bk["ask"]
                    row.update(ob=round(bk["imb"], 3), spread=round(bk["spread"], 4), ob_t=bk["t"])
                elif s in fallback:
                    self.bid[s], self.ask[s] = fallback[s]["bid"], fallback[s]["ask"]
                if tk and now - tk["t"] < 10:
                    row.update(price=tk["last"], chg24=tk["chg24"], qvol=tk["qvol"], px_t=tk["t"])
                elif s in fallback:
                    t = fallback[s]
                    row.update(price=t["last"], chg24=t.get("percentage"), qvol=t.get("quoteVolume"), px_t=now)
                if dv.get("funding") is not None and now - dv.get("t", 0) < 30:  # Bybit canli (tahmini) fonlama
                    row.update(funding=dv["funding"], oi_now=dv.get("oi"))
                te = self.tech.data.get(s)
                if te:
                    row.update(tech={tf: v["rec"] for tf, v in te.items()})

    # ---------------- analiz ----------------
    def self_review(self):
        """Gunde bir: ML tahminlerini gerceklesen sonuclarla karsilastir; model sapmissa yeniden egit."""
        if time.time() - self.ag.get("last_review", 0) < 86400:
            return
        self.ag["last_review"] = time.time()
        self.status = "Öz-değerlendirme"
        rv = M.review(M.build_dataset(self.ex, lambda m: None, self.symbols))
        if not rv:
            return
        n, pred, real = rv
        ok = real >= pred - C.DRIFT_MARGIN
        self.say(f"ML öz-değerlendirme: son {n} tahminde beklenen hedef oranı %{pred * 100:.0f}, gerçekleşen "
                 f"%{real * 100:.0f}. " + ("Tutarlı." if ok else "Fazla iyimser -> yeniden eğitiliyor."), kind="oz")
        if not ok:
            self.force_retrain = True

    def orderbook(self, s):
        """Giris filtresi: fiyatin %1 cevresinde alici payi ve spread (%). Her zaman ayni olcu (REST, 100 kademe);
        tarayicidaki saniyelik deger ilk 20 kademedir."""
        ob = self.ex.fetch_order_book(s, limit=100)
        if not ob["bids"] or not ob["asks"]:
            return 0.5, 99
        mid = (ob["bids"][0][0] + ob["asks"][0][0]) / 2
        b = sum(p * q for p, q, *_ in ob["bids"] if p >= mid * 0.99)
        a = sum(p * q for p, q, *_ in ob["asks"] if p <= mid * 1.01)
        return b / (a + b) if a + b else 0.5, (ob["asks"][0][0] - ob["bids"][0][0]) / mid * 100

    def scan(self, startup=False):
        """Saatlik: kapanan mumla ozellikler + ML olasiligi + piyasa rejimi (Claude'a giden tablonun temeli)."""
        self.status = "Piyasa taranıyor"
        model = self.brain["model"]
        dfs = {s: df.iloc[-1500:] for s, df in M.load_all(self.ex, self.symbols).items()}  # 1500 mum yeterli
        if C.BTC not in dfs:
            return
        feats = {s: D.features(df, dfs[C.BTC]).iloc[[-1]] for s, df in dfs.items()}
        breadth = sum(float(f.ema50.iloc[0]) > 0 for f in feats.values()) / max(len(feats), 1)
        mkt_r24 = sum(float(f.r24.iloc[0]) for f in feats.values()) / max(len(feats), 1)
        ok, journal = {}, []
        for s, f in feats.items():
            f = f.assign(breadth=breadth, mkt_r24=mkt_r24)
            if not f[M.CORE].isna().any(axis=None) and time.time() - f.index[0].timestamp() < 2 * 3600:
                ok[s] = f
        if not ok:
            return
        Xs = pd.concat(ok.values())[M.FEATS]
        probs = model.predict_proba(Xs)[:, 1]  # tek cagri: hizli
        pnet = self.brain["net_model"].predict_proba(Xs)[:, 1] if "net_model" in self.brain else [None] * len(Xs)
        with self.lock:
            for s in set(self.symbols) - ok.keys():  # verisi alinamayan coin eski saatin degerleriyle kalmasin
                self.market.setdefault(s, {"sym": s}).update(p=None, ev=None, scan_err="Veri alinamadi/bayat")
            for (s, f), p, pn in zip(ok.items(), probs, pnet):
                g = lambda k: float(f[k].iloc[0])
                self.market.setdefault(s, {"sym": s}).update(
                    p=round(float(p), 4), pn=None if pn is None else round(float(pn), 4), ev=round(float(M.ev(p, g("atr_pct"))), 5), atr_pct=g("atr_pct"),
                    rsi=g("rsi"), r1=g("r1"), r24=g("r24"), flow24=g("flow24"), oi24=g("oi24"), scan_err=None,
                    squeeze=g("squeeze"), vol_accel=g("vol_accel"), ema50=g("ema50"), ema200=g("ema200"))
                journal.append(f"{f.index[0].isoformat()},{s},{float(p):.4f},{float(M.ev(p, g('atr_pct'))):.5f}")
            btc = feats[C.BTC]
            self.regime = str(M.regime(float(btc.ema50.iloc[0]), float(btc.r24.iloc[0]), breadth))
            busy = {s for a in self.accounts.values() for s in a.st["positions"].keys() | a.st["pending"].keys()}
            self.extra = {s for s in self.extra if s in busy} | set(C.EXTRA_SYMBOLS)  # islemi biten radar coinleri birakilir
            self.market = {s: r for s, r in self.market.items() if s in self.symbols or s in busy or s in self.extra}
            self.last_scan = {"t": time.time(), "regime": self.regime, "breadth": breadth, "mkt_r24": mkt_r24,
                              "btc_r1": float(btc.r1.iloc[0]), "btc_r24": float(btc.r24.iloc[0])}
        if not startup:
            new = not os.path.exists(C.JOURNAL_FILE)
            with open(C.JOURNAL_FILE, "a", encoding="utf-8") as fh:
                fh.write(("bar,sym,p,ev\n" if new else "") + "".join(x + "\n" for x in journal))
        if startup:
            self.say(f"İlk tarama tamam: {len(ok)} coin, rejim {M.REGIME_TR[self.regime]}, genişlik %{breadth * 100:.0f}.")

    # ---------------- Claude karari ----------------
    def triggers(self):
        """Dakikalik, ucretsiz: Claude'u saat basini beklemeden cagirmaya deger olay var mi?"""
        cool = 60 if C.QUOTA_SAVER else C.DECISION_COOLDOWN_MIN * 60  # yerel motor ucretsiz: olaya hizli tepki
        if time.time() - self.ag["last"] < cool:
            return []
        if 3600 - time.time() % 3600 < 300:  # saat basi karari zaten yakinda
            return []
        ev, held = [], {s for a in self.accounts.values() for s in a.st["positions"]}
        for s in self.symbols:
            te = self.tech.data.get(s) or {}
            h1, m15, h4 = ((te.get(k) or {}).get("rec") for k in ("1h", "15m", "4h"))
            if h1 is None or m15 is None or h4 is None:
                continue
            strong = h1 >= 0.5 and m15 >= 0.1 and h4 > 0
            if strong and self._tv_prev.get(s) is False and s not in held:
                ev.append(f"{s}: 1 saatlik teknik ozet GUCLU AL'a dondu (15dk {m15:+.2f}, 4s {h4:+.2f})")
            self._tv_prev[s] = strong
        hot = set()  # tum piyasa radari: yeni hacim patlamasi + BTC'den ayrisma + guclu trend
        for o in self.tech.opps[:15] if self.tech.live(400) else []:
            te = o.get("tech") or {}
            if o.get("full") and (te.get("1h") or 0) >= 0.5 and (te.get("4h") or 0) >= 0.1 \
                    and (o.get("vspike") or 0) >= 3 and (o.get("rs24") or 0) >= 5:
                hot.add(o["sym"])
                if o["sym"] not in self._radar_prev and o["sym"] not in held:
                    ev.append(f"RADAR: {o['sym']} hacim {o['vspike']:.1f}x, BTC'ye gore %{o['rs24']:+.1f}, "
                              f"1s teknik {te['1h']:+.2f}" + (", 24s zirvesini kirdi" if o.get("breakout") else ""))
                    if o.get("breakout") and self._radar_prev:  # ilk turda hepsi "yeni" sayilmasin
                        self.say(f"{o['sym']} güçlü kırılım: 24 saatlik zirve kırıldı, hacim {o['vspike']:.1f}x, BTC'ye "
                                 f"göre {o['rs24']:+.1f} puan, 24s %{o.get('chg24') or 0:+.1f}, 1s/4s teknik "
                                 f"{te['1h']:+.2f}/{te.get('4h') or 0:+.2f}, fiyat {o.get('price')}.", kind="radar")
        self._radar_prev = hot
        for a in self.accounts.values():
            for s, pos in a.st["positions"].items():
                bid, ref = self.bid.get(s), self._ref_px.get(s, pos["entry"])
                if bid and abs(bid - ref) > 1.5 * pos["atr"]:
                    ev.append(f"{s}: pozisyonda sert hareket (son karardan beri %{(bid / ref - 1) * 100:+.1f})")
        btc, now = self.bid.get(C.BTC), time.time()
        if btc:  # dakikalik ornekler: son 15 dakikadaki BTC hareketi
            self._btc_hist = [x for x in self._btc_hist if now - x[0] < 16 * 60] + [(now, btc)]
            old = self._btc_hist[0]
            if now - old[0] > 12 * 60 and abs(btc / old[1] - 1) > 0.015:
                ev.append(f"BTC 15 dakikada %{(btc / old[1] - 1) * 100:+.1f} hareket etti")
                self._btc_hist = [(now, btc)]
        if ev and all(a.blocked() for a in self.accounts.values()) and not held:
            return []  # hicbir hesap islem acamiyorsa ve pozisyon yoksa Claude'a sormak bosa para
        return sorted(set(ev))

    @staticmethod
    def conviction_stats(trades):
        """Sonuclanmis islemlerin guven seviyesine gore isabeti: Claude kendi kalibrasyonunu gorur."""
        out = []
        for lo, hi, name in ((0.7, 9, "yuksek (>=0.7)"), (0.5, 0.7, "orta (0.5-0.7)"), (-9, 0.5, "dusuk (<0.5)")):
            xs = [t["pct"] for t in trades if t.get("conviction") is not None and lo <= t["conviction"] < hi]
            if xs:
                out.append(f"{name}: {len(xs)} islem, kazanma %{sum(x > 0 for x in xs) / len(xs) * 100:.0f}, "
                           f"ort %{sum(xs) / len(xs):+.2f}")
        return "; ".join(out)

    def brief(self, kind, events):
        """Claude'a giden kompakt piyasa ozeti (yaklasik 3-5 bin token). Sabit kurallar sistem isteminde (onbellekte)."""
        f = lambda v, d=2: "-" if v is None or v != v else f"{v:.{d}f}"
        pc = lambda v, d=1: "-" if v is None or v != v else f"{v * 100:+.{d}f}"
        acct = self.ref()
        st, eq = acct.st, acct.equity()
        sc, ctx, (mult, mwhy) = self.last_scan or {}, self.ctx(), acct.risk_mult()
        rows = self.rows()
        out = [f"ZAMAN: {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC | KARAR TURU: {kind}"]
        if events:
            out += ["OLAYLAR:"] + [f"- {e}" for e in events]
        why = acct.blocked()
        out.append(f"\nHESAP ({acct.mode}): kasa {eq:.2f} USDT, bos nakit {acct.usdt_free if acct.live else acct.free_cash():.2f}, "
                 f"bugun %{acct.day_pct():+.2f}, acik {len(st['positions'])}/{C.MAX_POSITIONS}, "
                 f"toplam {st['wins'] + st['losses']} islem net {st['pnl']:+.2f} USDT. "
                 f"Sinirlar: islem basi risk <= %{C.RISK_PCT:g}, tek pozisyon <= kasanin %{C.MAX_POS_PCT:g}'i, "
                 f"gunluk zarar limiti %{C.DAILY_LOSS_PCT:g}, ayni coine cikistan sonra {C.COOLDOWN_MIN} dk bekleme. "
                 f"Risk yoneticisi su alislari reddeder: 24s hacim < {C.MIN_QUOTE_VOL / 1e6:g}M$, spread > "
                 f"%{C.MAX_SPREAD_PCT:g}, fiyatin %1 bandinda alici payi < %{C.OB_MIN_IMBALANCE * 100:.0f}, stop "
                 f"girisin %0,3'unden yakin veya %15'inden uzak, tp2 odul/risk < 1.2, fiyat karar suresince "
                 f"max(0.4 ATR, %0,5)'ten fazla kacmissa. "
                 f"Yeni islem: {'ACIK' if not why else 'KAPALI - ' + why}.")
        out.append(f"KARTOPU KASA: anapara {st.get('principal') or eq:.2f}, zirve {st.get('peak') or eq:.2f}, risk carpani "
                   f"x{mult} ({mwhy}). Gercek islem riski = guncel kasa x senin risk_pct x carpan (ust sinir %{C.RISK_PCT:g}).")
        out.append(f"SIFIR ZARAR KALKANI (yerel, otomatik): fiyat ilk riskin {C.BE_TRIGGER_R:g}R'si kadar kara gecince stop "
                   f"komisyon dahil basabasin ustune cekilir; sonra zirveden {C.TRAIL_ATR:g} ATR geride, 1R'den sonra kazancin "
                   f"en az %{C.LOCK_FRAC * 100:.0f}'i kilitli izler. Bunun icin adjust gondermen gerekmez.")
        out.append("ACIK POZISYONLAR:" if st["positions"] or st["pending"] else "ACIK POZISYON YOK.")
        for s, p in st["positions"].items():
            bid = self.bid.get(s, p["entry"])
            tp1 = "alindi" if p["part"] else (f"{p['partial_at']:.6g}" if p.get("partial_at") else "-")
            shield = "MANUEL KILIT - kullanici yonetiyor, close/adjust gonderme" if p.get("manual") else (
                "iz suruyor (kar kilitli)" if p["sl"] > L.be_price(p["entry"], p.get("atr")) * 1.001 else (
                    "basabasta" if p.get("be") else "henuz yok"))
            out.append(f"- {s}: giris {p['entry']:.6g}, simdi {bid:.6g} (%{(bid / p['entry'] - 1) * 100:+.2f}), "
                       f"stop {p['sl']:.6g} (kalkan: {shield}), tp1 {tp1}, tp2 {p['tp']:.6g}, "
                       f"{(time.time() - p['t']) / 3600:.1f}/{p.get('max_hours', C.MAX_BARS):g} sa. "
                       f"Gerekce: {p.get('why', '')[:160]}")
        for s, p in st["pending"].items():
            out.append(f"- {s}: bekleyen pasif alis {p['price']:.6g} ({(time.time() - p['t']) / 60:.0f} dk)")
        cd = [s for s in rows if acct.locked(s)]
        if cd:
            out.append("SU AN ALINAMAZ (bekleme suresi / zarar kilidi): " + ", ".join(sorted(cd)))
        rj = [r for r in self.ag["rejects"] if time.time() - r["t"] < 6 * 3600][-6:]
        if rj:
            out.append("RISK YONETICISININ REDDETTIGI SON EMIRLERIN: " + "; ".join(f"{r['sym']}: {r['why']}" for r in rj))
        out.append(f"\nPIYASA: rejim {M.REGIME_TR.get(sc.get('regime'), '-')}, BTC 1s %{pc(sc.get('btc_r1'), 2)} 24s "
                   f"%{pc(sc.get('btc_r24'))}, genislik (50 EMA ustu) %{f((sc.get('breadth') or 0) * 100, 0)}, "
                   f"ortalama 24s %{pc(sc.get('mkt_r24'))}")
        mk = self.macro.brief_line()
        if mk:
            out.append(mk)
        out += self.chain.brief_line([C.BTC, "ETH/USDT"] + [o["sym"] for o in self.tech.opps[:12]] + list(st["positions"]))
        edge = lambda r: f(L.edge(L.signals(r, ctx), ctx.get("regime")))
        out.append("COINLER (sym | fiyat | 24s% | son 1s mum% | ATR% | RSI | EMA50/200 uzaklik% | teknik ozet "
                   "15dk/1s/4s/1g [-1 guclu sat..+1 guclu al] | alici akisi 24s | fonlama% | OI 24s% | "
                   "defter alici% (ilk 20 kademe) / spread% | hacim M$ | ML p | ders R):")
        self._brief_px = {}
        for r in sorted(self.market.values(), key=lambda r: -(r.get("qvol") or 0)):
            te = r.get("tech") or {}
            self._brief_px[r["sym"]] = r.get("price")
            out.append(f"{r['sym'].split('/')[0]} | {r.get('price') or '-'} | {f(r.get('chg24'), 1)} | {pc(r.get('r1'), 2)} | "
                       f"{pc(r.get('atr_pct'), 2)} | {f(r.get('rsi'), 0)} | {pc(r.get('ema50'))}/{pc(r.get('ema200'))} | "
                       + "/".join(f(te.get(k)) for k in ("15m", "1h", "4h", "1d")) +
                       f" | {f(r.get('flow24'))} | {f((r.get('funding') or 0) * 100, 4)} | {pc(r.get('oi24'))} | "
                       f"{f((r.get('ob') or 0) * 100, 0)}/{f(r.get('spread'), 3)} | {f((r.get('qvol') or 0) / 1e6, 0)} | "
                       f"{f(r.get('p'))} | {edge(rows.get(r['sym'], r))}")
        radar = [o for o in self.tech.opps if o["sym"] not in self.market][:15]
        if radar:
            out.append(f"\nRADAR (tum Binance USDT: {self.tech.n_all} parite tarandi, {self.tech.n_liquid} likit; ML/vadeli "
                       "verisi yok, emir defteri alista olculur) sym | fiyat | 24s% | BTC'ye gore puan | hacim patlamasi x "
                       "(son 3 saat / medyan) | 4s% | 24s zirve kirilimi | teknik 15dk/1s/4s/1g | RSI | ATR% | hacim M$ | "
                       "sinyaller | ders R:")
            for o in radar:
                te, r = o.get("tech") or {}, rows.get(o["sym"], o)
                self._brief_px[o["sym"]] = o.get("price")
                out.append(f"{o['sym'].split('/')[0]} | {o.get('price')} | {f(o.get('chg24'), 1)} | {f(o.get('rs24'), 1)} | "
                           f"{f(o.get('vspike'), 1)} | {f(o.get('r4h'), 1)} | {'evet' if o.get('breakout') else '-'} | "
                           + "/".join(f(te.get(k)) for k in ("15m", "1h", "4h", "1d")) +
                           f" | {f(o.get('rsi'), 0)} | {pc(o.get('atr_pct'), 2)} | {f((o.get('qvol') or 0) / 1e6, 0)} | "
                           f"{','.join(sorted(L.signals(r, ctx))) or '-'} | {edge(r)}")
        dl = L.brief_lines()
        if dl:
            out.append("\nDERS DEFTERI (kendi otopsilerinden ogrendiklerin; tuzaklari risk yoneticisi zorunlu uygular):")
            out += dl
        gs = G.RES.get("kinds") or {}
        if gs:  # 2 yillik Gecmis Okulu: hangi kurulum hangi rejimde kazandirdi (test donemi, maliyet sonrasi)
            out.append("\nGECMIS OKULU (2 yil, tum coinler, komisyon+kayma dahil; test = gorulmemis son 1/3 donem; rejim izni "
                       "olmayan kurulumu risk yoneticisi reddeder):")
            for k, v in gs.items():
                out.append(f"- {k}: test {v['test'].get('avg', 0):+.2f}R ({v['test'].get('n', 0)} ornek, kazanma "
                           f"%{(v['test'].get('win') or 0) * 100:.0f}); rejim izni " + ", ".join(
                               f"{g} {'acik' if x['allowed'] else 'KAPALI'}" for g, x in v["by_regime"].items()))
            tr_ = [f"{'+'.join(r['sigs'])} ({r['test']['avg']:+.2f}R)" for r in G.traps()[:8]]
            if tr_:
                out.append("- 2 yillik tuzaklar (otomatik veto): " + "; ".join(tr_))
        cands = [(o["sym"], L.signals(rows.get(o["sym"], o), ctx), ctx.get("regime")) for o in radar[:8]] + \
                [(s, L.signals(r, ctx), ctx.get("regime")) for s, r in list(rows.items())[:4]]
        hl = HF.brief_lines(cands)
        if hl:
            out.append("")
            out += hl
        tr = st["trades"]
        if tr:
            out.append("\nSON ISLEMLERIN SONUCU (R = risk birimi; kazanma orani degil BEKLENTI onemli):")
            for t in tr[-8:]:
                out.append(f"- {t['sym']} %{t['pct']:+.2f}" + (f" {t['R']:+.2f}R" if t.get("R") is not None else "")
                           + f" ({t['reason']}, {(t['t'] - t['t_open']) / 3600:.1f} sa, "
                           f"guven {f(t.get('conviction'))}). Giris gerekcesi: {t.get('why', '')[:140]}")
            cs = self.conviction_stats(tr[-100:])
            if cs:
                out.append(f"GUVEN KALIBRASYONUN (son 100 islem): {cs}")
        if self.ag.get("memo"):
            out.append(f"\nSENIN STRATEJI NOTUN: {self.ag['memo']}")
        if C.AGENT_NOTES.strip():
            out.append(f"\nADMIN TALIMATI (uy): {C.AGENT_NOTES.strip()[:1000]}")
        return "\n".join(out)

    def decide(self, kind, events=()):
        """Claude'a sor, kararlari risk yoneticisinden gecirip hesaplara uygula. Butce ve hatalar burada yonetilir."""
        if self.ag["day"] != today():
            self.ag.update(day=today(), spent=0.0, calls=0, chat_spent=0.0)
        brain = A.brain()  # Claude API > Claude CLI (abonelik) > yerel quant motoru: bot hicbir zaman durmaz
        if brain == "api" and self.ag["spent"] >= C.CLAUDE_DAILY_BUDGET:
            if "bütçe" not in self.ag["error"]:
                self.say(f"Günlük Claude API bütçesi ({C.CLAUDE_DAILY_BUDGET:g} $) doldu: yarına kadar ücretsiz beyinle "
                         "devam ediliyor.", True, "uyari")
            self.ag["error"] = "Günlük Claude API bütçesi doldu"
            brain = "cli" if A.cli_ready() else "local"
        if C.QUOTA_SAVER and kind not in ("elle", "strateji"):
            brain = "local"  # kota koruma: tarama/olay/saatlik kararlar ucretsiz yerel motorda
        review = C.REVIEW_MODEL if kind == "strateji" else None
        held = any(a.st["positions"] for a in self.accounts.values())
        blocked = [a.blocked() for a in self.accounts.values()]
        if all(blocked) and not held:  # hicbir hesap islem acamaz, yonetilecek pozisyon yok: bosa cagri olmasin
            self.ag["error"] = "Karar atlandı: " + blocked[0]
            return
        self.status = {"api": "Claude karar veriyor", "cli": "Claude (CLI) karar veriyor"}.get(brain, "Yerel motor karar veriyor")
        t0, usd, model = time.time(), 0.0, A.BRAIN_TR["local"]
        try:
            if brain == "local":
                with self.lock:
                    self._brief_px = {}  # aninda karar: bayatlama kontrolu gerekmez
                    dec = Q.decide(self)
                model = "Yerel Quant Motoru"
            else:
                with self.lock:
                    brief = self.brief(kind, events)
                dec, usd, model = (A.decide if brain == "api" else A.cli_decide)(brief, review)
        except A.AgentError as e:
            self.ag["spent"] += e.usd
            self.ag["error"] = str(e)
            self.say(f"Claude kararı alınamadı ({e}); bu karar yerel quant motoruyla veriliyor.", kind="uyari")
            with self.lock:
                self._brief_px = {}
                dec = Q.decide(self)
            model = "Yerel Quant Motoru (yedek)"
        with self.lock:
            self.ag.update(spent=self.ag["spent"] + usd, total=self.ag.get("total", 0) + usd, calls=self.ag["calls"] + 1,
                           last=time.time(), error="")
            if kind == "olay":
                self.ag["last_event"] = time.time()
            if dec.get("memo"):
                self.ag["memo"] = dec["memo"][:800]
        if self.stop.is_set():  # motor bu sirada durduruldu: emir verme
            return
        results = [self.apply(a) for a in dec.get("actions", [])[:10]]  # ag cagrilari kilit disinda
        why = dec.get("_why") or {}
        self.log_candidates(results, why)
        with self.lock:
            self._ref_px = {s: self.bid[s] for a in self.accounts.values() for s in a.st["positions"] if s in self.bid}
        # olay coinleri (GUCLU AL, radar, balina) ve radarin en guclu 5'i: neden alinmadi (karar kartinda gorunur)
        ev_syms = list(dict.fromkeys(re.findall(r"[A-Z0-9]{2,20}/USDT", " ".join(events))))
        top = [o["sym"] for o in self.tech.opps[:5]]
        skipped = [{"sym": s, "why": why[s][:220]} for s in dict.fromkeys(ev_syms + top) if s in why][:8]
        self._publish(kind, events, dec.get("market_view", ""), results, usd, model, t0, skipped)

    def _publish(self, kind, events, view, results, usd, model, t0, skipped=()):
        """Karar kaydi + gunluk notu + trading_memory.md + Telegram komite karti (karar ve portfoy ortak)."""
        with self.lock:
            rec = {"t": time.time(), "kind": kind, "events": list(events), "view": view, "actions": results,
                   "usd": round(usd, 4), "model": model, "sec": round(time.time() - t0, 2), "skipped": list(skipped)}
            self.ag["decisions"] = (self.ag["decisions"] + [rec])[-200:]
        head = {"saatlik": "Saatlik karar", "olay": "Olay kararı", "baslangic": "İlk karar", "elle": "Elle istenen karar",
                "tarama": "Tarama kararı", "strateji": "Strateji incelemesi", "portfoy": "Portföy stratejisi"}.get(kind, "Karar")
        txt = f"{head} ({model}, {rec['sec']:.1f} sn): {view}"
        if events:
            txt += "\nTetik: " + "; ".join(events)
        for r in results:
            txt += f"\n• {r['label']}: {r['reason']} -> {r['result']}"
        if not results:
            txt += "\nYeni işlem yok."
        self.say(txt, kind="karar" if results else "bekleme", tg=kind == "portfoy")  # digerleri: komite karti ayrica
        try:
            HF.record_decision(rec)  # trading_memory.md
        except OSError as e:
            log.warning("hafiza: %s", e)
        approved = any((r.get("card") or {}).get("verdict") == "ONAY" for r in results)
        if results and C.TELEGRAM_TOKEN and (approved or kind in ("elle", "strateji")):  # gurultu yok
            import telegram_bot
            threading.Thread(target=telegram_bot.notify_decision, args=(rec,), daemon=True).start()
        return rec

    def manual_buy(self, mode, s, usdt, stop, tp=None, entry="taker", who="kullanıcı"):
        """Grafikten elle alim: kullanicinin tutari ve stopu. Pozisyon Manuel Kilitle acilir (AI dokunmaz).
        Gercek hesapta admin'in gercek islem izni sart. Hata metni veya None."""
        acct, s = self.accounts.get(mode), norm(s)
        if not acct:
            return "Hesap bağlı değil."
        if acct.live and (not C.LIVE_ALLOWED or not acct.synced or acct.error):
            return acct.error or "Gerçek hesapta işlem için admin izni (Ayarlar → Gerçek işlem) gerekli."
        m = self.ex.markets.get(s)
        if not m or not m.get("active") or m.get("quote") != "USDT" or s.split("/")[0] in D.STABLE:
            return "Binance'te aktif spot USDT paritesi değil."
        if s in acct.st["positions"] or s in acct.st["pending"]:
            return "Bu coinde zaten pozisyon/emir var; stop ve hedefi grafikteki panelden değiştirin."
        if s not in self.bid:
            t = self.ex.fetch_ticker(s)
            with self.lock:
                self.bid[s], self.ask[s] = t["bid"], t["ask"]
                self.market.setdefault(s, {"sym": s}).update(price=t["last"], chg24=t.get("percentage"),
                                                             qvol=t.get("quoteVolume"), px_t=time.time())
                self.extra.add(s)
        price = self.ask[s] if entry == "taker" else self.bid[s]
        if not 0 < stop < price * 0.997:
            return f"Stop fiyatın en az %0,3 altında olmalı (fiyat {price:g})."
        if tp is not None and tp <= price * 1.003:
            return f"Hedef fiyatın üstünde olmalı (fiyat {price:g})."
        if not usdt or usdt <= 0:
            return "Tutar (USDT) girin."
        row = self.market.get(s) or {}
        plan = {"stop": stop, "tp1": 0.0, "tp2": tp or price * 100, "risk_pct": C.RISK_PCT, "max_hours": 24 * 365,
                "entry": "taker" if entry == "taker" else "maker", "conviction": None, "atr_pct": row.get("atr_pct") or 0.01,
                "reason": f"Elle açıldı ({who}): stop ve hedef kullanıcıda (Manuel Kilit).", "p": None, "usdt": float(usdt),
                "manual": True}
        with self.lock:
            err = acct.enter(s, plan)
        if not err:
            self.say(f"{s} elle alış ({who}, {'gerçek' if acct.live else 'sanal'} hesap): {usdt:g} USDT, stop {stop:g}"
                     + (f", hedef {tp:g}" if tp else ", hedef yok") + ".", kind="islem", acct=mode)
        return err

    def observe(self):
        """Saatlik gozlem gunlugu (gozlem.csv): gercek paraya gecis karari icin kasa, K/Z, BTC'ye gore seyir,
        dusus, pozisyonlar, son saatteki uyarilar. Yerel, ucretsiz; satir basina bir hesap."""
        import csv
        fn = os.path.join(C.APP_DIR, "gozlem.csv")
        new = not os.path.exists(fn)
        now = time.time()
        with self.lock:
            warns = [n["text"][:120] for n in self.notes if n["kind"] == "uyari" and now - n["t"] < 3600]
            rows = []
            for m, a in self.accounts.items():
                st, eq = a.st, a.equity()
                pos = {s: round(p["qty"] * self.bid.get(s, p["entry"]), 2) for s, p in st["positions"].items()}
                rows.append([time.strftime("%Y-%m-%d %H:%M", time.localtime(now)), m, round(eq, 2),
                             round(a.usdt_total if a.live else st["cash"], 2), round(sum(pos.values()), 2),
                             round(st["pnl"], 2), len(st["trades"]), round(self.bid.get(C.BTC) or 0, 2),
                             round(st.get("peak") or eq, 2), round((1 - eq / (st.get("peak") or eq)) * 100, 2),
                             round(a.day_pct(), 2), a.blocked(portfolio=True) or "",
                             time.strftime("%m-%d %H:%M", time.localtime(self.ag["pf"]["last"]))
                             if (self.ag.get("pf") or {}).get("last") else "-",
                             json.dumps(pos, ensure_ascii=False), " | ".join(warns)])
        with open(fn, "a", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            if new:
                w.writerow(["zaman", "hesap", "kasa_net", "nakit", "coinlerde", "kapanan_kz", "islem_sayisi", "btc",
                            "zirve", "zirveden_dusus_%", "gun_%", "engel", "son_dengeleme", "pozisyonlar", "uyarilar"])
            w.writerows(rows)

    def run_portfolio(self):
        """Portfoy dengelemesi kendi is parcaciginda: ML egitimi / uzun karar islerini beklemez. Basladiysa True."""
        if getattr(self, "_pf_thread", None) and self._pf_thread.is_alive():
            return False

        def run():
            try:
                self.portfolio()
            except (ccxt.NetworkError, requests.RequestException) as e:
                log.warning("portfoy ag hatasi: %s", e)
                self.ag["pf_d"] = ""  # ag hatasi: dongu tekrar dener
            except Exception as e:
                log.exception("portfoy")
                self.say(f"Portföy stratejisi hatası: {e}", kind="uyari")
        self._pf_thread = threading.Thread(target=run, name="portfoy", daemon=True)
        self._pf_thread.start()
        return True

    def portfolio(self, force_ml=False):
        """Portfoy stratejisi (portfoy.py): gun kapanisinda cekirdek agirliklari, Pazartesi (veya ilk calisma) ML sepeti.
        Sanal hesap her zaman; gercek hesap sadece admin 'PORTFOY_LIVE' + gercek islem izni verdiyse."""
        t0 = time.time()
        pf = self.ag.setdefault("pf", {})
        week = time.strftime("%G-%V", time.gmtime())
        ml_day = force_ml or pf.get("ml_week") != week  # haftada bir (Pazartesi ya da hafta icindeki ilk calisma)
        tg, info = PF.targets(self.ex, pf, ml_day)
        if ml_day:
            pf["ml_week"] = week
        for s in tg:  # stream'de olmayan coin: anlik fiyat REST ile
            if not self.bid.get(s) or not self.ask.get(s):
                t = self.ex.fetch_ticker(s)
                with self.lock:
                    self.bid[s], self.ask[s] = t["bid"] or t["last"], t["ask"] or t["last"]
        accts = [a for m, a in self.accounts.items() if m == "PAPER" or (C.PORTFOY_LIVE and C.LIVE_ALLOWED and a.synced)]
        word = lambda f: "güçlü" if f >= 0.75 else "orta" if f >= 0.5 else "zayıf" if f > 0 else "yok"
        lines, res, usd, model, ozet, cnote = [], {}, 0.0, "yerel", "", ""
        try:  # ogrenme: gecmis itirazlari ve haftalik secimleri puanla (7 gun sonra)
            KT.score(self.ex)
            pf["ml_card"] = PF.ml_card(self.ex, pf)
        except Exception as e:
            log.warning("karne: %s", e)
        buys = self._pf_buys(tg, info, ml_day)
        if buys and A.brain() != "local":  # dusunerek alim: Claude kontrolor (sadece ertele / yariya indir)
            try:
                ozet, objs, cnote, usd = KT.review(A.ask_json, buys, self._pf_context(info))
                self.add_cost(usd)
                model = "Claude kontrolör"
                cur = {s: c for s, _, c, _, _ in buys}
                for s, k in objs.items():
                    tg[s] = cur[s] if k == "ertele" else cur[s] + (tg[s] - cur[s]) / 2
            except A.AgentError as e:
                cnote = f"Claude'a ulaşılamadı ({e}); plan kuralla uygulandı."
        for a in accts:
            try:
                res[a.mode] = a.rebalance(tg, ml_day)
            except ccxt.BaseError as e:
                res[a.mode] = [f"borsa hatası: {str(e)[:120]}"]
        ml = info["ml"]
        for s, v in info["core"].items():
            lines.append(f"{s.split('/')[0]}: yükseliş gücü {word(v['trend'])} → kasadaki payı %{v['w'] * 100:.0f}")
        picks = ", ".join(s.split("/")[0] for s, _ in ml["picks"]) or "-"
        lines.append("Haftalık sepet: " + ("dinlenmede (model son 6 ayda isabet kaybetti, pay nakitte)" if ml.get("rest") else
                                          f"{picks}" if ml["reg"] else "piyasa zayıf, sepet nakitte"))
        lines.append(f"Nakitte bekleyen: %{max(0.0, 1 - sum(tg.values())) * 100:.0f}")
        if ozet:
            lines.insert(0, ozet)
        if cnote:
            lines.append("Kontrolör: " + cnote)
        pf.update(last=time.time(), targets=tg, info={"core": info["core"]}, result=res, ozet=ozet, kontrol=cnote)
        results = [{"type": "rebalance", "sym": "PORTFÖY", "label": "Yeniden dengeleme", "reason": "hedef ağırlıklar",
                    "result": "; ".join(f"{'sanal' if m == 'PAPER' else 'gerçek'}: " + " / ".join(r) for m, r in res.items())}]
        self._publish("portfoy", [], " | ".join(lines), results, usd, model, t0)
        PF.maybe_train(lambda t: self.say(t, kind="oz"))

    def _pf_buys(self, tg, info, ml_day):
        """Bugun alinacaklar (sanal hesaba gore, Account.rebalance ile ayni esik): [(sym, hedef, simdiki, fiyat, aciklama)]."""
        band = PF.BAND_ML if ml_day else PF.BAND
        a = self.accounts["PAPER"]
        with self.lock:
            held = {s: p["qty"] * self.bid.get(s, p["entry"]) for s, p in a.st["positions"].items() if p.get("kind") == PF.KIND}
            base = a.st["cash"] + sum(held.values())
        out = []
        for s, t in tg.items():
            c = held.get(s, 0.0) / base if base > 0 else 0.0
            if (t - c >= band or (s not in held and t >= 0.01)) and self.ask.get(s):
                why = "çekirdek (trend)" if s in info["core"] else "haftalık ML seçimi"
                out.append((s, t, c, self.ask[s], why))
        return out

    def _pf_context(self, info):
        """Kontrolore sade piyasa ozeti: BTC/ETH ve alinacak coinlerin son 7/30 gunu, makro risk."""
        out = [f"Makro risk iştahı: {self.macro.risk()[0]}"]
        for s in sorted(set(info["core"]) | {x for x, _ in info["ml"]["picks"]}):
            try:
                c = PF.klines(self.ex, s, 95)["c"]
                out.append(f"{s}: son 7 gün %{(c.iloc[-1] / c.iloc[-8] - 1) * 100:+.1f}, son 30 gün "
                           f"%{(c.iloc[-1] / c.iloc[-31] - 1) * 100:+.1f}, 90 günlük zirveden %{(c.iloc[-1] / c.max() - 1) * 100:+.1f}")
            except Exception:
                continue
        return out

    def daily_trend(self):
        """Gunluk Trend Kolu (gunluk.py): UTC gun kapanisindan sonra once kolun pozisyonlarinda 10 gunluk dip cikisi,
        sonra en likit 30 coinde Donchian-20 kirilimiyla yeni girisler (komite + risk yoneticisi). Gercek hesap ancak
        sanal karne (30+ islem, ort R > 0) olusunca acilir."""
        t0 = time.time()
        self.ag["daily_d"] = time.strftime("%Y-%m-%d", time.gmtime())
        res = GT.scan(self.ex)
        events = []
        for acct in self.accounts.values():
            for s, pos in list(acct.st["positions"].items()):
                if pos.get("kind") != GT.KIND or pos.get("manual"):
                    continue
                d = res["rows"].get(s)
                if d is None:
                    try:
                        d = GT.daily(self.ex, s, 30)
                    except ccxt.BaseError:
                        continue
                if GT.exit_signal(d):
                    with self.lock:
                        if s in acct.st["positions"]:
                            acct.say(f"{s} günlük trend kolu: kapanış önceki {GT.N_EXIT} günün en düşük kapanışının "
                                     f"altında, trend bitti.", True, "islem")
                            acct.exit(s, pos, "GUNLUK_CIKIS")
                            events.append(f"ÇIKIŞ: {s}")
        tr = GT.track(self.accounts["PAPER"].st["trades"])
        view = (f"Günlük trend kolu: BTC günlük kapanış {res['btc_close']:.0f}, EMA50 {res['btc_ema50']:.0f} → "
                + ("rejim UYGUN" if res["regime_ok"] else "rejim UYGUN DEĞİL (yeni giriş yok)")
                + f". En likit {len(res['universe'])} coinde {len(res['buys'])} Donchian-20 kırılımı. Sanal karne: "
                  f"{tr['n']} işlem" + (f", ort {tr['avg_r']:+.2f}R" if tr["avg_r"] is not None else "")
                + (" — gerçek hesap açık." if tr["live_ok"] else f" (gerçek hesap {GT.LIVE_MIN_N} işlem ve pozitif "
                                                                   f"ortalamadan sonra açılır)."))
        results = []
        if res["regime_ok"]:
            for b in res["buys"][:C.MAX_POSITIONS]:
                px = self.ask.get(b["sym"]) or b["close"]
                if px > b["close"] * 1.02:  # gun icinde kacmis kirilim kovalanmaz (test kapanistan girdi)
                    continue
                results.append(self.apply({
                    "type": "buy", "symbol": b["sym"], "kind": GT.KIND, "entry": "taker", "stop": b["stop"],
                    "tp2": b["close"] * 100, "tp1": 0, "risk_pct": GT.RISK, "max_hours": GT.MAX_DAYS * 24,
                    "conviction": 0.5,
                    "reason": f"{b['sym'].split('/')[0]}: günlük kapanış {b['close']:.6g} önceki 20 günün zirvesini "
                              f"({b['hi20']:.6g}) kırdı; 20 günlük getiri %{b['roc20'] * 100:+.1f}. Stop 2 ATR "
                              f"({b['stop']:.6g}, %{(1 - b['stop'] / b['close']) * 100:.1f}); çıkış 10 günlük dip kapanışında.",
                    "bull_case": "Donchian-20 kırılımı; BTC günlük EMA50 üstünde (trend piyasası)",
                    "bear_case": "kazanma oranı ~%30: çoğu işlem küçük zararla kapanır, kâr birkaç büyük trendden gelir"}))
        events += [f"KIRILIM: {b['sym']}" for b in res["buys"]]
        self._publish("gunluk", events, view, results, 0.0, "Günlük Trend Kolu (yerel)", t0)

    def log_candidates(self, results, why):
        """Ders Defteri: karar anindaki en guclu 25 adayi ornek olarak kaydet; 24 saat sonra sonucu olculur
        (alinmayip sert yukselen = kacan firsat, alinip zarar eden = tuzak adayi)."""
        from teknik import radar_score
        try:
            with self.lock:
                rows, ctx = self.rows(), self.ctx()
                busy = {s for a in self.accounts.values() for s in a.st["positions"].keys() | a.st["pending"].keys()}
            acted = {r["sym"] for r in results if r["type"] == "buy" and "emir verildi" in r.get("result", "")}
            why = {**why, **{r["sym"]: r["result"] for r in results if r["type"] == "buy"}}
            # alinan/eldeki coinler radar ornegi olmaz: onlarin dersi gercek otopsiden gelir (cift sayilmasin)
            cands = sorted(({**r, "score": radar_score(r)} for s, r in rows.items() if s not in busy),
                           key=lambda r: -r["score"])[:25]
            L.log_samples(cands, ctx, acted, {r["sym"]: why.get(r["sym"], "karar beyni seçmedi") for r in cands})
        except Exception as e:  # ogrenme kaydi karar akisini bozmasin
            log.warning("ders defteri ornek kaydi: %s", e)

    def apply(self, a):
        """Tek Claude emrini uygular; sonucu kayit icin dondurur."""
        s, t = norm(a.get("symbol")), a.get("type")
        out = {"type": t, "sym": s, "reason": str(a.get("reason", ""))[:400], "label": f"{str(t).upper()} {s}",
               "conviction": a.get("conviction"), "bull": str(a.get("bull_case", ""))[:300],
               "bear": str(a.get("bear_case", ""))[:300]}
        try:
            if not re.fullmatch(r"[A-Z0-9]{1,20}/USDT", s):
                out["result"] = "geçersiz sembol"
            elif t == "buy":
                plan = self.vet_buy(s, a)  # ag cagrilari (defter, fiyat) kilit disinda
                if isinstance(plan, str):
                    out["result"] = "risk yöneticisi reddetti: " + plan
                else:
                    out["plan"] = {k: plan[k] for k in ("stop", "tp1", "tp2", "risk_pct", "max_hours", "entry", "liq",
                                                        "kind")}
                    if plan["liq"]:
                        out["reason"] += f" Likidasyon haritası: {plan['liq']}."
                    res = []
                    for m, acct in self.accounts.items():
                        try:
                            with self.lock:
                                why = acct.blocked() or (
                                    "zaten pozisyonda" if s in acct.st["positions"] or s in acct.st["pending"] else None
                                ) or acct.locked(s) or (
                                    "gerçek hesapta sadece 2 yıllık testte kanıtlanmış kurulum açılır"
                                    if acct.live and not plan.get("proven") else None) or acct.enter(s, plan)
                        except ccxt.BaseError as e:
                            why = f"borsa hatası: {str(e)[:120]}"
                        res.append(f"{'sanal' if m == 'PAPER' else 'gerçek'}: {'emir verildi' if not why else why}")
                    out["result"] = "; ".join(res)
            elif t == "close":
                with self.lock:
                    locked = [m for m, acct in self.accounts.items() if (acct.st["positions"].get(s) or {}).get("manual")
                              or (acct.st["positions"].get(s) or {}).get("kind") in (GT.KIND, PF.KIND)]
                    done = [m for m, acct in self.accounts.items() if m not in locked and acct.close(s, "CLAUDE_KAPATTI")]
                out["result"] = "kapatıldı" if done else (
                    "Manuel Kilit / strateji pozisyonu: kendi kuralıyla yönetiliyor, kapatılmadı" if locked
                    else "açık pozisyon yok")
            elif t == "adjust":
                with self.lock:
                    res = [acct.adjust(s, float(a.get("stop") or 0), float(a.get("tp2") or 0))
                           for acct in self.accounts.values() if s in acct.st["positions"]]
                out["result"] = "güncellendi" if res and None in res else (res[0] if res else "pozisyon yok")
            else:
                out["result"] = "-"
        except ccxt.BaseError as e:
            out["result"] = f"borsa hatası: {str(e)[:150]}"
        if t == "buy" and "emir verildi" not in out["result"]:
            self.ag["rejects"] = (self.ag["rejects"] + [{"t": time.time(), "sym": s, "why": out["result"][:160]}])[-20:]
        try:  # komite karti: analist notu, boga/ayi, risk onayi, Ders Defteri/hafiza notu
            row = a.get("_row") or self.market.get(s) or {}
            out["card"] = K.card(a, out, row, self.ctx(), a.get("_sigs") or set(), a.get("_checks") or [],
                                 a.get("_memory"), self.chain.whale.get(s), self.chain.liq.get(s), self.macro.risk())
        except Exception as e:  # kart karar akisini bozmasin
            log.warning("komite karti %s: %s", s, e)
        return out

    def vet_buy(self, s, a):
        """Risk yoneticisi (komitenin son asamasi): alis onerisini kasa ve piyasa kurallarina gore denetler. Plan veya
        red sebebi. Her kontrol a['_checks']'e (ad, gecti mi, ayrinti) yazilir: karar kartinda 'Risk Onayi'."""
        chk = a.setdefault("_checks", [])

        def veto(name, why):
            chk.append((name, False, why))
            return why

        def ok(name, detail):
            chk.append((name, True, detail))
        m = self.ex.markets.get(s)
        if not m or not m.get("spot") or not m.get("active") or m.get("quote") != "USDT" or s.split("/")[0] in D.STABLE:
            return veto("Parite", "Binance'te aktif spot USDT paritesi değil")
        if s not in self.bid:  # evren disi firsat: canli izlemeye al
            t = self.ex.fetch_ticker(s)
            self.bid[s], self.ask[s] = t["bid"], t["ask"]
            self.market.setdefault(s, {"sym": s}).update(price=t["last"], chg24=t.get("percentage"),
                                                         qvol=t.get("quoteVolume"), px_t=time.time())
            self.extra.add(s)
        with self.lock:
            row = self.market.setdefault(s, {"sym": s})
            for k, v in (self.rows().get(s) or {}).items():  # radar coini: teknik ozet, ATR, hacim patlamasi...
                if row.get(k) in (None, {}):
                    row[k] = v
            ctx = self.ctx()
        kind = a.get("kind") or Q.classify(row, ctx.get("regime"))  # Claude onerisi: hangi kuruluma benziyor
        a["kind"] = kind
        a["_row"], a["_sigs"] = dict(row), L.signals(row, ctx)
        mem = HF.recall(s, a["_sigs"], ctx.get("regime"), kind)  # erken: veto edilen kartta da hafiza notu olsun
        a["_memory"] = mem["note"]
        if (row.get("qvol") or 0) < C.MIN_QUOTE_VOL:
            return veto("Likidite", f"likidite düşük (24s hacim {(row.get('qvol') or 0) / 1e6:.1f}M$)")
        ok("Likidite", f"24s hacim {(row.get('qvol') or 0) / 1e6:.1f}M$ (pozisyon en fazla %{C.MAX_VOL_SHARE * 100:g}'si)")
        price, stop = self.ask[s] if a.get("entry") == "taker" else self.bid[s], float(a.get("stop") or 0)
        tick = (m.get("precision") or {}).get("price")
        if tick and tick < 1 and tick / price > 0.002:  # tek fiyat adimi stopu bozacak kadar buyuk
            return veto("Fiyat adımı", f"fiyat adımı çok kaba (%{tick / price * 100:.2f})")
        ref = None if kind == "balina" else self._brief_px.get(s)  # refleks anlik: ozet fiyati yok
        atr = row.get("atr_pct") or 0.01
        if ref and abs(price / ref - 1) > max(0.4 * atr, 0.005):  # karar suresince fiyat kactiysa bayat karar
            return veto("Güncellik", f"karar bayatladı: fiyat Claude'un gördüğü {ref:g} değerinden "
                                     f"%{(price / ref - 1) * 100:+.2f} uzaklaştı")
        if kind and kind != "elle":
            al, why = G.allowed(kind, ctx.get("regime"))
            if not al:
                return veto("Rejim uyumu", why)
            ok("Rejim uyumu", why or f"{ctx.get('regime')} rejimi")
        imb, spread = self.orderbook(s)
        row.update(ob1=round(imb, 3))
        if spread > C.MAX_SPREAD_PCT:
            return veto("Spread", f"spread geniş (%{spread:.2f})")
        if imb < C.OB_MIN_IMBALANCE:
            return veto("Emir defteri", f"emir defterinde satış baskısı (%1 bantta alıcı %{imb * 100:.0f})")
        ok("Emir defteri", f"alıcı %{imb * 100:.0f}, spread %{spread:.2f}")
        if not 0 < stop < price * 0.997:
            return veto("Stop", "stop fiyatın en az %0,3 altında olmalı")
        dist = (price - stop) / price
        daily_arm = kind == GT.KIND  # gunluk trend kolu: gunluk 2 ATR stop (%5-25), boyut riske gore kuculur
        if dist > (0.30 if daily_arm else 0.15):
            return veto("Stop", f"stop çok uzak (%{dist * 100:.1f})")
        cost_r = (2 * C.FEE + 2 * C.SLIPPAGE) / dist  # komisyon+kayma riskin ne kadari: dar stop = maliyet R'yi yer
        ks = G.kind_stat(kind, ctx.get("regime")) if kind else None
        proven = bool(ks and ks["allowed"] and ks["avg"] > 0)  # maliyet dahil 2 yillik testte pozitif kurulum
        if daily_arm:  # gercek hesap icin kanit: kolun kendi sanal karnesi (ileri test)
            proven = GT.track(self.accounts["PAPER"].st["trades"])["live_ok"]
        if cost_r > C.MAX_COST_R + 1e-9 and not proven:
            return veto("Maliyet", f"stop çok dar (%{dist * 100:.2f}): komisyon+kayma riskin %{cost_r * 100:.0f}'i "
                                   f"(üst sınır %{C.MAX_COST_R * 100:.0f})")
        ok("Maliyet", f"stop %{dist * 100:.2f}, komisyon+kayma {cost_r:.2f}R")
        tp2 = float(a.get("tp2") or 0) or price + 2 * (price - stop)
        tp1 = float(a.get("tp1") or 0)
        # likidasyon haritasi: stop igne avi kumesinin ustundeyse kume altina, hedef short-likidasyon miknatisina
        stop0, liq_note = stop, ""
        if not daily_arm:  # gunluk kol test edilen kurala sadik: likidasyon miknatisi hedef/stop eklemez
            stop, tp2, liq_note = self.chain.adjust(s, price, stop, tp2, (row.get("atr_pct") or 0.01) * price)
        if tp1 and stop < stop0:  # stop genisledi: kismi kar da ayni R'de kalsin
            tp1 = price + (tp1 - price) * (price - stop) / (price - stop0)
        rr = (tp2 - price) / (price - stop)
        if rr < 1.2:
            return veto("Ödül/risk", f"ödül/risk düşük ({rr:.2f})")
        ok("Ödül/risk", f"{rr:.2f} (stop {stop:.6g}, hedef {tp2:.6g})" + (f"; likidasyon: {liq_note}" if liq_note else ""))
        ex = dict(GT.EXIT) if daily_arm else G.exit_params(kind) if kind in G.KINDS else {}
        mc, mc_mult = None, 1.0
        try:  # Monte Carlo Zaman Makinesi: 10.000 yol, botun cikis kurallariyla
            hrs = min(float(a.get("max_hours") or 24), 72) if not ex.get("max_h") else min(ex["max_h"], 72)
            if daily_arm:  # gunluk kol: bilgi amacli (ufuk 90 gun, 72 saatlik simulasyon karar vermez)
                mc = MC.run(self.ex, s, price, stop, tp2, ex, hrs, ctx.get("regime"), row.get("vspike"),
                            a.get("entry") == "taker", partial_at=tp1 or None)
            else:
                s2, t2, mc, mc_note = MC.best_stop(self.ex, s, price, stop, tp2, ex, hrs, ctx.get("regime"),
                                                   row.get("vspike"), a.get("entry") == "taker",
                                                   (row.get("atr_pct") or 0) * price)
                if mc_note:  # ayni odul/risk ile daha iyi beklenen deger: stop/hedef bu noktaya sabitlenir
                    if tp1:
                        tp1 = price + (tp1 - price) * (price - s2) / (price - stop)
                    stop, tp2 = s2, t2
                    ok("Stop optimizasyonu", mc_note)
        except Exception as e:  # veri alinamazsa simulasyon atlanir (karar engellenmez)
            log.debug("monte carlo %s: %s", s, e)
        a["_mc"] = mc
        if mc and not daily_arm:
            why_mc = (f"stop önce %{mc['stop'] * 100:.0f}, hedef önce %{mc['tp'] * 100:.0f}, beklenen "
                      f"{mc['ev_r']:+.2f}R ({mc['n']} yol)")
            if mc["stop"] > 0.5 and mc["ev_r"] < 0:
                return veto("Monte Carlo", "Yüksek Risk / Düşük Olasılık: " + why_mc)
            if mc["stop"] > 0.5 or mc["ev_r"] < 0:
                mc_mult = 0.5
                ok("Monte Carlo", "UYARI Yüksek Risk / Düşük Olasılık, pozisyon yarıya indirildi: " + why_mc)
            else:
                ok("Monte Carlo", why_mc)
        elif mc:
            ok("Monte Carlo", f"bilgi (günlük kol, {mc['hours']} sa): stop önce %{mc['stop'] * 100:.0f}, "
                              f"beklenen {mc['ev_r']:+.2f}R")
        now_row = {**row, "ob": imb}
        sigs = L.signals(now_row, ctx)
        a["_sigs"] = sigs
        why, mult, edge = L.check(sigs, ctx.get("regime"))  # gecmiste zarar ettiren tuzak / benzer zararli islemler
        a["_edge"] = edge
        if why:
            return veto("Ders Defteri", why)
        ok("Ders Defteri", f"tuzak yok, öğrenilen avantaj {edge:+.2f}R")
        if mem["veto"]:
            return veto("Hafıza", mem["veto"])
        ok("Hafıza", mem["note"])
        pn = None
        if daily_arm and not proven:
            tr = GT.track(self.accounts["PAPER"].st["trades"])
            ok("Kanıt", f"günlük trend kolu sanal hesapta ileri testte ({tr['n']}/{GT.LIVE_MIN_N} işlem); gerçek hesap "
                        f"pozitif karneden sonra açılır")
        elif not proven:
            ok("Kanıt", "2 yıllık testte kanıtlanmamış kurulum: gerçek hesapta açılmaz, sanal hesapta yarım riskle denenir")
        ok("Sıfır zarar kalkanı", (f"başabaş {ex['be_r']:g}R'de, iz {ex['trail_atr']:g} ATR" if ex.get("be_r") else
                                   "başabaş/iz kalkanı") + (f", {ex['ts_h']} saatte ilerlemezse çıkış" if ex.get("ts_h") else ""))
        return {"stop": stop, "tp1": tp1 if price < tp1 < tp2 else 0.0, "tp2": tp2, "pn": pn,
                "risk_pct": min(max(float(a.get("risk_pct") or C.RISK_PCT / 2), 0.1) * mult * mc_mult
                                * (1.0 if proven or daily_arm else 0.5), C.RISK_PCT),  # kanitsiz fikir: yarim risk
                "mc": mc,
                "max_pos_pct": GT.MAX_POS_PCT if daily_arm else C.MAX_POS_PCT,
                "max_hours": min(max(float(a.get("max_hours") or 24), 1), 72),
                "entry": "taker" if a.get("entry") == "taker" else "maker", "conviction": a.get("conviction"),
                "reason": str(a.get("reason", ""))[:400] + (f" | Likidasyon: {liq_note}" if liq_note else ""),
                "liq": liq_note, "atr_pct": row.get("atr_pct"), "p": row.get("p"), "kind": kind, "exit": ex,
                "proven": proven,  # gercek hesap sadece bunu acar
                "sigs": sorted(sigs), "feats": L.feats(now_row, ctx), "edge": edge, "ders_mult": mult}


if __name__ == "__main__" and sys.argv[1:] == ["egit"]:  # gelistirici: python bot.py egit
    M.train(D.exchange(), print)
