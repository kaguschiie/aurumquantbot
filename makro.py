"""Borsa / Makro modulu: kuresel endeksler, altin, dolar endeksi, faiz, BIST ve buyuk hisseler (Yahoo Finance, 60 sn).
Kripto kararlarina risk istahi baglami olarak girer (risk-on / risk-off) ve altinin (PAXG) gucunu gosterir.
Hisseler ve endeksler Binance spotta islem goremez: sadece izlenir; altin PAXG/USDT ile islem gorur."""
import logging
import threading
import time

import requests

log = logging.getLogger("bot")
URL = "https://query1.finance.yahoo.com/v7/finance/spark"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
ASSETS = [  # (Yahoo sembolu, ad, grup)
    ("^GSPC", "S&P 500", "Endeks"), ("^NDX", "Nasdaq 100", "Endeks"), ("^DJI", "Dow Jones", "Endeks"),
    ("XU100.IS", "BIST 100", "Endeks"), ("^VIX", "VIX korku endeksi", "Risk"), ("DX-Y.NYB", "Dolar endeksi (DXY)", "Döviz"),
    ("USDTRY=X", "USD/TRY", "Döviz"), ("^TNX", "ABD 10 yıllık faiz", "Faiz"), ("GC=F", "Altın (ons)", "Emtia"),
    ("SI=F", "Gümüş (ons)", "Emtia"), ("CL=F", "Ham petrol", "Emtia"),
    ("NVDA", "Nvidia", "Hisse"), ("AAPL", "Apple", "Hisse"), ("MSFT", "Microsoft", "Hisse"), ("AMZN", "Amazon", "Hisse"),
    ("GOOGL", "Alphabet", "Hisse"), ("META", "Meta", "Hisse"), ("TSLA", "Tesla", "Hisse"), ("COIN", "Coinbase", "Hisse"),
    ("MSTR", "Strategy (MSTR)", "Hisse")]
NAMES = {s: (n, g) for s, n, g in ASSETS}


