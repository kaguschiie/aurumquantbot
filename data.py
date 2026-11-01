"""Veri: Binance spot mumlari (alici akisi dahil) + Bybit vadeli verisi (fonlama, prim, OI, long/short).
Egitim ve canli karar ayni onbellekli yukleyiciyi kullanir: ikisi birebir ayni veriyi gorur."""
import os
import time

import ccxt
import numpy as np
import pandas as pd
import requests

import config as C

TF_MS = ccxt.Exchange.parse_timeframe(C.TIMEFRAME) * 1000
BYBIT = "https://api.bybit.com/v5/market/"
# tur: (endpoint, sayfa limiti, ek parametre, bitis parametresi, satir -> (ms, deger))
BYBIT_EP = {
    "funding": ("funding/history", 200, {}, "endTime",
                lambda r: (int(r["fundingRateTimestamp"]), float(r["fundingRate"]))),
    "premium": ("premium-index-price-kline", 1000, {"interval": "60"}, "end", lambda r: (int(r[0]), float(r[4]))),
    "oi": ("open-interest", 200, {"intervalTime": "1h"}, "endTime",
           lambda r: (int(r["timestamp"]), float(r["openInterest"]))),
    "lsr": ("account-ratio", 500, {"period": "1h"}, "endTime", lambda r: (int(r["timestamp"]), float(r["buyRatio"]))),
}
COLS = ["open", "high", "low", "close", "volume", "taker_buy", "trades"]
MAX_AGE = {"funding": 9 * 3_600_000, "premium": 2 * 3_600_000, "oi": 2 * 3_600_000, "lsr": 2 * 3_600_000}


def exchange(auth=False):
    params = {"enableRateLimit": True}
    if auth:
        params.update(apiKey=C.API_KEY, secret=C.API_SECRET)
    ex = ccxt.binance(params)
    ex.load_markets()
    return ex


STABLE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "EURI", "USD1", "PYUSD", "AEUR", "XUSD", "USDE",
          "BFUSD", "RLUSD", "WBTC", "WBETH", "BNSOL"}  # sabit/sarili varliklar (PAXG/XAUT altin: islem gorur)


def bybit_perps():
    """Bybit USDT vadeli sembolleri: vadeli verisi (fonlama/OI) olan gercek kripto varliklar."""
    out, cursor = set(), ""
    while True:
        r = requests.get(BYBIT + "instruments-info", params={"category": "linear", "limit": 1000, "cursor": cursor},
                         timeout=15).json()["result"]
        out |= {x["symbol"] for x in r["list"] if x["quoteCoin"] == "USDT"}
        cursor = r.get("nextPageCursor")
        if not cursor:
            return out


def universe(ex):
    """24s USDT hacmine gore en likit spot pariteler; Bybit vadelisi olanlar (tokenize hisseler elenir). BTC dahil."""
    rows, perps = [], bybit_perps()
    for s, t in ex.fetch_tickers().items():
        m = ex.markets.get(s, {})
        base = s.split("/")[0]
        if (m.get("spot") and m.get("active") and m.get("quote") == "USDT" and base not in STABLE and base + "USDT" in perps
                and not base.endswith(("UP", "DOWN", "BULL", "BEAR")) and (t.get("quoteVolume") or 0) >= C.MIN_QUOTE_VOL):
            rows.append((t["quoteVolume"], s))
    top = [s for _, s in sorted(rows, reverse=True)[:C.UNIVERSE_SIZE]]
    return [C.BTC] + [s for s in top if s != C.BTC] if top else list(C.SYMBOLS)


def _cache(name, old_start, fetch_new):
    """Diskte onbellek + artimli guncelleme. fetch_new(since_ms) -> DataFrame (ms indeksli)."""
    os.makedirs(C.DATA_DIR, exist_ok=True)
    path = os.path.join(C.DATA_DIR, name + ".pkl")
    old = pd.read_pickle(path) if os.path.exists(path) else None
    # son 3 mum yeniden cekilir: onceki cagrida henuz olusmakta olan degerler kesinlesmis haliyle yazilir
    since = int(old.index[-1]) - 3 * TF_MS if old is not None and len(old) else old_start
    new = fetch_new(since)
    df = new if old is None else pd.concat([old, new])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_pickle(path)
    return df


