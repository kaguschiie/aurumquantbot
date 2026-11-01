"""Gecmis Okulu: Binance'teki tum USDT paritelerinin 2 yillik 1s mumlari -> canli motorla ayni ozellikler ve sinyaller ->
canli cikis politikasiyla maliyet sonrasi simulasyon -> somut dersler:
  - strateji x rejim beklentisi (egitim: ilk 2/3, test: son 1/3 zaman; ikisi de negatifse o kurulum o rejimde kapanir)
  - sampiyon cikis parametreleri (stop/hedef/basabas/iz/kismi kar/zaman stopu), sadece testte de ustunse
  - sinyal ve ikili sinyal istatistikleri (binlerce ornek) -> Ders Defteri tuzak/altin kurallari ve agirlik onsel'i
  - portfoy simulasyonu: kasa egrisi vs BTC al-tut (son 1/3 = hic gorulmemis veri)
Sadece herkese acik mum verisi; emir yok, ucret yok. 15dk teknik, emir defteri, balina, fonlama mumdan uretilemez:
bu sinyaller testte yoktur (canlida ek filtre olarak kalir)."""
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import ccxt
import numpy as np
import pandas as pd

import config as C
import data as D
import teknik as T

log = logging.getLogger("bot")
LEVERAGED = ("UP", "DOWN", "BULL", "BEAR")
FILE = os.path.join(C.APP_DIR, "gecmis.json")
STATE = {"phase": "", "done": 0, "total": 0, "t": 0.0, "error": "", "running": False}
RES = {}             # son sonuc (gecmis.json): canli motor buradan okur
TF_H = 3_600_000
H_MAX = 48           # en uzun tutma (saat)
GAP_H = 4            # ayni coinde iki ornek arasi en az (ust uste binen ornekler istatistigi sisirmesin)
KINDS = ("trend", "ayrisma", "momentum", "altin", "geri_cekilme", "dip")
KIND_TR = {"trend": "Zaman dilimleri uyumlu yükseliş", "ayrisma": "BTC'den pozitif ayrışma",
           "momentum": "Momentum / kırılım avcısı", "altin": "Altın (PAXG)", "geri_cekilme": "Trend içi geri çekilme",
           "dip": "Sert düşüş sonrası dip alımı"}
REGS = ("trend", "yatay", "dusus")
VOLS = ("dusuk", "normal", "yuksek")
SIGS = ("trend_1s", "trend_4s", "zayif_4s", "gunluk_asagi", "hacim_patlamasi", "btc_ayrisma", "kirilim", "rsi_asiri",
        "rsi_dusuk", "pompa", "alici_akisi", "rejim_dusus", "rejim_trend", "altin")
CONTEXT = {"rejim_dusus", "rejim_trend", "altin"}
DEFAULT_EXIT = {"stop_atr": 1.5, "tp_atr": 3.2, "be_r": 0.4, "trail_atr": 1.0, "part_r": 1.0, "part_frac": 0.5,
                "ts_h": 0, "ts_r": 0.3, "max_h": 24}


# ---------------- veri ----------------
def symbols(ex, min_vol=1_000_000):
    """Islemdeki tum USDT spot pariteleri (sabit/kaldiracli haric), 24s hacmi min_vol uzeri; BTC ve ek pariteler dahil."""
    out = []
    for s, t in ex.fetch_tickers().items():
        m, base = ex.markets.get(s) or {}, s.split("/")[0]
        if not (m.get("spot") and m.get("active") and m.get("quote") == "USDT") or base in D.STABLE or base.endswith(LEVERAGED):
            continue
        last, hi, lo = t.get("last") or 0, t.get("high") or 0, t.get("low") or 0
        if 0.97 < last < 1.03 and hi < lo * 1.01:  # listede olmayan dolar sabitli coin
            continue
        if (t.get("quoteVolume") or 0) >= min_vol or s in C.EXTRA_SYMBOLS or s == C.BTC:
            out.append(s)
    return sorted(out)


