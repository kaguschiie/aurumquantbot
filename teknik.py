import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ccxt
import numpy as np
import pandas as pd

import config as C
import data as D

log = logging.getLogger("bot")
TFS = ("15m", "1h", "4h", "1d")
TF_SN = {"15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}


def label(v):
    if v is None:
        return None
    return "GUCLU SAT" if v < -.5 else "SAT" if v < -.1 else "NOTR" if v <= .1 else "AL" if v <= .5 else "GUCLU AL"


def _wma(s, n):
    w = np.arange(1, n + 1, dtype=float)
    out = np.full(len(s), np.nan)
    if len(s) >= n:
        out[n - 1:] = np.convolve(np.asarray(s, float), w[::-1], "valid") / w.sum()
    return pd.Series(out, index=s.index)


def _mad(s, n):
    out = np.full(len(s), np.nan)
    if len(s) >= n:
        win = np.lib.stride_tricks.sliding_window_view(np.asarray(s, float), n)
        out[n - 1:] = np.abs(win - win.mean(axis=1, keepdims=True)).mean(axis=1)
    return pd.Series(out, index=s.index)


def rating(df):
    c, h, l, v = df.close, df.high, df.low, df.volume
    px, up = c.iloc[-1], lambda s: s.iloc[-1] > s.iloc[-2]
    ma = []
    for n in (10, 20, 30, 50, 100, 200):
        if len(c) > n:
            ma += [np.sign(px - c.rolling(n).mean().iloc[-1]), np.sign(px - c.ewm(span=n, adjust=False).mean().iloc[-1])]
    ma.append(np.sign(px - ((c * v).rolling(20).sum() / v.rolling(20).sum()).iloc[-1]))
    ma.append(np.sign(px - _wma(2 * _wma(c, 4) - _wma(c, 9), 3).iloc[-1]))
    base = (h.rolling(26).max() + l.rolling(26).min()) / 2
    ma.append(np.sign(px - base.iloc[-1]))

    osc = []
    d = c.diff()
    rsi = 100 - 100 / (1 + d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
                       / (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean())
    osc.append(1 if rsi.iloc[-1] < 30 and up(rsi) else -1 if rsi.iloc[-1] > 70 and not up(rsi) else 0)
    k = 100 * (c - l.rolling(14).min()) / (h.rolling(14).max() - l.rolling(14).min())
    k = k.rolling(3).mean()
    dd = k.rolling(3).mean()
    osc.append(1 if k.iloc[-1] < 20 and dd.iloc[-1] < 20 and k.iloc[-1] > dd.iloc[-1]
               else -1 if k.iloc[-1] > 80 and dd.iloc[-1] > 80 and k.iloc[-1] < dd.iloc[-1] else 0)
    tp = (h + l + c) / 3
    cci = (tp - tp.rolling(20).mean()) / (0.015 * _mad(tp, 20))
    osc.append(1 if cci.iloc[-1] < -100 and up(cci) else -1 if cci.iloc[-1] > 100 and not up(cci) else 0)
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdm = (h.diff()).where((h.diff() > -l.diff()) & (h.diff() > 0), 0.0)
    mdm = (-l.diff()).where((-l.diff() > h.diff()) & (-l.diff() > 0), 0.0)
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi)).ewm(alpha=1 / 14, adjust=False).mean()
    osc.append((1 if pdi.iloc[-1] > mdi.iloc[-1] else -1) if adx.iloc[-1] > 20 and up(adx) else 0)
    med = (h + l) / 2
    ao = med.rolling(5).mean() - med.rolling(34).mean()
    osc.append(1 if ao.iloc[-1] > 0 and up(ao) else -1 if ao.iloc[-1] < 0 and not up(ao) else 0)
    mom = c - c.shift(10)
    osc.append(1 if up(mom) else -1)
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    osc.append(1 if macd.iloc[-1] > macd.ewm(span=9, adjust=False).mean().iloc[-1] else -1)
    srsi = 100 * (rsi - rsi.rolling(14).min()) / (rsi.rolling(14).max() - rsi.rolling(14).min())
    sk = srsi.rolling(3).mean()
    sd = sk.rolling(3).mean()
    osc.append(1 if sk.iloc[-1] < 20 and sk.iloc[-1] > sd.iloc[-1] else -1 if sk.iloc[-1] > 80 and sk.iloc[-1] < sd.iloc[-1] else 0)
    wr = -100 * (h.rolling(14).max() - c) / (h.rolling(14).max() - l.rolling(14).min())
    osc.append(1 if wr.iloc[-1] < -80 and up(wr) else -1 if wr.iloc[-1] > -20 and not up(wr) else 0)
    ema13 = c.ewm(span=13, adjust=False).mean()
    bbp = (h - ema13) + (l - ema13)
    trend_up = up(ema13)
    osc.append(1 if trend_up and bbp.iloc[-1] < 0 and up(bbp) else -1 if not trend_up and bbp.iloc[-1] > 0 and not up(bbp) else 0)
    bp = c - pd.concat([l, c.shift()], axis=1).min(axis=1)
    tr2 = pd.concat([h, c.shift()], axis=1).max(axis=1) - pd.concat([l, c.shift()], axis=1).min(axis=1)
    avg = lambda n: bp.rolling(n).sum() / tr2.rolling(n).sum()
    uo = 100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7
    osc.append(1 if uo.iloc[-1] > 70 else -1 if uo.iloc[-1] < 30 else 0)

    ma = [x for x in ma if np.isfinite(x)]
    m, o = (float(np.mean(ma)) if ma else 0.0), float(np.mean(osc))
    rec = (m + o) / 2
    f = lambda s: None if not np.isfinite(s.iloc[-1]) else round(float(s.iloc[-1]), 1)
    return {"rec": round(rec, 3), "ma": round(m, 3), "osc": round(o, 3), "rsi": f(rsi), "adx": f(adx),
            "atr_pct": round(float(atr.iloc[-1] / px), 5) if px else None, "label": label(rec)}


