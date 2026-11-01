"""Gunluk Trend Kolu (Donchian-20 kirilimi, Turtle tarzi). Her gun UTC 00:00 gunluk mum kapanisindan sonra:
- Evren: son 30 tamamlanmis gunun medyan gunluk USDT hacmine gore en likit 30 coin (24s hacim >= 3M, >= 60 gun gecmis;
  sabit/altin/kaldiracli haric).
- Rejim: BTC gunluk kapanis > BTC gunluk EMA50 degilse yeni giris yok (acik pozisyonlar kurala gore yonetilir).
- Giris: gunluk kapanis > onceki 20 gunun en yuksegi. Ayni gun birden fazlaysa 20 gunluk getirisi yuksek olan once.
- Stop: giris - 2 x ATR14 (gunluk, sabit, iz surmez). Cikis: gunluk kapanis < onceki 10 gunun en dusuk kapanisi veya
  90 gun. Hedef, kismi kar, basabas yok: kar birkac buyuk trendden gelir; erken kesmek kari siler.
Kanit (arastirma, maliyet dahil): 2 yil egitim islem basi +%2,2 PF 1,34, test +%1,6 PF 1,27; hic gorulmemis 2023-24
on donemi +%6 PF 1,9. Zayif-orta kanit, BTC al-tutu gecmedi. Bu yuzden once sanal hesapta: gercek hesap ancak sanalda
30+ islem ve ortalama R > 0 olunca acilir."""
import time

import numpy as np

import config as C
import data as D

KIND = "gunluk_trend"
N_BREAK, N_EXIT, ATR_K, TOP, MAX_DAYS, RISK, MAX_POS_PCT = 20, 10, 2.0, 30, 90, 0.5, 20.0
EXIT = {"be_r": 0, "trail_atr": 1.0, "part_r": 1.0, "part_frac": 0, "ts_h": 0, "ts_r": 0.3, "max_h": MAX_DAYS * 24}
LIVE_MIN_N = 30
EXCLUDE = ("PAXG", "XAUT")


def atr(h, low, c, n=14):
    """Wilder ATR (data.atr ile ayni)."""
    pc = np.concatenate([[c[0]], c[:-1]])
    tr = np.maximum(h - low, np.maximum(abs(h - pc), abs(low - pc)))
    out = np.empty(len(tr))
    out[0] = tr[0]
    for i in range(1, len(tr)):
        out[i] = out[i - 1] + (tr[i] - out[i - 1]) / n
    return out


def ema(x, span):
    a, out = 2 / (span + 1), np.empty(len(x))
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = out[i - 1] + a * (x[i] - out[i - 1])
    return out


def entry_signal(d):
    """d: tamamlanmis gunluk mumlar (n, 5) acilis/yuksek/dusuk/kapanis/USDT hacmi. Son satir dun kapanan gun.
    Donus: None veya {close, stop, atr, hi20, roc20}."""
    if len(d) < 60:
        return None
    h, low, c = d[:, 1], d[:, 2], d[:, 3]
    hi = h[-N_BREAK - 1:-1].max()
    if not c[-1] > hi:
        return None
    a = atr(h, low, c)[-1]
    return {"close": float(c[-1]), "stop": float(c[-1] - ATR_K * a), "atr": float(a), "hi20": float(hi),
            "roc20": float(c[-1] / c[-N_BREAK - 1] - 1)}


def exit_signal(d):
    """Gunluk kapanis onceki 10 gunun en dusuk kapanisinin altinda mi?"""
    c = d[:, 3]
    return len(c) > N_EXIT and c[-1] < c[-N_EXIT - 1:-1].min()


def regime_ok(btc_d):
    c = btc_d[:, 3]
    return len(c) >= 60 and c[-1] > ema(c, 50)[-1]


def daily(ex, sym, limit=120):
    """Tamamlanmis gunluk mumlar (olusan gun atilir)."""
    rows = ex.public_get_klines({"symbol": sym.replace("/", ""), "interval": "1d", "limit": limit})
    now = time.time() * 1000
    return np.array([[float(r[i]) for i in (1, 2, 3, 4, 7)] for r in rows if int(r[0]) + 86_400_000 <= now])


def universe(ex):
    """En likit 30: 24s hacme gore ilk 60 aday, sonra 30 gunluk medyan gunluk hacimle siralama."""
    out = []
    for s, t in ex.fetch_tickers().items():
        m, base = ex.markets.get(s) or {}, s.split("/")[0]
        if not (m.get("spot") and m.get("active") and m.get("quote") == "USDT") or base in D.STABLE \
                or base.endswith(("UP", "DOWN", "BULL", "BEAR")) or base in EXCLUDE:
            continue
        last, hi, lo = t.get("last") or 0, t.get("high") or 0, t.get("low") or 0
        if 0.97 < last < 1.03 and hi < lo * 1.01:
            continue
        if (t.get("quoteVolume") or 0) >= C.MIN_QUOTE_VOL:
            out.append((t["quoteVolume"], s))
    return [s for _, s in sorted(out, reverse=True)[:60]]


def scan(ex):
    """Gunun karari: rejim, en likit 30, giris sinyalleri. Ag cagrisi ~62 kucuk istek (gunde bir)."""
    btc = daily(ex, C.BTC, 200)
    rows = {}
    for s in universe(ex):
        try:
            d = daily(ex, s, 120)
        except Exception:  # tek coin hatasi taramayi durdurmasin
            continue
        if len(d) >= 60:
            rows[s] = d
    ranked = sorted(rows, key=lambda s: -float(np.median(rows[s][-30:, 4])))[:TOP]
    buys = []
    for s in ranked:
        sig = entry_signal(rows[s])
        if sig:
            buys.append({"sym": s, **sig})
    buys.sort(key=lambda b: -b["roc20"])
    btc_c, btc_e = float(btc[-1, 3]), float(ema(btc[:, 3], 50)[-1])
    return {"t": time.time(), "regime_ok": regime_ok(btc), "btc_close": btc_c, "btc_ema50": btc_e, "universe": ranked,
            "buys": buys, "rows": rows}


def track(trades):
    """Sanal hesaptaki karne: gercek hesabin acilma sarti (30+ islem, ortalama R > 0)."""
    rs = [t["R"] for t in trades if t.get("kind") == KIND and t.get("R") is not None]
    avg = sum(rs) / len(rs) if rs else None
    return {"n": len(rs), "avg_r": avg, "live_ok": len(rs) >= LIVE_MIN_N and avg is not None and avg > 0}