def download(syms=None, days=C.HISTORY_DAYS, workers=2):
    """Eksik/yeni mumlari indirir (artimli, data.py onbellegi: DATA_DIR/<SEMBOL>_1h.pkl). Donus: indirilen sembol listesi."""
    exs = [D.exchange() for _ in range(workers)]  # her is parcacigi kendi baglantisi (ccxt hiz siniri baglanti basina)
    syms = syms or symbols(exs[0], 0)
    start = int(time.time() * 1000) - days * 86_400_000
    STATE.update(phase="indirme", done=0, total=len(syms), error="")
    ok, lock = [], threading.Lock()

    def one(i_sym):
        i, sym = i_sym
        ex = exs[i % workers]
        try:
            D._cache(f"{sym.replace('/', '')}_{C.TIMEFRAME}", start, lambda s: D._spot(ex, sym, s))
            with lock:
                ok.append(sym)
        except (ccxt.BaseError, ValueError, KeyError, OSError) as e:
            log.debug("gecmis indirme %s: %s", sym, e)
        with lock:
            STATE["done"] += 1

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(one, enumerate(syms)))
    return sorted(ok)


def _cached_syms():
    if not os.path.isdir(C.DATA_DIR):
        return []
    suf = f"USDT_{C.TIMEFRAME}.pkl"
    return sorted(f[:-len(suf)] + "/USDT" for f in os.listdir(C.DATA_DIR) if f.endswith(suf))


def _frame(sym, days=C.HISTORY_DAYS):
    df = pd.read_pickle(os.path.join(C.DATA_DIR, f"{sym.replace('/', '')}_{C.TIMEFRAME}.pkl"))
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df[df.index >= df.index[-1] - days * 86_400_000] if len(df) else df


