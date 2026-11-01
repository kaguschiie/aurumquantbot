"""Balina akisi + likidasyon haritasi (Arkham / CoinGlass mantigi, ucretsiz veriyle).
- Borsa balinalari: Binance'teki buyuk tekil islemlerden (aggTrades) net balina alis/satis baskisi (her coin icin).
- Zincir ustu (istege bagli): Whale Alert API anahtari girilirse etiketli buyuk transferler: borsaya giris = satis
  baskisi olabilir, borsadan cikis = birikim. (Arkham tarzi etiketli cuzdan verisi ucretli API ister.)
- Likidasyon haritasi: Binance vadeli 1s mumlar + acik pozisyon (OI) artislari; yeni acilan pozisyonlar agresif
  alici payina gore long/short ayrilir, 10x-100x kaldirac katmanlarinda tahmini likidasyon fiyatlari toplanir. Fiyatin
  gectigi seviyeler silinir (zaten likide oldu). Sonuc: fiyatin altinda long, ustunde short likidasyon kumeleri."""
import logging
import threading
import time

import requests

import config as C

log = logging.getLogger("bot")
SPOT, FAPI = "https://api.binance.com/api/v3/", "https://fapi.binance.com/"
LEV = ((10, 0.30), (25, 0.30), (50, 0.25), (100, 0.15))  # kaldirac katmani ve pozisyon payi (tahmini)
MMR = 0.005                                              # bakim marji payi