def rating_series(df):
    c, h, l, v = df.close, df.high, df.low, df.volume
    up, sg = lambda s: s > s.shift(), lambda s: np.sign(c - s)

    def wma(s, n):
        w = np.arange(1, n + 1, dtype=float)
        out = np.full(len(s), np.nan)
        if len(s) >= n:
            out[n - 1:] = np.convolve(np.asarray(s, float), w[::-1], "valid") / w.sum()
        return pd.Series(out, index=s.index)

    ma = [sg(c.rolling(n).mean()) for n in (10, 20, 30, 50, 100, 200)] + \
         [sg(c.ewm(span=n, adjust=False).mean()) for n in (10, 20, 30, 50, 100, 200)]
    ma += [sg((c * v).rolling(20).sum() / v.rolling(20).sum()), sg(wma(2 * wma(c, 4) - wma(c, 9), 3)),
           sg((h.rolling(26).max() + l.rolling(26).min()) / 2)]
    ma = pd.concat(ma, axis=1)
    m = ma.mean(axis=1).fillna(0.0)
    vote = lambda buy, sell: np.where(buy, 1, np.where(sell, -1, 0))
    d = c.diff()
    rsi = 100 - 100 / (1 + d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean()
                       / (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean())
    osc = [vote((rsi < 30) & up(rsi), (rsi > 70) & ~up(rsi))]
    k = (100 * (c - l.rolling(14).min()) / (h.rolling(14).max() - l.rolling(14).min())).rolling(3).mean()
    dd = k.rolling(3).mean()
    osc.append(vote((k < 20) & (dd < 20) & (k > dd), (k > 80) & (dd > 80) & (k < dd)))
    tp = (h + l + c) / 3
    mad = np.full(len(tp), np.nan)
    if len(tp) >= 20:
        win = np.lib.stride_tricks.sliding_window_view(tp.values.astype(float), 20)
        mad[19:] = np.abs(win - win.mean(axis=1, keepdims=True)).mean(axis=1)
    cci = (tp - tp.rolling(20).mean()) / (0.015 * pd.Series(mad, index=tp.index))
    osc.append(vote((cci < -100) & up(cci), (cci > 100) & ~up(cci)))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1 / 14, adjust=False).mean()
    pdm = (h.diff()).where((h.diff() > -l.diff()) & (h.diff() > 0), 0.0)
    mdm = (-l.diff()).where((-l.diff() > h.diff()) & (-l.diff() > 0), 0.0)
    pdi = 100 * pdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    mdi = 100 * mdm.ewm(alpha=1 / 14, adjust=False).mean() / atr
    adx = (100 * (pdi - mdi).abs() / (pdi + mdi)).ewm(alpha=1 / 14, adjust=False).mean()
    osc.append(np.where((adx > 20) & up(adx), np.where(pdi > mdi, 1, -1), 0))
    med = (h + l) / 2
    ao = med.rolling(5).mean() - med.rolling(34).mean()
    osc.append(vote((ao > 0) & up(ao), (ao < 0) & ~up(ao)))
    osc.append(np.where(up(c - c.shift(10)), 1, -1))
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    osc.append(np.where(macd > macd.ewm(span=9, adjust=False).mean(), 1, -1))
    sk = (100 * (rsi - rsi.rolling(14).min()) / (rsi.rolling(14).max() - rsi.rolling(14).min())).rolling(3).mean()
    sd = sk.rolling(3).mean()
    osc.append(vote((sk < 20) & (sk > sd), (sk > 80) & (sk < sd)))
    wr = -100 * (h.rolling(14).max() - c) / (h.rolling(14).max() - l.rolling(14).min())
    osc.append(vote((wr < -80) & up(wr), (wr > -20) & ~up(wr)))
    ema13 = c.ewm(span=13, adjust=False).mean()
    bbp = (h - ema13) + (l - ema13)
    osc.append(vote(up(ema13) & (bbp < 0) & up(bbp), ~up(ema13) & (bbp > 0) & ~up(bbp)))
    bp = c - pd.concat([l, c.shift()], axis=1).min(axis=1)
    tr2 = pd.concat([h, c.shift()], axis=1).max(axis=1) - pd.concat([l, c.shift()], axis=1).min(axis=1)
    avg = lambda n: bp.rolling(n).sum() / tr2.rolling(n).sum()
    uo = 100 * (4 * avg(7) + 2 * avg(14) + avg(28)) / 7
    osc.append(vote(uo > 70, uo < 30))
    o = np.mean(np.vstack(osc), axis=0)
    return pd.DataFrame({"rec": (m.values + o) / 2, "rsi": rsi.values, "atr_pct": (atr / c).values}, index=df.index)


