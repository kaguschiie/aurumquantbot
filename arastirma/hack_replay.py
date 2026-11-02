"""Gercek sinav: 29 Ekim Binance hack'inde haber kurallari ne yapardi? Gercek basliklar (zamanlariyla) + gercek 5 dk mumlar.
Claude puani varsayimi: olayin ilk haberleri kritik/-0.95 (bu parca varsayim, gercek Claude cevabi degil)."""
import calendar, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ccxt, feedparser, requests
import haberler as HB

ex = ccxt.binance()
rows = []
for src, url in HB.FEEDS.items():
    try:
        for e in feedparser.parse(requests.get(url, headers=HB.UA, timeout=15).content).entries:
            p = e.get("published_parsed")
            c = HB.classify(HB._clean(e.get("title"), 220))
            if p and c and c["key"] == "binance:hack":
                rows.append((calendar.timegm(p), src, e.title))
    except Exception as err:
        print(src, err)
rows.sort()
print(f"{len(rows)} filtre gecen binance:hack haberi; ilkleri:")
for t, s, ti in rows[:6]:
    print(" ", time.strftime("%m-%d %H:%M", time.gmtime(t)), s, "|", ti[:90])


def candles(sym, since, n=1000, tf="5m"):
    return ex.fetch_ohlcv(sym, tf, since=int(since * 1000), limit=n)


def px_at(sym, t):
    return candles(sym, t, 1)[0][1]


def chg(sym, t):
    k = candles(sym, t - 3600, 13)
    return k[-1][4] / k[0][1] - 1


# kural: 2 farkli kaynak (3 saat icinde) + 1 saatte <= %-1.5
fire = None
for t, s, ti in rows:
    srcs = {x[1] for x in rows if t - 3 * 3600 <= x[0] <= t}
    d = chg("BTC/USDT", t)
    print(f"  {time.strftime('%m-%d %H:%M', time.gmtime(t))} {s:<14} kaynak {len(srcs)} BTC 1s %{d * 100:+.2f}")
    if len(srcs) >= 2 and d <= HB.DROP_CONFIRM:
        fire = t
        break
    if t - rows[0][0] > 3600 * 6:
        break
print("KURAL TETIKLENDI:", time.strftime("%m-%d %H:%M UTC", time.gmtime(fire)) if fire else "HAYIR")
t0 = rows[0][0]
for s in ("BTC/USDT", "ETH/USDT", "BNB/USDT"):
    a = px_at(s, t0 - 1800)
    b_ = px_at(s, fire) if fire else None
    end = px_at(s, (fire or t0) + HB.BLOCK_MARKET)
    now = ex.fetch_ticker(s)["last"]
    print(f"{s}: ilk haberden 30 dk once {a:g}"
          + (f" | satis ani {b_:g} (%{(b_ / a - 1) * 100:+.2f}) | 48 saat engel sonu {end:g} (satmakla kazanc %{(1 - end / b_) * 100:+.2f})" if fire else "")
          + f" | simdi {now:g}")