def _spot(ex, sym, since):
    now, rows = int(time.time() * 1000), []
    while since < now:
        batch = ex.public_get_klines({"symbol": ex.market(sym)["id"], "interval": C.TIMEFRAME,
                                      "startTime": since, "limit": 1000})
        if not batch:
            break
        rows += [[int(r[0])] + [float(r[i]) for i in (1, 2, 3, 4, 5, 9, 8)] for r in batch]
        since = int(batch[-1][0]) + 1
        if len(batch) < 1000:
            break
    df = pd.DataFrame(rows, columns=["t"] + COLS).set_index("t")
    return df[df.index + TF_MS <= now]  # olusan mumu at


def _bybit(sym, kind, since):
    """Bybit listeleri yeniden eskiye gelir: bitis zamanini geri kaydirarak sayfalanir."""
    path, limit, extra, end_key, parse = BYBIT_EP[kind]
    end, out = int(time.time() * 1000), {}
    while end > since:
        r = requests.get(BYBIT + path, params={"category": "linear", "symbol": sym.replace("/", ""), "limit": limit,
                                               end_key: end, **extra}, timeout=15).json()
        rows = (r.get("result") or {}).get("list") or []
        if r.get("retCode") != 0 or not rows:
            break
        pts = [parse(x) for x in rows]
        out.update(pts)
        oldest = min(t for t, _ in pts)
        if oldest >= end:
            break
        end = oldest - 1
        time.sleep(0.05)
    return pd.DataFrame({kind: pd.Series(out, dtype=float)}).loc[lambda d: d.index >= since]


def load(ex, sym):
    """Sembolun tum gecmisi: spot mumlar + vadeli veriler, mum kapanisinda bilinen degerlerle hizali."""
    start = int(time.time() * 1000) - C.HISTORY_DAYS * 86_400_000
    key = sym.replace("/", "")
    df = _cache(f"{key}_{C.TIMEFRAME}", start, lambda s: _spot(ex, sym, s))
    close_ms = df.index + TF_MS - 1  # sadece mum kapanmadan once bilinen deger kullanilir
    for kind in BYBIT_EP:
        try:
            s = _cache(f"{key}_{kind}", start, lambda since, k=kind: _bybit(sym, k, since))[kind]
            idx = s.index.union(close_ms)
            vals = s.reindex(idx).ffill().reindex(close_ms).values
            # kaynak kesildiyse son deger sonsuza tasinmasin: fonlama 9 sa, digerleri 2 sa'ten eskiyse bos (NaN)
            src = pd.Series(s.index, index=s.index, dtype=float).reindex(idx).ffill().reindex(close_ms).values
            df[kind] = np.where((close_ms.values - src) <= MAX_AGE[kind], vals, np.nan)  # .values salt okunur olabilir
        except (requests.RequestException, ValueError, KeyError):
            df[kind] = np.nan  # vadeli veri yoksa model NaN ile calisabilir
    df.index = pd.to_datetime(df.index, unit="ms", utc=True)
    return df