class Zincir:
    def __init__(self):
        self.whale, self.chain, self.liq, self.t, self.error, self._started = {}, [], {}, 0.0, "", False
        self.get_symbols = lambda: ["BTC/USDT", "ETH/USDT"]

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def start(self, get_symbols):
        self.get_symbols = get_symbols
        if not self._started:
            self._started = True
            threading.Thread(target=self._loop, name="zincir", daemon=True).start()

    # ---------------- borsa balinalari ----------------
    def whale_flow(self, sym):
        """Son ~1000 islemdeki buyuk tekil islemler: net balina alis/satis orani (-1..+1)."""
        rows = requests.get(SPOT + "aggTrades", params={"symbol": sym.replace("/", ""), "limit": 1000}, timeout=10).json()
        if not isinstance(rows, list) or not rows:
            return None
        thr = 250_000 if sym in ("BTC/USDT", "ETH/USDT") else 50_000
        buy = sell = n = 0.0
        for r in rows:
            usd = float(r["p"]) * float(r["q"])
            if usd >= thr:
                n += 1
                if r["m"]:  # alici piyasa yapici = agresif satici
                    sell += usd
                else:
                    buy += usd
        mins = max((rows[-1]["T"] - rows[0]["T"]) / 60000, 0.1)
        return {"sym": sym, "buy": round(buy), "sell": round(sell), "n": int(n), "mins": round(mins, 1),
                "ratio": round((buy - sell) / (buy + sell), 2) if buy + sell else 0.0, "thr": thr, "t": time.time()}

    def _onchain(self):
        """Whale Alert (istege bagli anahtar): son 1 saatin 1M$+ transferleri, borsa giris/cikis olarak."""
        r = requests.get("https://api.whale-alert.io/v1/transactions", timeout=15,
                         params={"api_key": C.WHALE_ALERT_KEY, "min_value": 1_000_000, "start": int(time.time()) - 3600}).json()
        out = []
        for x in r.get("transactions") or []:
            fr, to = x.get("from") or {}, x.get("to") or {}
            kind = ("giris" if to.get("owner_type") == "exchange" and fr.get("owner_type") != "exchange" else
                    "cikis" if fr.get("owner_type") == "exchange" and to.get("owner_type") != "exchange" else "diger")
            out.append({"sym": str(x.get("symbol", "")).upper(), "usd": x.get("amount_usd"), "kind": kind,
                        "from": fr.get("owner") or fr.get("owner_type") or "bilinmeyen",
                        "to": to.get("owner") or to.get("owner_type") or "bilinmeyen", "t": x.get("timestamp")})
        self.chain = sorted(out, key=lambda x: -(x["usd"] or 0))[:40]

    def _loop(self):
        last_chain = 0.0
        while True:
            try:
                for s in list(dict.fromkeys(self.get_symbols()))[:14]:
                    try:
                        w = self.whale_flow(s)
                        if w:
                            self.whale[s] = w
                    except (requests.RequestException, ValueError, KeyError):
                        pass
                if C.WHALE_ALERT_KEY and time.time() - last_chain > 120:
                    last_chain = time.time()
                    self._onchain()
                self.t, self.error = time.time(), ""
            except Exception as e:
                self.error = str(e)[:160]
                log.warning("zincir: %s", e)
            time.sleep(60)

    def chain_summary(self, coin):
        """Coin icin son 1 saatin zincir ustu borsa girisi/cikisi (USD)."""
        xs = [x for x in self.chain if x["sym"] == coin]
        return sum(x["usd"] or 0 for x in xs if x["kind"] == "giris"), sum(x["usd"] or 0 for x in xs if x["kind"] == "cikis")

    # ---------------- likidasyon haritasi ----------------
    def liq_map(self, sym, ttl=600):
        """Tahmini likidasyon kumeleri (10 dk onbellek). Vadelisi olmayan coin: None."""
        c = self.liq.get(sym)
        if c and time.time() - c["t"] < ttl:
            return c
        key = sym.replace("/", "")
        try:
            kl = requests.get(FAPI + "fapi/v1/klines", params={"symbol": key, "interval": "1h", "limit": 120}, timeout=10).json()
            oi = requests.get(FAPI + "futures/data/openInterestHist", params={"symbol": key, "period": "1h", "limit": 120},
                              timeout=10).json()
        except (requests.RequestException, ValueError):
            return None
        if not isinstance(kl, list) or not isinstance(oi, list) or len(kl) < 10 or len(oi) < 10:
            self.liq[sym] = {"t": time.time(), "long": [], "short": [], "none": True}
            return None
        oim = {int(x["timestamp"]): float(x["sumOpenInterest"]) for x in oi}
        bars = [(int(k[0]), float(k[2]), float(k[3]), float(k[4]), float(k[5]), float(k[9])) for k in kl]
        price = bars[-1][3]
        lows, highs = [b[2] for b in bars], [b[1] for b in bars]
        step = price * 0.0025  # %0,25'lik fiyat kovalari
        longs, shorts, prev = {}, {}, None
        for i, (t, h, low, cl, v, tb) in enumerate(bars):
            cur = oim.get(t)
            if cur is None:
                continue
            d = cur - prev if prev is not None else 0.0
            prev = cur
            if d <= 0 or v <= 0:
                continue
            px, fb = (h + low + cl) / 3, min(max(tb / v, 0.0), 1.0)
            later_lo, later_hi = min(lows[i + 1:] or [low]), max(highs[i + 1:] or [h])
            for lev, w in LEV:
                lp, sp = px * (1 - 1 / lev + MMR), px * (1 + 1 / lev - MMR)
                if later_lo > lp:  # henuz likide olmamis long'lar
                    k = round(lp / step)
                    longs[k] = longs.get(k, 0.0) + d * fb * w * px
                if later_hi < sp:
                    k = round(sp / step)
                    shorts[k] = shorts.get(k, 0.0) + d * (1 - fb) * w * px
        mx = max(list(longs.values()) + list(shorts.values()) + [1e-9])
        pick = lambda m, lo, hi: sorted(([round(k * step, 8), round(v / mx, 3), round(v)] for k, v in m.items()
                                         if lo < k * step < hi and v / mx >= 0.15), key=lambda x: -x[1])[:6]
        out = {"t": time.time(), "price": price, "long": pick(longs, price * 0.8, price),
               "short": pick(shorts, price, price * 1.2)}
        self.liq[sym] = out
        return out

    def adjust(self, sym, price, stop, tp2, atr):
        """Stop/hedef optimizasyonu: stop buyuk long-likidasyon kumesinin hemen ustundeyse (igne avi bolgesi) kumenin
        altina alinir; hedef, ustteki buyuk short-likidasyon kumesine (fiyat miknatisi) cekilir. Donus: (stop, tp2, not)."""
        m = self.liq_map(sym)
        if not m or m.get("none"):
            return stop, tp2, ""
        notes = []
        near = [p for p, w, _ in m["long"] if w >= 0.3 and stop * 0.985 <= p <= stop * 1.012 and p < price * 0.997]
        if near:
            new = min(near) - 0.25 * atr
            if (price - new) / price <= 0.15:
                notes.append(f"stop {stop:.6g} → {new:.6g}: altındaki long likidasyon kümesinin ({min(near):.6g}) "
                             f"iğnesine karşı küme altına alındı (pozisyon risk sabit kalacak şekilde küçülür)")
                tp2 = price + (tp2 - price) * (price - new) / (price - stop)  # odul/risk orani korunur
                stop = new
        r = price - stop
        mag = sorted(p for p, w, _ in m["short"] if w >= 0.3 and price + 1.5 * r <= p < tp2)
        if mag:
            notes.append(f"hedef {tp2:.6g} → {mag[0] * 0.997:.6g}: üstteki short likidasyon kümesi ({mag[0]:.6g}) fiyat mıknatısı")
            tp2 = mag[0] * 0.997
        return stop, tp2, "; ".join(notes)

    def brief_line(self, syms):
        """Claude'a: balina akisi + likidasyon kumeleri (sadece onbellekteki veriden, ag cagrisi yok)."""
        ws = [self.whale[s] for s in syms if s in self.whale and self.whale[s]["n"]]
        parts = [f"{w['sym'].split('/')[0]} {w['ratio']:+.2f} (al {w['buy'] / 1e6:.1f}M$/sat {w['sell'] / 1e6:.1f}M$, "
                 f"{w['mins']:.0f} dk)" for w in ws[:10]]
        out = ["BALINA AKISI (Binance buyuk tekil islemler; +1 tamamen balina alisi, -1 satisi): " + "; ".join(parts)] if parts else []
        inflow = {}
        for x in self.chain:
            if x["kind"] == "giris":
                inflow[x["sym"]] = inflow.get(x["sym"], 0) + (x["usd"] or 0)
        if inflow:
            out.append("ZINCIR USTU (son 1 sa borsaya giris, satis baskisi olabilir): " + ", ".join(
                f"{k} {v / 1e6:.0f}M$" for k, v in sorted(inflow.items(), key=lambda kv: -kv[1])[:6]))
        liq = [(s, self.liq[s]) for s in syms if s in self.liq and not self.liq[s].get("none")]
        if liq:
            out.append("LIKIDASYON KUMELERI (tahmini; asagida long, yukarida short): " + "; ".join(
                f"{s.split('/')[0]} long {'/'.join(f'{p:.6g}' for p, _, _ in m['long'][:2]) or '-'} short "
                f"{'/'.join(f'{p:.6g}' for p, _, _ in m['short'][:2]) or '-'}" for s, m in liq[:8]))
        return out

    def snapshot(self):
        return {"whale": sorted(self.whale.values(), key=lambda w: -(w["buy"] + w["sell"])), "chain": self.chain[:20],
                "t": self.t, "error": self.error, "key": bool(C.WHALE_ALERT_KEY)}