# ---------------- ozellikler (canli teknik.py/quant.py tanimlariyla ayni) ----------------
def _tf_rec(df, hours):
    """Her 1s mumda biten 'hours' saatlik mumlarin teknik ozeti (ofsetli yeniden ornekleme; ileriye bakis yok)."""
    o, h, l, c, v = (df[k].values.astype(float) for k in ("open", "high", "low", "close", "volume"))
    out = np.full(len(c), np.nan)
    for off in range(0, hours, max(1, hours // 6)):  # gunlukte 4 saatte bir ofset; aradaki saatler bir oncekiyle
        m = (len(c) - off) // hours
        if m < 60:
            continue
        s = len(c) - off - m * hours  # bloklar off kadar onceki mumda biter
        blk = lambda x: x[s:s + m * hours].reshape(m, hours)
        agg = pd.DataFrame({"open": blk(o)[:, 0], "high": blk(h).max(1), "low": blk(l).min(1), "close": blk(c)[:, -1],
                            "volume": blk(v).sum(1)})
        out[s + hours - 1:s + m * hours:hours] = T.rating_series(agg)["rec"].values
    return pd.Series(out).ffill(limit=hours).values


def features(df):
    """1s mumdan canli ozellikler (her satir: o mum kapanisinda bilinen)."""
    c, h, l, v, tb = (df[k].values.astype(float) for k in ("close", "high", "low", "volume", "taker_buy"))
    rt = T.rating_series(df)
    S = lambda x: pd.Series(x)
    f = {"t": df.index.values.astype(np.int64), "c": c, "h": h, "l": l, "rec1h": rt["rec"].values,
         "rsi": rt["rsi"].values, "atr": rt["atr_pct"].values, "rec4h": _tf_rec(df, 4), "rec1d": _tf_rec(df, 24)}
    f["vspike"] = (S(v).rolling(3).mean() / S(v).shift(3).rolling(48).median()).values
    f["flow3"] = (S(tb).rolling(3).sum() / S(v).rolling(3).sum()).values - 0.5
    f["breakout"] = c > S(h).shift(1).rolling(24).max().values
    f["chg24"] = (c / S(c).shift(24).values - 1) * 100
    f["r4h"] = (c / S(c).shift(4).values - 1) * 100
    f["qv24"] = S(v * c).rolling(24).sum().values
    f["ema50"] = c / S(c).ewm(span=50, adjust=False).mean().values - 1
    f["ema200"] = c / S(c).ewm(span=200, adjust=False).mean().values - 1
    return {k: np.asarray(x, dtype=np.int64 if k == "t" else np.float32 if k not in ("c", "h", "l") else np.float64)
            for k, x in f.items()}


def _market(syms):
    """Saatlik piyasa baglami (1. gecis, hafif): BTC 24s/4s getiri, BTC ATR (volatilite), genislik (likit coinlerin
    50 EMA ustu orani), rejim (model.regime ile ayni kural)."""
    import model as M
    bdf = _frame(C.BTC)
    t0, t1 = int(bdf.index[0]), int(bdf.index[-1])
    n = (t1 - t0) // TF_H + 1
    up, cnt = np.zeros(n), np.zeros(n)
    for s in syms:
        try:
            df = _frame(s)
        except (OSError, ValueError, KeyError, EOFError):
            continue
        c = df.close.values.astype(float)
        ix = (df.index.values - t0) // TF_H
        e50 = c / pd.Series(c).ewm(span=50, adjust=False).mean().values - 1
        qv = pd.Series(df.volume.values * c).rolling(24).sum().values
        ok = (ix >= 0) & (ix < n) & (qv >= C.MIN_QUOTE_VOL)
        np.add.at(cnt, ix[ok], 1)
        np.add.at(up, ix[ok], e50[ok] > 0)
    breadth = np.where(cnt >= 10, up / np.maximum(cnt, 1), 0.5)
    bix = (bdf.index.values - t0) // TF_H
    full = lambda x: pd.Series(np.nan, index=range(n)).fillna(pd.Series(x, index=bix)).ffill().values
    bc = full(bdf.close.values.astype(float))
    r24 = np.log(bc / pd.Series(bc).shift(24).values)
    reg = M.regime(bc / pd.Series(bc).ewm(span=50, adjust=False).mean().values - 1, np.nan_to_num(r24), breadth)
    atr = full(T.rating_series(bdf)["atr_pct"].values)
    vol = np.where(atr < 0.0035, 0, np.where(atr > 0.009, 2, 1))
    return {"t0": t0, "n": n, "breadth": breadth, "btc_chg24": (bc / pd.Series(bc).shift(24).values - 1) * 100,
            "btc_r4": (bc / pd.Series(bc).shift(4).values - 1) * 100, "reg": np.array([REGS.index(x) for x in reg]),
            "vol": vol, "btc_c": bc}


def signals(f, mk, ix, gold=False):
    """Ders Defteri sinyalleri (dersler.SIGNALS ile ayni esikler; mumdan uretilebilenler) -> bit maskesi."""
    rs24 = f["chg24"] - mk["btc_chg24"][ix]
    reg = mk["reg"][ix]
    conds = {"trend_1s": f["rec1h"] >= 0.5, "trend_4s": f["rec4h"] >= 0.3, "zayif_4s": f["rec4h"] < 0.1,
             "gunluk_asagi": f["rec1d"] < -0.1, "hacim_patlamasi": f["vspike"] >= 3, "btc_ayrisma": rs24 >= 5,
             "kirilim": f["breakout"] > 0, "rsi_asiri": f["rsi"] >= 70, "rsi_dusuk": f["rsi"] < 40,
             "pompa": f["chg24"] >= 15, "alici_akisi": f["flow3"] >= 0.05, "rejim_dusus": reg == 2,
             "rejim_trend": reg == 0, "altin": np.full(len(ix), gold)}
    m = np.zeros(len(ix), dtype=np.int32)
    for b, k in enumerate(SIGS):
        m |= np.where(np.nan_to_num(conds[k]).astype(bool), 1 << b, 0).astype(np.int32)
    return m, rs24


def kinds(f, rs24, gold=False):
    """Canli quant.py kurulumlari (15dk/emir defteri kosullari mumdan uretilemez) + 2 arastirma kurulumu.
    Donus: {tur: (maske, puan)}."""
    h1, h4, d1, rsi, vs, chg = f["rec1h"], f["rec4h"], f["rec1d"], f["rsi"], f["vspike"], f["chg24"]
    with np.errstate(invalid="ignore"):
        out = {}
        if gold:
            out["altin"] = ((h1 >= 0.3) & (h4 >= 0.3), h1 + h4)
            return out
        out["trend"] = ((h1 >= 0.5) & (h4 >= 0.1) & ~(d1 < -0.1) & (rsi <= 72) & (chg <= 15),
                        h1 + 0.7 * h4 + 0.3 * np.nan_to_num(d1))
        out["ayrisma"] = ((h1 >= 0.5) & (h4 >= 0.3) & (rs24 >= 5) & (vs >= 2) & (rsi <= 75) & (chg <= 30),
                          h1 + 0.7 * h4 + np.minimum(vs, 6) / 3 + np.minimum(rs24, 30) / 10)
        out["momentum"] = ((h1 >= 0.1) & (rsi >= 50) & (rsi <= 74) & (vs >= 3) & (rs24 >= 3) & (f["flow3"] > 0),
                           h1 + 0.5 * np.nan_to_num(h4) + np.minimum(vs, 10) / 4 + np.minimum(rs24, 40) / 15
                           + 0.3 * f["breakout"])
        out["geri_cekilme"] = ((d1 >= 0.1) & (h4 >= 0.1) & (rsi < 42) & (f["ema200"] > 0) & (chg > -8),
                               d1 + h4 + (42 - rsi) / 20)
        out["dip"] = ((rsi < 25) & (chg <= -10) & (vs >= 2) & (f["flow3"] > 0), (25 - rsi) / 10 - chg / 20)
    return out


# ---------------- ornekler ve fiyat yollari ----------------
def build(syms=None, say=lambda m: None):
    """Tum coinler: ozellikler -> kurulum ornekleri (+ her ornegin sonraki 48 saatlik goreli fiyat yolu)."""
    syms = sorted(syms or _cached_syms())
    if C.BTC not in syms:
        raise ValueError("BTC verisi yok")
    STATE.update(phase="piyasa bağlamı", done=0, total=len(syms))
    mk = _market(syms)
    STATE.update(phase="özellikler")
    rows, paths, used = [], [], []
    for s in syms:  # tek tek: bellekte ayni anda tek coinin ozellikleri (dusuk RAM'li bilgisayarda da calissin)
        STATE["done"] += 1
        try:
            df = _frame(s)
            if len(df) < 400:
                continue
            f = features(df)
        except (OSError, ValueError, KeyError, EOFError) as e:
            log.debug("gecmis ozellik %s: %s", s, e)
            continue
        si = len(used)
        used.append(s)
        ix = (f["t"] - mk["t0"]) // TF_H
        ok = (ix >= 0) & (ix < mk["n"])
        if not ok.all():
            f = {k: v[ok] for k, v in f.items()}
            ix = ix[ok]
        gold = s.split("/")[0] in ("PAXG", "XAUT")
        sig, rs24 = signals(f, mk, ix, gold)
        n = len(f["c"])
        valid = (np.arange(n) >= 250) & (np.arange(n) < n - 2) & (f["qv24"] >= C.MIN_QUOTE_VOL) & np.isfinite(f["atr"])
        rel = np.stack([f["h"], f["l"], f["c"]], 1)
        last = {}
        for ki, (k, (mask, sc)) in enumerate(kinds(f, rs24, gold).items()):
            for i in np.flatnonzero(np.nan_to_num(mask).astype(bool) & valid):
                if i - last.get(k, -GAP_H) < GAP_H:
                    continue
                last[k] = i
                seg = rel[i + 1:i + 1 + H_MAX] / f["c"][i]
                if len(seg) < H_MAX:  # veri sonu: son kapanisla doldur (acik kalan pozisyon son fiyattan degerlenir)
                    seg = np.vstack([seg, np.repeat(seg[-1:, [2, 2, 2]], H_MAX - len(seg), 0)])
                paths.append(seg.astype(np.float32))
                rows.append((int(f["t"][i]), si, KINDS.index(k), int(mk["reg"][ix[i]]), int(mk["vol"][ix[i]]),
                             int(sig[i]), float(np.nan_to_num(sc[i])), float(min(max(f["atr"][i], 0.004), 0.06)),
                             float(f["chg24"][i]), float(f["rsi"][i])))
    cols = ("t", "sym", "kind", "reg", "vol", "sig", "score", "atr", "chg24", "rsi")
    E = {k: np.array([r[j] for r in rows]) for j, k in enumerate(cols)}
    E["path"] = np.stack(paths) if paths else np.zeros((0, H_MAX, 3), np.float32)
    E["syms"] = used
    say(f"Geçmiş Okulu: {len(used)} coin, {len(rows)} kurulum örneği hazırlandı.")
    return E, mk


# ---------------- vektorlu cikis simulasyonu (canli Account._manage/_shield/partial ile ayni kurallar) ----------------
def simulate(path, atr, p, taker=True):
    """path: (n, T, 3) girise gore goreli (yuksek, dusuk, kapanis); atr: (n,) ATR/fiyat. Ayni mumda once stop
    (temkinli), kalkan bir sonraki mumdan gecerli. Donus: (net R, net getiri, tutulan saat, cikis turu kodu)."""
    n, T_ = path.shape[:2]
    e = 1 + (C.SLIPPAGE if taker else 0.0)
    stop = 1 - p["stop_atr"] * atr
    r0 = e - stop
    tp = 1 + p["tp_atr"] * atr if p["tp_atr"] else np.full(n, np.inf)
    lock = np.maximum(C.BE_LOCK_PCT / 100, np.minimum(0.2 * atr, 0.02))
    bep = e * (1 + 2 * C.FEE + C.SLIPPAGE + lock)
    trig = np.maximum(p["be_r"] * r0, bep - e + e * 0.002) if p["be_r"] else np.full(n, np.inf)
    part_at = e + p["part_r"] * r0 if p.get("part_frac") else np.full(n, np.inf)
    sl, hi = stop.copy(), np.full(n, e)
    be, done = np.zeros(n, bool), np.zeros(n, bool)
    qty, realized = np.ones(n), np.zeros(n)
    out, held, why = np.zeros(n), np.full(n, float(min(T_, p["max_h"]))), np.zeros(n, np.int8)
    prev_c = np.ones(n)
    for t in range(min(T_, p["max_h"])):
        h, low, c = path[:, t, 0], path[:, t, 1], path[:, t, 2]
        hit = ~done & (low <= sl)                          # stop (bosluk varsa onceki kapanistan / acilistan)
        px = np.minimum(sl, prev_c) * (1 - C.SLIPPAGE)
        out[hit], held[hit], done[hit] = px[hit], t + 1, True
        why[hit] = np.where(sl[hit] > bep[hit] * 1.001, 3, np.where(sl[hit] >= e, 2, 1))  # 1 stop 2 basabas 3 iz
        pp = ~done & (h >= part_at)                        # kademeli kar
        if pp.any():
            f = p["part_frac"]
            realized[pp] += f * (part_at[pp] * (1 - C.FEE) - e)
            qty[pp] -= f
            part_at[pp] = np.inf
            sl[pp], be[pp] = np.maximum(sl[pp], bep[pp]), True
        ht = ~done & (h >= tp)                             # hedef
        out[ht], held[ht], done[ht], why[ht] = tp[ht], t + 1, True, 4
        hi = np.maximum(hi, h)
        nb = ~done & ~be & (hi - e >= trig)                # sifir zarar kalkani
        sl[nb], be[nb] = np.maximum(sl[nb], bep[nb]), True
        tr = np.maximum(hi - p["trail_atr"] * atr, np.where(hi - e >= r0, e + C.LOCK_FRAC * (hi - e), -np.inf))
        sl = np.where(~done & be, np.maximum(sl, np.minimum(tr, c * 0.998)), sl)
        if p["ts_h"] and t + 1 == p["ts_h"]:              # zaman stopu: N saatte ilerlemeyen pozisyon
            ts = ~done & (hi - e < p["ts_r"] * r0)
            out[ts], held[ts], done[ts], why[ts] = c[ts], t + 1, True, 5
        prev_c = c
    rest = ~done                                           # sure doldu: son kapanistan
    out[rest] = path[rest, min(T_, p["max_h"]) - 1, 2]
    why[rest] = 6
    ret = realized + qty * (out * (1 - C.FEE) - e) - C.FEE * e
    return ret / r0, ret, held, why


EXIT_TR = {1: "STOP", 2: "BASABAS", 3: "IZ_STOP", 4: "HEDEF", 5: "ZAMAN_STOPU", 6: "SURE_DOLDU"}


def stats(r, w=None):
    r = np.asarray(r, float)
    if not len(r):
        return {"n": 0}
    win, loss = r[r > 0], r[r <= 0]
    return {"n": int(len(r)), "avg": round(float(r.mean()), 4), "win": round(float((r > 0).mean()), 3),
            "avg_win": round(float(win.mean()), 3) if len(win) else 0.0,
            "avg_loss": round(float(loss.mean()), 3) if len(loss) else 0.0,
            "pf": round(float(win.sum() / -loss.sum()), 3) if loss.sum() < 0 else None, "sum": round(float(r.sum()), 1)}


# ---------------- arama ----------------
GRID = {"stop_atr": (1.0, 1.5, 2.0, 2.5, 3.0), "tp_atr": (2.0, 3.2, 4.5, 6.0, 0), "be_r": (0, 0.4, 0.7, 1.0, 1.5),
        "trail_atr": (1.0, 1.5, 2.0, 3.0), "part_frac": (0, 0.33, 0.5), "part_r": (1.0, 1.5, 2.0),
        "ts_h": (0, 4, 8, 12), "ts_r": (0.2, 0.5), "max_h": (12, 24, 48)}


def search(path, atr, taker, p0=None, rounds=2):
    """Koordinat inisi: her turda her parametrenin tum degerleri denenir, egitim verisinde ort. net R en iyi olan
    secilir. Az parametre + ayri test donemi = asiri uyuma karsi."""
    p = dict(p0 or DEFAULT_EXIT)
    best = float(simulate(path, atr, p, taker)[0].mean())
    for _ in range(rounds):
        moved = False
        for k, vals in GRID.items():
            for v in vals:
                if v == p[k]:
                    continue
                q = {**p, k: v}
                if q["tp_atr"] and q["tp_atr"] < 1.2 * q["stop_atr"]:
                    continue
                m = float(simulate(path, atr, q, taker)[0].mean())
                if m > best + 0.005:
                    best, p, moved = m, q, True
        if not moved:
            break
    return p, best


def _rules(E, R, train, test, base):
    """Tekli ve ikili sinyal istatistikleri (egitim/test); her iki donemde de tutarli olanlar kural olur."""
    out, n_s = [], len(SIGS)
    bits = [(E["sig"] >> b) & 1 for b in range(n_s)]
    keys = [(a,) for a in range(n_s)] + [(a, b) for a in range(n_s) for b in range(a + 1, n_s)]
    for key in keys:
        m = np.ones(len(R), bool)
        for b in key:
            m &= bits[b].astype(bool)
        tr, te = R[m & train], R[m & test]
        if len(tr) < 150 or len(te) < 50:
            continue
        sigs = [SIGS[b] for b in key]
        row = {"sigs": sigs, "n": int(len(tr) + len(te)), "train": stats(tr), "test": stats(te),
               "avg": round(float(R[m].mean()), 3), "win": round(float((R[m] > 0).mean()), 3)}
        a1, a2 = row["train"]["avg"], row["test"]["avg"]
        single_ctx = len(key) == 1 and sigs[0] in CONTEXT
        if not single_ctx and a1 <= base - 0.15 and a1 <= -0.15 and a2 <= -0.05:
            row["kind"] = "tuzak"
        elif not single_ctx and a1 >= max(base + 0.1, 0.12) and a2 >= 0.08:
            row["kind"] = "altin"
        else:
            row["kind"] = "bilgi"
        out.append(row)
    return out


def portfolio(E, R, ret, held, mask, risk_pct=1.0, max_pos=C.MAX_POSITIONS):
    """Kronolojik portfoy: her saatte en yuksek puanli kurulumlar, coin basina tek pozisyon, en fazla max_pos acik.
    Islem basi risk kasanin risk_pct'si (R x risk). Donus: kasa egrisi ozeti."""
    idx = np.flatnonzero(mask)
    idx = idx[np.lexsort((-E["score"][idx], E["t"][idx]))]
    eq, open_until, busy, curve, n = 1.0, [], {}, [], 0
    for i in idx:
        t = int(E["t"][i])
        open_until = [x for x in open_until if x > t]
        s = int(E["sym"][i])
        if len(open_until) >= max_pos or busy.get(s, 0) > t:
            continue
        end = t + int(held[i]) * TF_H
        open_until.append(end)
        busy[s] = end + 3_600_000  # cikistan sonra 60 dk bekleme (canli COOLDOWN_MIN)
        eq *= 1 + risk_pct / 100 * float(R[i])
        n += 1
        curve.append((t, eq))
    return {"n": n, "ret_pct": round((eq - 1) * 100, 2), "curve": curve}


# ---------------- ana is ----------------
def run(say=lambda m: None, fetch=True):
    """Tam Gecmis Okulu: indir -> ornekle -> her kurulum icin cikis parametresi ara (egitim) -> test donemiyle dogrula ->
    kurallar + portfoy -> gecmis.json. Uzun surer (ilk sefer ~15-25 dk); arka planda calisir."""
    if STATE["running"]:
        return "zaten çalışıyor"
    STATE.update(running=True, error="", t=time.time())
    try:
        if fetch:
            download()
        E, mk = build(say=say)
        STATE.update(phase="arama", done=0, total=len(KINDS))
        cut = int(np.quantile(E["t"], 2 / 3)) if len(E["t"]) else 0
        train, test = E["t"] < cut, E["t"] >= cut
        res = {"t": time.time(), "n_syms": len(E["syms"]), "n": int(len(E["t"])), "cut": cut,
               "span": [int(E["t"].min()), int(E["t"].max())] if len(E["t"]) else None, "kinds": {}}
        R_all, ret_all, held_all, why_all = (np.zeros(len(E["t"])) for _ in range(4))
        for ki, k in enumerate(KINDS):
            m = E["kind"] == ki
            STATE["done"] = ki
            if m.sum() < 100:
                continue
            taker = k in ("momentum", "ayrisma", "dip")
            p0 = DEFAULT_EXIT
            r0, *_ = simulate(E["path"][m], E["atr"][m], p0, taker)
            tr_m = train[m]
            p, _ = search(E["path"][m & train], E["atr"][m & train], taker)
            r1, ret1, held1, why1 = simulate(E["path"][m], E["atr"][m], p, taker)
            better = r1[~tr_m].mean() > r0[~tr_m].mean() + 0.02  # yeni parametre test doneminde de ustun mu?
            use = p if better else p0
            r, rt, hd, wy = (r1, ret1, held1, why1) if better else simulate(E["path"][m], E["atr"][m], p0, taker)
            R_all[m], ret_all[m], held_all[m], why_all[m] = r, rt, hd, wy
            by_reg = {}
            for gi, g in enumerate(REGS):
                mm = E["reg"][m] == gi
                by_reg[g] = {"train": stats(r[mm & tr_m]), "test": stats(r[mm & ~tr_m])}
                a, b = by_reg[g]["train"].get("avg"), by_reg[g]["test"].get("avg")
                # izin sadece KANITLA: egitimde de, hic gorulmemis test doneminde de maliyet sonrasi pozitif
                by_reg[g]["allowed"] = a is not None and b is not None and a > 0 and b > 0 and \
                    by_reg[g]["test"]["n"] >= 100  # az ornekli 'kanit' sans olabilir
            exits = {EXIT_TR[c]: int((wy == c).sum()) for c in EXIT_TR}
            res["kinds"][k] = {"name": KIND_TR[k], "taker": taker, "params": use, "searched": p, "promoted": bool(better),
                               "default": {"train": stats(r0[tr_m]), "test": stats(r0[~tr_m])},
                               "train": stats(r[tr_m]), "test": stats(r[~tr_m]), "by_regime": by_reg, "exits": exits}
            say(f"Geçmiş Okulu: {KIND_TR[k]} — test dönemi ort. {res['kinds'][k]['test'].get('avg', 0):+.2f}R "
                f"({res['kinds'][k]['test'].get('n', 0)} örnek, eski çıkışla {res['kinds'][k]['default']['test'].get('avg', 0):+.2f}R)")
        STATE.update(phase="kurallar")
        base = float(R_all[train].mean()) if train.any() else 0.0
        res["base"] = round(base, 3)
        res["rules"] = _rules(E, R_all, train, test, base)
        allowed = np.zeros(len(E["t"]), bool)
        for ki, k in enumerate(KINDS):
            kk = res["kinds"].get(k)
            if kk:
                for gi, g in enumerate(REGS):
                    allowed |= (E["kind"] == ki) & (E["reg"] == gi) & kk["by_regime"][g]["allowed"]
        traps = [r for r in res["rules"] if r["kind"] == "tuzak"]
        veto = np.zeros(len(E["t"]), bool)
        for rl in traps:
            mm = np.ones(len(E["t"]), bool)
            for s in rl["sigs"]:
                mm &= ((E["sig"] >> SIGS.index(s)) & 1).astype(bool)
            veto |= mm
        bc = mk["btc_c"]
        bh = lambda a, b: round((bc[min(max((b - mk["t0"]) // TF_H, 0), mk["n"] - 1)] /
                                 bc[min(max((a - mk["t0"]) // TF_H, 0), mk["n"] - 1)] - 1) * 100, 2)
        pf = {}
        for name, mask in (("eski", np.ones(len(E["t"]), bool)), ("yeni", allowed & ~veto)):
            for per, pm in (("tum", np.ones(len(E["t"]), bool)), ("test", test)):
                if name == "eski":  # eski politika: varsayilan cikis + tum kurulumlar (geri cekilme/dip haric)
                    m2 = pm & np.isin(E["kind"], [KINDS.index(x) for x in ("trend", "ayrisma", "momentum", "altin")])
                    r_old = np.zeros(len(E["t"]))
                    h_old = np.zeros(len(E["t"]))
                    for ki in range(len(KINDS)):
                        mm = E["kind"] == ki
                        if mm.any():
                            a, _, hh, _ = simulate(E["path"][mm], E["atr"][mm], DEFAULT_EXIT,
                                                   KINDS[ki] in ("momentum", "ayrisma", "dip"))
                            r_old[mm], h_old[mm] = a, hh
                    pf[f"{name}_{per}"] = portfolio(E, r_old, None, h_old, m2)
                else:
                    pf[f"{name}_{per}"] = portfolio(E, R_all, ret_all, held_all, pm & mask)
        span = res["span"] or [0, 0]
        res["portfolio"] = {k: {kk: vv for kk, vv in v.items() if kk != "curve"} for k, v in pf.items()}
        res["portfolio"]["btc_tum"], res["portfolio"]["btc_test"] = bh(span[0], span[1]), bh(cut, span[1])
        res["curve"] = [[t, round(e, 4)] for t, e in pf["yeni_tum"]["curve"][::max(1, len(pf["yeni_tum"]["curve"]) // 400)]]
        res["curve_old"] = [[t, round(e, 4)] for t, e in pf["eski_tum"]["curve"][::max(1, len(pf["eski_tum"]["curve"]) // 400)]]
        save(res)
        apply(res)
        STATE.update(phase="tamam", done=len(KINDS))
        say(f"Geçmiş Okulu tamamlandı: {res['n']} örnek, {len(traps)} tuzak, "
            f"{sum(1 for r in res['rules'] if r['kind'] == 'altin')} altın kural. Yeni politika test döneminde "
            f"%{res['portfolio']['yeni_test']['ret_pct']:+.1f} (eski %{res['portfolio']['eski_test']['ret_pct']:+.1f}, "
            f"BTC al-tut %{res['portfolio']['btc_test']:+.1f}).")
        return None
    except Exception as e:
        STATE["error"] = str(e)[:300]
        log.exception("gecmis okulu")
        return STATE["error"]
    finally:
        STATE["running"] = False


def save(res):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, "item") else str(o))
    os.replace(tmp, FILE)


def load():
    try:
        with open(FILE, encoding="utf-8") as fh:
            apply(json.load(fh))
    except (OSError, ValueError):
        pass


_IDX = {}


def apply(res):
    RES.clear()
    RES.update(res)
    _IDX.clear()
    _IDX.update({tuple(sorted(r["sigs"])): r for r in res.get("rules") or []})


# ---------------- canli motorun okudugu dersler ----------------
def sig_stat(keys):
    """Sinyal (veya ikili) icin 2 yillik istatistik: {avg, n, win, kind} | None."""
    return _IDX.get(tuple(sorted(keys)))


def kind_stat(kind, regime):
    """Kurulum x rejim test donemi sonucu: {avg, n, allowed} | None."""
    k = (RES.get("kinds") or {}).get(kind)
    g = (k or {}).get("by_regime", {}).get(regime) if regime else None
    if not g or not g["test"].get("n"):
        return None
    return {"avg": g["test"]["avg"], "n": g["test"]["n"], "allowed": g["allowed"]}


def allowed(kind, regime):
    """Gecmis Okulu bu kurulumu bu rejimde aciyor mu? Donus: (izin, sebep). Okul hic calismadiysa izin (eski davranis)."""
    if not RES.get("kinds"):
        return True, ""
    ks = kind_stat(kind, regime)
    if ks is None:
        k = RES["kinds"].get(kind)
        return (True, "") if k is None else (False, f"Geçmiş Okulu: {KIND_TR.get(kind, kind)} bu rejimde yeterli kanıt yok")
    if not ks["allowed"]:
        return False, (f"Geçmiş Okulu: {KIND_TR.get(kind, kind)} kurulumu {regime} rejiminde 2 yılda zarar ettirdi "
                       f"(test {ks['avg']:+.2f}R, {ks['n']} örnek)")
    return True, f"Geçmiş Okulu: bu rejimde test {ks['avg']:+.2f}R ({ks['n']} örnek)"


def exit_params(kind):
    """Kurulumun sampiyon cikis parametreleri (okul calismadiysa varsayilan)."""
    k = (RES.get("kinds") or {}).get(kind)
    return {**DEFAULT_EXIT, **((k or {}).get("params") or {})}


def traps():
    """2 yilda egitim ve testte tutarli zarar ettiren sinyal kombinasyonlari (Ders Defteri tuzaklarina eklenir)."""
    return [r for r in RES.get("rules") or [] if r.get("kind") == "tuzak"]


def golden():
    return [r for r in RES.get("rules") or [] if r.get("kind") == "altin"]


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    t0 = time.time()
    print(run(print, fetch=False), f"{time.time() - t0:.0f} sn")