def atr(df, n=14):
    pc = df.close.shift()
    tr = pd.concat([df.high - df.low, (df.high - pc).abs(), (df.low - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn)


def zscore(s, n):
    return (s - s.rolling(n).mean()) / s.rolling(n).std()


def features(df, btc):
    """Sadece olcekten bagimsiz ozellikler: model tum coinlerde ortak ogrenir."""
    c, f = df.close, pd.DataFrame(index=df.index)
    a = atr(df)
    lr = np.log(c).diff()
    for k in (1, 3, 6, 12, 24, 72):
        f[f"r{k}"] = np.log(c / c.shift(k))
    f["atr_pct"] = a / c
    f["vol24"] = lr.rolling(24).std()
    f["vol_ratio"] = f.vol24 / lr.rolling(168).std()
    for n in (20, 50, 200):
        f[f"ema{n}"] = c / c.ewm(span=n, adjust=False).mean() - 1
    f["ema50_slope"] = c.ewm(span=50, adjust=False).mean().pct_change(12)
    f["rsi"] = rsi(c)
    hi, lo = df.high.rolling(48).max(), df.low.rolling(48).min()
    f["range_pos"] = (c - lo) / (hi - lo)
    f["vol_z"] = zscore(np.log1p(df.volume), 48)
    f["body"] = (c - df.open) / a
    f["low_wick"] = (np.minimum(df.open, c) - df.low) / a
    prev_lo = df.low.shift().rolling(24).min()
    f["sweep"] = ((df.low < prev_lo) & (c > prev_lo)).astype(float)  # likidite avi
    f["hour"] = df.index.hour
    # Alici/satici akisi (agresif alim payi, kumulatif delta)
    delta = 2 * df.taker_buy - df.volume
    f["flow1"] = df.taker_buy / df.volume - 0.5
    f["flow6"] = delta.rolling(6).sum() / df.volume.rolling(6).sum()
    f["flow24"] = delta.rolling(24).sum() / df.volume.rolling(24).sum()
    f["trades_z"] = zscore(np.log1p(df.trades), 48)
    # Mikro-yapi: hacim ivmesi ve volatilite sikismasi (Bollinger genisliginin 120 mumluk yuzdelik sirasi)
    f["vol_accel"] = df.volume.rolling(3).mean() / df.volume.rolling(24).mean()
    bbw = c.rolling(20).std() / c.rolling(20).mean()
    f["squeeze"] = bbw.rolling(120).rank(pct=True)
    f["atr_ratio"] = a / atr(df, 100)
    # Vadeli: kaldirac kalabaligi ve konumlanma
    f["funding"] = df.funding
    f["funding24"] = df.funding.rolling(24).mean()
    f["premium"] = df.premium
    f["premium_z"] = zscore(df.premium, 72)
    oi = np.log(df.oi)
    for k in (1, 6, 24):
        f[f"oi{k}"] = oi.diff(k)
    f["oi_z"] = zscore(oi, 168)
    f["lsr"] = df.lsr
    f["lsr24"] = df.lsr.diff(24)
    # Piyasa rejimi (BTC)
    b = btc.close.reindex(df.index)
    for k in (1, 6, 24):
        f[f"btc_r{k}"] = np.log(b / b.shift(k))
    f["btc_ema50"] = b / b.ewm(span=50, adjust=False).mean() - 1
    f["btc_ema200"] = b / b.ewm(span=200, adjust=False).mean() - 1
    f["btc_flow24"] = ((2 * btc.taker_buy - btc.volume).rolling(24).sum() / btc.volume.rolling(24).sum()).reindex(df.index)
    f["btc_funding"] = btc.funding.reindex(df.index)
    return f.replace([np.inf, -np.inf], np.nan)


def labels(df):
    """Her mum icin iki cikis politikasinin sonucu.
    y: once +TP_ATR mi (1) yoksa -SL_ATR/sure mi (0) -> modelin ogrendigi hedef.
    ret/held/clean: sabit cikis (TP/SL/sure) brut getirisi, tutulan mum, temiz (limit) cikis mi.
    ret_s/held_s/clean_s: akilli cikis (kismi kar + basabas + iz suren stop + zaman asimi)."""
    c, h, l = df.close.values, df.high.values, df.low.values
    a = atr(df).values
    n = len(df)
    out = {k: np.full(n, np.nan) for k in ("y", "ret", "held", "clean", "ret_s", "held_s", "clean_s")}
    be_mult = 1 + 2 * C.FEE
    for i in range(n - C.MAX_BARS):
        c0, ai = c[i], a[i]
        # --- sabit ---
        tp, sl = c0 + C.TP_ATR * ai, c0 - C.SL_ATR * ai
        y, r, hd, cl = 0, c[i + C.HORIZON] / c0 - 1, C.HORIZON, 0
        for j in range(i + 1, i + C.HORIZON + 1):
            if l[j] <= sl:  # ayni mumda ikisi de varsa stop varsayilir (temkinli)
                r, hd = sl / c0 - 1, j - i
                break
            if h[j] >= tp:
                y, r, hd, cl = 1, tp / c0 - 1, j - i, 1
                break
        out["y"][i], out["ret"][i], out["held"][i], out["clean"][i] = y, r, hd, cl
        # --- akilli ---
        stop, part, hi, r_part = sl, False, c0, 0.0
        r_rest, hd, cl = c[i + C.MAX_BARS] / c0 - 1, C.MAX_BARS, 0
        for j in range(i + 1, i + C.MAX_BARS + 1):
            if l[j] <= stop:
                r_rest, hd = stop / c0 - 1, j - i
                break
            if not part and h[j] >= c0 + C.PARTIAL_ATR * ai:
                part, r_part, stop = True, C.PARTIAL_ATR * ai / c0, max(stop, c0 * be_mult)
            if part:
                if h[j] >= c0 + C.FINAL_TP_ATR * ai:
                    r_rest, hd, cl = C.FINAL_TP_ATR * ai / c0, j - i, 1
                    break
                hi = max(hi, h[j])
                stop = max(stop, hi - C.TRAIL_ATR * ai)
            elif j - i >= C.DECAY_BARS and c[j] < c0 + 0.25 * ai:
                r_rest, hd = c[j] / c0 - 1, j - i
                break
        f = C.PARTIAL_FRAC
        out["ret_s"][i] = f * r_part + (1 - f) * r_rest if part else r_rest
        out["held_s"][i], out["clean_s"][i] = hd, cl
    return out