class Makro:
    def __init__(self):
        self.data, self.t, self.t_long, self.error, self._started = {}, 0.0, 0.0, "", False

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def start(self):
        if not self._started:
            self._started = True
            threading.Thread(target=self._loop, name="makro", daemon=True).start()

    def _get(self, rng, iv):
        out, syms = {}, [a[0] for a in ASSETS]
        for i in range(0, len(syms), 20):
            r = requests.get(URL, params={"symbols": ",".join(syms[i:i + 20]), "range": rng, "interval": iv},
                             headers=UA, timeout=15)
            r.raise_for_status()
            for x in (r.json().get("spark") or {}).get("result") or []:
                out[x["symbol"]] = (x.get("response") or [{}])[0]
        return out

    @staticmethod
    def _closes(resp):
        q = ((resp.get("indicators") or {}).get("quote") or [{}])[0]
        return [c for c in q.get("close") or [] if c is not None]

    def _intraday(self):
        now = time.time()
        for sym, resp in self._get("1d", "5m").items():
            m, cl = resp.get("meta") or {}, self._closes(resp)
            per = (m.get("currentTradingPeriod") or {}).get("regular") or {}
            px, prev = m.get("regularMarketPrice"), m.get("previousClose") or m.get("chartPreviousClose")
            name, grp = NAMES.get(sym, (sym, ""))
            self.data.setdefault(sym, {"sym": sym}).update(
                name=name, group=grp, price=px, prev=prev, day=(px / prev - 1) * 100 if px and prev else None,
                spark=cl[::max(1, len(cl) // 48)][-48:],
                open=per.get("start", 0) <= now < per.get("end", 0) and now - (m.get("regularMarketTime") or 0) < 1800,
                t_px=m.get("regularMarketTime"), cur=m.get("currency"))
        self.t = now

    def _long(self):
        for sym, resp in self._get("3mo", "1d").items():
            cl = self._closes(resp)
            px = (self.data.get(sym) or {}).get("price") or (cl[-1] if cl else None)
            ch = lambda n: (px / cl[-n - 1] - 1) * 100 if px and len(cl) > n and cl[-n - 1] else None
            self.data.setdefault(sym, {"sym": sym}).update(w1=ch(5), m1=ch(21), m3=(px / cl[0] - 1) * 100 if px and cl else None)
        self.t_long = time.time()

    def _loop(self):
        while True:
            try:
                self._intraday()
                if time.time() - self.t_long > 1800:
                    self._long()
                self.error = ""
            except (requests.RequestException, ValueError, KeyError, TypeError) as e:
                self.error = f"Makro veri alınamadı: {str(e)[:120]}"
                log.warning("makro: %s", e)
            time.sleep(60)

    def live(self):
        return time.time() - self.t < 300

    def risk(self):
        """Kuresel risk istahi: 'riskoff' / 'riskon' / 'notr' ve sebebi. Veri yoksa (None, sebep)."""
        g = lambda s: self.data.get(s) or {}
        ndx, vix, dxy = g("^NDX"), g("^VIX"), g("DX-Y.NYB")
        if not self.live() or ndx.get("day") is None:
            return None, "makro veri yok"
        nd, vx, vd, dd = ndx.get("day"), vix.get("price"), vix.get("day"), dxy.get("day")
        bad = []
        if nd <= -1.0:
            bad.append(f"Nasdaq %{nd:+.1f}")
        if vx and vx >= 25:
            bad.append(f"VIX {vx:.0f}")
        if vd is not None and vd >= 10:
            bad.append(f"VIX %{vd:+.0f}")
        if dd is not None and dd >= 0.6:
            bad.append(f"dolar endeksi %{dd:+.1f}")
        if bad:
            return "riskoff", ", ".join(bad)
        if nd >= 0.5 and (vx or 99) < 20:
            return "riskon", f"Nasdaq %{nd:+.1f}, VIX {vx:.0f}"
        return "notr", f"Nasdaq %{nd:+.1f}" + (f", VIX {vx:.0f}" if vx else "")

    def gold(self):
        """Altinin (ons) gucu: gunluk ve haftalik degisim."""
        x = self.data.get("GC=F") or {}
        return {"price": x.get("price"), "day": x.get("day"), "w1": x.get("w1"), "m1": x.get("m1"),
                "up": (x.get("w1") or 0) > 0 and (x.get("day") or 0) > -0.3}

    def snapshot(self):
        risk, why = self.risk()
        rows = [dict(self.data[s]) for s, _, _ in ASSETS if s in self.data]
        return {"rows": rows, "risk": risk, "why": why, "gold": self.gold(), "t": self.t, "error": self.error}

    def brief_line(self):
        """Claude'a giden tek satir makro ozet."""
        if not self.live():
            return ""
        f = lambda s, k="day": (self.data.get(s) or {}).get(k)
        part = lambda s, n: f"{n} %{f(s):+.1f}" if f(s) is not None else ""
        risk, why = self.risk()
        open_ = any((self.data.get(s) or {}).get("open") for s in ("^GSPC", "^NDX"))
        items = [part("^GSPC", "S&P"), part("^NDX", "Nasdaq"), part("XU100.IS", "BIST"), part("DX-Y.NYB", "DXY"),
                 part("GC=F", "altin ons") + (f" (hafta %{f('GC=F', 'w1'):+.1f})" if f("GC=F", "w1") is not None else ""),
                 part("CL=F", "petrol"), f"VIX {f('^VIX', 'price'):.1f}" if f("^VIX", "price") else "",
                 f"ABD 10Y %{f('^TNX', 'price'):.2f}" if f("^TNX", "price") else ""]
        return (f"MAKRO (Yahoo, borsa {'acik' if open_ else 'kapali: son kapanis'}): " + ", ".join(i for i in items if i)
                + f". Kuresel risk istahi: {risk} ({why}).")
