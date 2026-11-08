"""USDS-M perpetual (Binance Futures) SANAL adaptoru: LONG/SHORT pozisyon modeli, risk, stop/hedef, fonlama, likidasyon.
SADECE PAPER: bu modulde gercek emir yolu yok; spot motoruna (bot.py) baglanmaz. Canliya gecis icin ayri borsa baglantisi,
gercek hesapta order/test ve kullanici izni gerekir (bkz. CANLI_ONCESI).
Varsayilan: One-way pozisyon modu, Isolated teminat, 1x kaldirac; kaldirac otomatik artmaz (MAX_LEV ustu reddedilir).
Her pozisyonun stopu zorunlu (fail-closed): stopsuz, stopu yanlis tarafta veya likidasyona stoptan once ulasan emir acilmaz.
Stop sadece lehe tasinir (LONG yukari, SHORT asagi). Ayni istemci kimligiyle (cid) ikinci emir reddedilir."""
import math
import time

FEE = 0.0005        # USDS-M taker (VIP0) tek yon
SLIPPAGE = 0.0005   # piyasa/stop dolumunda kayma
MMR = 0.004         # bakim teminati orani (kucuk pozisyon kademesi; gercekte sembol/kademe bazli)
MAX_LEV = 1         # otomatik artis yok; degistirmek ayri karar
LIQ_BUFFER = 0.5    # stop, girisle likidasyon arasinin en cok yarisinda olmali
CANLI_ONCESI = ("gercek hesapta positionSide=BOTH (one-way) ve marginType=ISOLATED dogrulamasi",
                "sembol filtreleri (tickSize, stepSize, minQty, MIN_NOTIONAL) borsadan okunmali",
                "STOP_MARKET + reduceOnly (veya closePosition=true, ikisi birlikte degil) order/test ile denenmeli",
                "bakim teminati kademeleri (leverageBracket) ile likidasyon fiyati borsanin hesabiyla karsilastirilmali",
                "pozisyon/emir mutabakati gercek hesapta kucuk tutarla denenmeli")


class Rules:
    """Sembol kisitlari. Gercekte borsanin exchangeInfo filtrelerinden; burada sanal varsayilan."""

    def __init__(self, tick=0.01, step=0.001, min_qty=0.001, min_notional=5.0):
        self.tick, self.step, self.min_qty, self.min_notional = tick, step, min_qty, min_notional

    def qty(self, q):
        return math.floor(q / self.step + 1e-9) * self.step  # asagi: teminattan fazlasi kullanilmasin

    def price(self, p):
        return round(round(p / self.tick) * self.tick, 12)


def liq_price(side, entry, lev, mmr=MMR):
    """Isolated, tek pozisyon: teminat = nominal/lev. LONG: entry*(1-1/lev+mmr), SHORT: entry*(1+1/lev-mmr)."""
    return entry * (1 - 1 / lev + mmr) if side == "long" else entry * (1 + 1 / lev - mmr)


