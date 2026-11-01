"""Canli piyasa akisi (WebSocket): Binance fiyat/24s/emir defteri/mumlar + Bybit fonlama/acik pozisyon.
Saniyede bir guncellenir; baglanti koparsa kendiligindan yeniden baglanir."""
import json
import logging
import threading
import time

import websocket

log = logging.getLogger("bot")
BINANCE_WS = "wss://stream.binance.com:9443/stream?streams="
BYBIT_WS = "wss://stream.bybit.com/v5/public/linear"


def sid(sym):
    return sym.replace("/", "").lower()


def usym(stream_id):
    s = stream_id.upper()
    return s[:-4] + "/USDT" if s.endswith("USDT") else s


class Stream:
    def __init__(self):
        self.ticker, self.book, self.deriv, self.kline = {}, {}, {}, {}
        self.symbols, self.klines = set(), {}  # klines: (sym, tf) -> son abone olma zamani
        self.lock = threading.Lock()
        self.last = {"binance": 0.0, "bybit": 0.0}
        self._ws = {"binance": None, "bybit": None}
        self._req = 1

    # ---- disari acik ----
    def start(self, symbols):
        if getattr(self, "_started", False):  # motor yeniden baslarsa ayni akis kullanilir
            return self.set_symbols(symbols)
        self._started = True
        self.symbols = set(symbols)
        for name, fn in (("binance", self._binance), ("bybit", self._bybit), ("watchdog", self._watchdog)):
            threading.Thread(target=fn, name=f"ws-{name}", daemon=True).start()

    def set_symbols(self, symbols):
        symbols = set(symbols)
        if symbols != self.symbols:
            self.symbols = symbols
            for ws in self._ws.values():  # yeni listeyle yeniden baglan
                if ws:
                    ws.close()

    def live(self, name="binance", max_age=10):
        return time.time() - self.last[name] < max_age

    def watch_kline(self, sym, tf):
        """Grafikte acik coin icin canli mum aboneligi (son 3 grafik tutulur)."""
        key = (sym, tf)
        new = key not in self.klines
        self.klines[key] = time.time()
        if new:
            self._send("binance", {"method": "SUBSCRIBE", "params": [f"{sid(sym)}@kline_{tf}"]})
            for old in sorted(self.klines, key=self.klines.get)[:-3]:
                self.klines.pop(old)
                self.kline.pop(old, None)  # geri donulunce eski (donmus) mum kullanilmasin
                self._send("binance", {"method": "UNSUBSCRIBE", "params": [f"{sid(old[0])}@kline_{old[1]}"]})

    def snapshot(self, sym):
        with self.lock:
            return dict(self.ticker.get(sym, {})), dict(self.book.get(sym, {})), dict(self.deriv.get(sym, {}))

    # ---- ic ----
    def _send(self, name, msg):
        ws = self._ws.get(name)
        if ws and ws.sock and ws.sock.connected:
            msg["id"] = self._req
            self._req += 1
            try:
                ws.send(json.dumps(msg))
            except Exception:
                pass

    def _binance(self):
        def url():
            streams = ["!miniTicker@arr"] + [f"{sid(s)}@depth20@1000ms" for s in sorted(self.symbols)]
            return BINANCE_WS + "/".join(streams)

        def on_open(ws):
            for sym, tf in list(self.klines):
                self._send("binance", {"method": "SUBSCRIBE", "params": [f"{sid(sym)}@kline_{tf}"]})

        while True:  # url sembol listesine bagli: her baglantida yeniden kur
            self._run_once("binance", url(), on_open, self._on_binance)
            time.sleep(2)

    def _run_once(self, name, url, on_open, on_message):
        try:
            ws = websocket.WebSocketApp(url, on_open=on_open, on_message=on_message)
            self._ws[name] = ws
            ws.run_forever(ping_interval=20, ping_timeout=10)
        except Exception as e:
            log.warning("%s akis hatasi: %s", name, e)

    def _on_binance(self, _ws, msg):
        self.last["binance"] = time.time()
        m = json.loads(msg)
        stream, d = m.get("stream", ""), m.get("data")
        if d is None:
            return
        now = time.time()
        with self.lock:
            if stream == "!miniTicker@arr":
                for t in d:
                    s = usym(t["s"])
                    if s in self.symbols:
                        o, c = float(t["o"]), float(t["c"])
                        self.ticker[s] = {"last": c, "open": o, "high": float(t["h"]), "low": float(t["l"]),
                                          "qvol": float(t["q"]), "chg24": (c / o - 1) * 100 if o else 0.0, "t": now}
            elif "@depth20" in stream:
                s = usym(stream.split("@")[0])
                bids = [(float(p), float(q)) for p, q in d["bids"]]
                asks = [(float(p), float(q)) for p, q in d["asks"]]
                if bids and asks:
                    b, a = sum(p * q for p, q in bids), sum(p * q for p, q in asks)
                    mid = (bids[0][0] + asks[0][0]) / 2
                    self.book[s] = {"bid": bids[0][0], "ask": asks[0][0], "imb": b / (a + b) if a + b else 0.5,
                                    "spread": (asks[0][0] - bids[0][0]) / mid * 100, "t": now}
            elif "@kline_" in stream:
                k = d["k"]
                self.kline[(usym(k["s"]), k["i"])] = {"time": k["t"] // 1000, "open": float(k["o"]), "high": float(k["h"]),
                                                      "low": float(k["l"]), "close": float(k["c"]), "volume": float(k["v"]),
                                                      "rt": now}

    def _bybit(self):
        def on_open(ws):
            args = [f"tickers.{sid(s).upper()}" for s in sorted(self.symbols)]
            for i in range(0, len(args), 10):
                ws.send(json.dumps({"op": "subscribe", "args": args[i:i + 10]}))

        def pinger():
            while True:
                time.sleep(20)
                ws = self._ws.get("bybit")
                if ws and ws.sock and ws.sock.connected:
                    try:
                        ws.send('{"op":"ping"}')
                    except Exception:
                        pass
        threading.Thread(target=pinger, daemon=True).start()
        while True:
            self._run_once("bybit", BYBIT_WS, on_open, self._on_bybit)
            time.sleep(2)

    def _on_bybit(self, _ws, msg):
        m = json.loads(msg)
        topic = m.get("topic", "")
        if not topic.startswith("tickers."):
            return
        self.last["bybit"] = time.time()
        d, s = m["data"], usym(m["data"].get("symbol", topic.split(".")[1]))
        with self.lock:
            cur = self.deriv.setdefault(s, {})
            for src, dst in (("fundingRate", "funding"), ("openInterest", "oi"), ("markPrice", "mark"),
                             ("nextFundingTime", "next_funding")):
                if d.get(src) not in (None, ""):
                    cur[dst] = float(d[src])
            cur["t"] = time.time()

    _shared = None

    @classmethod
    def shared(cls):
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def _watchdog(self):
        while True:
            time.sleep(15)
            for name in ("binance", "bybit"):
                ws = self._ws.get(name)
                if ws and self.last[name] and time.time() - self.last[name] > 45:
                    log.warning("%s akisi sessiz, yeniden baglaniliyor", name)
                    ws.close()