RADAR_TOP = 45
RADAR_FULL = 20
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")


def radar_score(r):
    te = r.get("tech") or {}
    sc = (te.get("1h") or 0) + 0.6 * (te.get("4h") or 0) + 0.3 * (te.get("15m") or 0) + 0.2 * (te.get("1d") or 0)
    sc += min(r.get("vspike") or 0, 6) / 4 + max(min(r.get("rs24") or 0, 30), -10) / 15 + (0.3 if r.get("breakout") else 0)
    if (r.get("rsi") or 0) > 75:
        sc -= 0.5
    return round(sc, 3)


class Teknik:

    def __init__(self):
        self.data, self.opps, self.t, self.t_opps, self.error = {}, [], 0.0, 0.0, ""
        self.radar_map, self.n_all, self.n_liquid, self.liquid = {}, 0, 0, []
        self._started, self._saklanan = False, {}
        self.ex = ccxt.binance({"enableRateLimit": True})

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def start(self, get_symbols):
        if self._started:
            self.get_symbols = get_symbols
            return
        self._started, self.get_symbols = True, get_symbols
        threading.Thread(target=self._loop, name="teknik", daemon=True).start()

    def live(self, max_age=300):
        return time.time() - self.t < max_age

    def _klines(self, sym, tf, limit=250, zaman=False):
        rows = self.ex.public_get_klines({"symbol": sym.replace("/", ""), "interval": tf, "limit": limit})
        a = np.array([[float(r[i]) for i in (1, 2, 3, 4, 5, 9)] for r in rows])
        df = pd.DataFrame(a, columns=["open", "high", "low", "close", "volume", "tb"])
        return (df, float(rows[-1][0]) / 1000 if rows else 0.0) if zaman else df

    def ratings(self, sym, tfs=TFS):
        out = {}
        for tf in tfs:
            x = self._saklanan.get((sym, tf))
            if x and time.time() < x[0]:
                out[tf] = x[1]
                continue
            try:
                df, t_son = self._klines(sym, tf, zaman=True)
                if len(df) >= 61:
                    out[tf] = rating(df.iloc[:-1])
                    self._saklanan[(sym, tf)] = (t_son + TF_SN.get(tf, 0) + 5, out[tf])
            except (ccxt.BaseError, ValueError, KeyError, IndexError) as e:
                log.debug("teknik %s %s: %s", sym, tf, e)
        return out

    def _universe_pass(self):
        syms = list(self.get_symbols())
        with ThreadPoolExecutor(4) as pool:
            res = dict(zip(syms, pool.map(self.ratings, syms)))
        self.data.update({s: r for s, r in res.items() if r})
        self.t = time.time()

    def _deep(self, r, btc_r4):
        try:
            df = self._klines(r["sym"], "1h")
        except (ccxt.BaseError, ValueError, KeyError, IndexError):
            return None
        if len(df) < 61:
            return None
        rt = rating(df.iloc[:-1])
        c, v, h = df.close.values, df.volume.values, df.high.values
        base = float(np.median(v[-52:-4]))
        r4 = c[-1] / c[-5] - 1
        r.update(tech={"1h": rt["rec"]}, rt={"1h": rt}, rsi=rt["rsi"], atr_pct=rt["atr_pct"],
                 vspike=round(float(np.mean(v[-4:-1])) / base, 2) if base > 0 else None,
                 r1h=round((c[-1] / c[-2] - 1) * 100, 2), r4h=round(r4 * 100, 2), rs4=round((r4 - btc_r4) * 100, 2),
                 flow3=round(float(df.tb.values[-4:-1].sum() / max(v[-4:-1].sum(), 1e-12)) - 0.5, 3),
                 breakout=bool(c[-1] > h[-25:-1].max()))
        return r

    def _radar_pass(self):
        tk = self.ex.fetch_tickers()
        bchg = (tk.get(C.BTC) or {}).get("percentage") or 0.0
        rows, n_all = [], 0
        for s, t in tk.items():
            m, base = self.ex.markets.get(s) or {}, s.split("/")[0]
            if not (m.get("spot") and m.get("active") and m.get("quote") == "USDT") or base in D.STABLE \
                    or base.endswith(LEVERAGED):
                continue
            n_all += 1
            last, chg, vol = t.get("last"), t.get("percentage"), t.get("quoteVolume") or 0
            if not last or chg is None or (vol < C.RADAR_MIN_VOL and s not in C.EXTRA_SYMBOLS):
                continue
            hi, lo = t.get("high") or last, t.get("low") or last
            if 0.97 < last < 1.03 and hi < lo * 1.01:
                continue
            rows.append({"sym": s, "price": last, "chg24": round(chg, 2), "qvol": vol, "rs24": round(chg - bchg, 2),
                         "rng": (last - lo) / (hi - lo) if hi > lo else 0.5})
        pre = sorted(rows, key=lambda r: -(min(r["rs24"], 40) / 4 + 3 * r["rng"]))[:RADAR_TOP - 10]
        pre += sorted(rows, key=lambda r: -r["qvol"])[:10] + [r for r in rows if r["sym"] in C.EXTRA_SYMBOLS]
        pick = list({r["sym"]: r for r in pre}.values())
        bdf = self._klines(C.BTC, "1h")
        btc_r4 = float(bdf.close.iloc[-1] / bdf.close.iloc[-5] - 1)
        with ThreadPoolExecutor(4) as pool:
            deep = [r for r in pool.map(lambda r: self._deep(r, btc_r4), pick) if r]
        for r in deep:
            r["score"] = radar_score(r)
        deep.sort(key=lambda r: -r["score"])
        best = deep[:RADAR_FULL] + [r for r in deep[RADAR_FULL:] if r["sym"] in C.EXTRA_SYMBOLS]
        with ThreadPoolExecutor(4) as pool:
            for r, rt in zip(best, pool.map(lambda r: self.ratings(r["sym"], ("15m", "4h", "1d")), best)):
                r["rt"].update(rt)
                r["tech"].update({k: v["rec"] for k, v in rt.items()})
                r["score"], r["full"] = radar_score(r), True
        deep.sort(key=lambda r: -r["score"])
        for r in deep:
            r["t"] = time.time()
        self.opps, self.radar_map, self.liquid = deep, {r["sym"]: r for r in deep}, rows
        self.n_all, self.n_liquid, self.t_opps = n_all, len(rows), time.time()

    def _loop(self):
        while True:
            t0 = time.time()
            try:
                self.ex.load_markets()
                self._universe_pass()
                self._radar_pass()
                self.error = ""
            except Exception as e:
                self.error = str(e)[:200]
                log.warning("teknik tarama: %s", e)
            time.sleep(max(20, 120 - (time.time() - t0)))