class FuturesPaper:
    def __init__(self, balance=1000.0, fee=FEE, slip=SLIPPAGE, rules=None, clock=time.time):
        self.cash, self.fee, self.slip, self.clock = float(balance), fee, slip, clock
        self.rules = rules or {}
        self.pos, self.trades, self.cids, self.log = {}, [], set(), []

    def rule(self, s):
        return self.rules.get(s) or Rules()

    # ---------------- acilis ----------------
    def open(self, s, side, qty, price, sl, tp=None, lev=1, cid=None, fill=1.0):
        """Piyasa girisi. fill<1: kismi dolum (sadece dolan miktar pozisyon olur, stop o miktar icin). Donus: hata metni veya None."""
        if side not in ("long", "short"):
            return "geçersiz yön"
        if cid is None or cid in self.cids:
            return "emir kimliği yok veya daha önce kullanıldı (çift emir engellendi)"
        if s in self.pos:  # one-way: ayni sembolde ters/ek pozisyon yok
            return f"{s} için açık pozisyon var (one-way modda çakışan emir reddedilir)"
        if not 1 <= lev <= MAX_LEV:
            return f"kaldıraç {lev}x, izin verilen en çok {MAX_LEV}x"
        if sl is None or not sl > 0:
            return "stop zorunlu"
        sgn = 1 if side == "long" else -1
        px = price * (1 + sgn * self.slip)
        if sgn * (px - sl) <= 0:
            return "stop yanlış tarafta"
        lq = liq_price(side, px, lev)
        if abs(px - sl) > LIQ_BUFFER * abs(px - lq):  # stop, giris-likidasyon yolunun en cok yarisinda
            return f"stop likidasyona çok yakın (stop {sl:g}, likidasyon {lq:g})"
        if tp is not None and sgn * (tp - px) <= 0:
            return "hedef yanlış tarafta"
        r = self.rule(s)
        q = r.qty(qty * fill)
        if q < r.min_qty or q * px < r.min_notional:
            return "miktar borsa alt sınırının altında"
        margin, fee = q * px / lev, q * px * self.fee
        if margin + fee > self.cash + 1e-9:
            return "teminat yetersiz"
        self.cids.add(cid)
        self.cash -= margin + fee
        self.pos[s] = {"side": side, "qty": q, "entry": px, "lev": lev, "margin": margin, "margin0": margin, "sl": r.price(sl), "sl0": r.price(sl),
                       "tp": None if tp is None else r.price(tp), "liq": lq, "t": self.clock(), "fee": fee, "funding": 0.0, "realized": 0.0,
                       "partial": fill < 1}
        self.log.append(("open", s, side, q, px, cid))
        return None

    # ---------------- yonetim ----------------
    def move_stop(self, s, new):
        """Stop sadece lehe: LONG yukari, SHORT asagi; anlik fiyatin otesine gecemez (cagiran kontrol eder)."""
        p = self.pos.get(s)
        if not p:
            return "pozisyon yok"
        sgn = 1 if p["side"] == "long" else -1
        if sgn * (new - p["sl"]) <= 0:
            return "stop sadece pozisyon lehine taşınabilir (riski artıran değişiklik reddedildi)"
        p["sl"] = self.rule(s).price(new)
        return None

    def funding(self, s, rate, mark):
        """Fonlama: rate>0 iken LONG oder, SHORT alir (rate<0 tersi). Isolated: teminattan duser/eklenir."""
        p = self.pos.get(s)
        if not p:
            return 0.0
        amt = p["qty"] * mark * rate * (1 if p["side"] == "long" else -1)
        p["funding"] += amt
        p["margin"] -= amt
        p["liq"] = self._liq(p)
        return amt

    def _liq(self, p):
        """Teminat degisince likidasyon: zarar = teminat - bakim teminati olunca."""
        sgn = 1 if p["side"] == "long" else -1
        return p["entry"] - sgn * (p["margin"] - p["qty"] * p["entry"] * MMR) / p["qty"]

    def bar(self, s, o, h, l, c):
        """Kapanmis mum: once likidasyon/stop (ayni mumda hedefle birlikte: once stop), sonra hedef. Bosluk: acilistan dolar."""
        p = self.pos.get(s)
        if not p:
            return None
        lg = p["side"] == "long"
        stop_hit = l <= p["sl"] if lg else h >= p["sl"]
        liq_hit = l <= p["liq"] if lg else h >= p["liq"]
        if liq_hit and (not stop_hit or (p["liq"] >= p["sl"] if lg else p["liq"] <= p["sl"])):
            return self._book(s, p["liq"], "LIKIDASYON", p["qty"], liq=True)
        if stop_hit:
            px = min(p["sl"], o) if lg else max(p["sl"], o)
            return self._book(s, px * (1 - self.slip if lg else 1 + self.slip), "STOP", p["qty"])
        if p["tp"] is not None and (h >= p["tp"] if lg else l <= p["tp"]):
            px = max(p["tp"], o) if lg else min(p["tp"], o)
            return self._book(s, px, "HEDEF", p["qty"])
        return None

    def close(self, s, price, reason, qty=None, cid=None):
        """reduceOnly piyasa cikisi: miktar acik pozisyonu asamaz (ters pozisyon acilmaz)."""
        p = self.pos.get(s)
        if not p:
            return "pozisyon yok"
        if cid is not None:
            if cid in self.cids:
                return "emir kimliği daha önce kullanıldı (çift emir engellendi)"
            self.cids.add(cid)
        q = p["qty"] if qty is None else self.rule(s).qty(min(qty, p["qty"]))
        if q <= 0:
            return "geçersiz miktar"
        lg = p["side"] == "long"
        self._book(s, price * (1 - self.slip if lg else 1 + self.slip), reason, q)
        return None

    def _book(self, s, px, reason, q, liq=False):
        p = self.pos[s]
        sgn = 1 if p["side"] == "long" else -1
        frac = q / p["qty"]
        gross = sgn * (px - p["entry"]) * q
        fee = q * px * self.fee
        margin, m0 = p["margin"] * frac, p["margin0"] * frac  # margin: fonlama sonrasi kalan teminat
        back = 0.0 if liq else max(margin + gross - fee, 0.0)  # isolated: zarar en cok teminat kadar
        self.cash += back
        pnl = back - m0 - p["fee"] * frac  # fonlama teminattan dustugu icin back icinde
        p["realized"] += pnl
        r0 = abs(p["entry"] - p["sl0"]) * q
        if frac >= 1 - 1e-12:
            del self.pos[s]
        else:
            p["qty"] -= q
            p["margin"] -= margin
            p["margin0"] -= m0
            p["fee"] -= p["fee"] * frac
            p["funding"] -= p["funding"] * frac
        t = {"sym": s, "side": p["side"], "entry": p["entry"], "exit": px, "qty": q, "pnl": pnl, "R": pnl / r0 if r0 else 0.0,
             "reason": reason, "t_open": p["t"], "t": self.clock()}
        self.trades.append(t)
        return t

    # ---------------- raporlar ----------------
    def equity(self, marks):
        out = self.cash
        for s, p in self.pos.items():
            sgn = 1 if p["side"] == "long" else -1
            m = marks.get(s, p["entry"])
            out += max(p["margin"] + sgn * (m - p["entry"]) * p["qty"] - p["qty"] * m * self.fee, 0.0)
        return out

    def open_risk(self):
        """Stoplar dolarsa kaybedilecek toplam tutar (komisyon haric)."""
        return sum(max((1 if p["side"] == "long" else -1) * (p["entry"] - p["sl"]), 0.0) * p["qty"] for p in self.pos.values())

    def report(self):
        """LONG ve SHORT ayri: islem, kazanma, PF, ortalama R, net."""
        out = {}
        for side in ("long", "short"):
            t = [x for x in self.trades if x["side"] == side]
            w = sum(x["pnl"] for x in t if x["pnl"] > 0)
            ls = -sum(x["pnl"] for x in t if x["pnl"] <= 0)
            out[side] = {"islem": len(t), "kazanma": sum(x["pnl"] > 0 for x in t) / len(t) * 100 if t else None,
                         "PF": w / ls if ls > 0 else None, "ort_R": sum(x["R"] for x in t) / len(t) if t else None,
                         "net": sum(x["pnl"] for x in t)}
        return out

    def reconcile(self, exchange):
        """exchange: {sym: (side, qty)} borsadaki pozisyonlar. Donus: uyusmazlik listesi (canli icin; sanalda test edilir)."""
        bad = []
        for s in set(exchange) | set(self.pos):
            e, p = exchange.get(s), self.pos.get(s)
            if not e and p:
                bad.append(f"{s}: defterde {p['side']} {p['qty']:g}, borsada yok")
            elif e and not p:
                bad.append(f"{s}: borsada {e[0]} {e[1]:g}, defterde yok")
            elif e and (e[0] != p["side"] or abs(e[1] - p["qty"]) > self.rule(s).step / 2):
                bad.append(f"{s}: borsa {e[0]} {e[1]:g}, defter {p['side']} {p['qty']:g}")
        return bad
