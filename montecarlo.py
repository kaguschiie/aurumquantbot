import json
import threading
import time

import numpy as np

import config as C
import dersler as L
import gecmis as G

N = 10_000
BLOCK = 6
HIST_H = 960
MAX_T = 72
_CACHE, _LOCK = {}, threading.Lock()
CAL = {"n": 0, "pred_tp": None, "real_tp": None, "pred_stop": None, "real_stop": None, "f_tp": 1.0, "f_stop": 1.0}


def bars(ex, sym):
    with _LOCK:
        c = _CACHE.get(sym)
    if c and time.time() - c["t"] < 600:
        return c
    rows = ex.public_get_klines({"symbol": sym.replace("/", ""), "interval": "1h", "limit": 1000})
    a = np.array([[float(r[i]) for i in (2, 3, 4, 5)] for r in rows[:-1]])
    pc = a[:-1, 2]
    rel = np.stack([a[1:, 0] / pc, a[1:, 1] / pc, a[1:, 2] / pc], 1)[-HIST_H:]
    vol = a[1:, 3][-HIST_H:]
    tr = np.maximum(a[1:, 0] - a[1:, 1], np.maximum(abs(a[1:, 0] - pc), abs(a[1:, 1] - pc))) / pc
    out = {"t": time.time(), "rel": rel, "vol": vol, "atr_hist": float(np.mean(tr[-HIST_H:])),
           "atr_now": float(np.mean(tr[-24:])), "price": float(a[-1, 2])}
    with _LOCK:
        _CACHE[sym] = out
    return out


def paths(b, hours, regime=None, vspike=None, n=N, seed=None):
    rng = np.random.default_rng(seed)
    rel = b["rel"].copy()
    lr = np.log(rel[:, 2])
    mu = lr.mean()
    drift = 0.5 * mu + (-0.0005 if regime == "dusus" else 0.0)
    scale = min(max(b["atr_now"] / b["atr_hist"], 0.5), 2.0) if b["atr_hist"] else 1.0
    adj = np.exp((np.log(rel) - mu) * scale + drift)
    m = len(adj) - BLOCK
    w = np.ones(m)
    if vspike and vspike >= 3:
        v = b["vol"]
        med = np.median(v)
        w = np.where(v[:m] >= 2 * med, 3.0, 1.0)
    w /= w.sum()
    nb = -(-hours // BLOCK)
    starts = rng.choice(m, size=(n, nb), p=w)
    idx = (starts[:, :, None] + np.arange(BLOCK)).reshape(n, -1)[:, :hours]
    step = adj[idx]
    cl = np.cumprod(step[:, :, 2], axis=1)
    prev = np.concatenate([np.ones((n, 1)), cl[:, :-1]], 1)
    return np.stack([prev * step[:, :, 0], prev * step[:, :, 1], cl], 2).astype(np.float32)


def run(ex, sym, entry, stop, tp, exit_p=None, hours=24, regime=None, vspike=None, taker=True, n=N, seed=None,
        partial_at=None):
    t0 = time.time()
    b = bars(ex, sym)
    e = entry
    r0 = e - stop
    if r0 <= 0:
        return None
    a = max(b["atr_now"], 0.002)
    hrs = int(min(max(hours or 24, 1), MAX_T))
    ep = exit_p or {}
    p = {"stop_atr": (1 - stop / e) / a, "tp_atr": ((tp / e - 1) / a) if tp and tp < e * 50 else 0,
         "be_r": ep.get("be_r", C.BE_TRIGGER_R), "trail_atr": ep.get("trail_atr", C.TRAIL_ATR),
         "part_frac": ep.get("part_frac", C.PARTIAL_FRAC if partial_at else 0),
         "part_r": ep.get("part_r", ((partial_at - e) / r0) if partial_at else 1.0),
         "ts_h": ep.get("ts_h", 0), "ts_r": ep.get("ts_r", 0.3), "max_h": hrs}
    P = paths(b, hrs, regime, vspike, n, seed)
    R, _, _, why = G.simulate(P, np.full(n, a), p, taker)
    raw = {"tp": float((why == 4).mean()), "stop": float((why == 1).mean())}
    raw["neutral"] = 1 - raw["tp"] - raw["stop"]
    tp_p, st_p = min(raw["tp"] * CAL["f_tp"], 1.0), min(raw["stop"] * CAL["f_stop"], 1.0)
    s = tp_p + st_p
    if s > 1:
        tp_p, st_p = tp_p / s, st_p / s
    return {"tp": round(tp_p, 4), "stop": round(st_p, 4), "neutral": round(1 - tp_p - st_p, 4),
            "raw_tp": round(raw["tp"], 4), "raw_stop": round(raw["stop"], 4), "ev_r": round(float(R.mean()), 3),
            "p_loss": round(float((R < 0).mean()), 4), "q10": round(float(np.quantile(R, 0.1)), 2),
            "q90": round(float(np.quantile(R, 0.9)), 2), "n": n, "hours": hrs, "ms": round((time.time() - t0) * 1000),
            "calibrated": CAL["n"] >= 20}


def best_stop(ex, sym, entry, stop, tp, exit_p, hours, regime, vspike, taker, atr_abs):
    base = run(ex, sym, entry, stop, tp, exit_p, hours, regime, vspike, taker, seed=1)
    if not base or not atr_abs:
        return stop, tp, base, ""
    rr = (tp - entry) / (entry - stop) if tp and tp < entry * 50 else None
    best = (base["ev_r"], stop, tp, base)
    for k in (1.5, 2.0, 2.5, 3.0):
        s2 = entry - k * atr_abs
        if s2 <= 0 or abs(s2 - stop) / entry < 0.002 or (entry - s2) / entry > 0.15 \
                or (2 * C.FEE + 2 * C.SLIPPAGE) / ((entry - s2) / entry) > C.MAX_COST_R:
            continue
        t2 = entry + rr * (entry - s2) if rr else tp
        m = run(ex, sym, entry, s2, t2, exit_p, hours, regime, vspike, taker, n=4000, seed=1)
        if m and m["ev_r"] > best[0] + 0.05:
            best = (m["ev_r"], s2, t2, None)
    if best[3] is base:
        return stop, tp, base, ""
    mc = run(ex, sym, entry, best[1], best[2], exit_p, hours, regime, vspike, taker, seed=2)
    return best[1], best[2], mc, (f"Monte Carlo stop optimizasyonu: stop {stop:.6g} → {best[1]:.6g} (beklenen değer "
                                   f"{base['ev_r']:+.2f}R → {mc['ev_r']:+.2f}R, ödül/risk korundu)")


def line(mc):
    if not mc:
        return ""
    return (f"Monte Carlo ({mc['n']:,}".replace(",", ".") + f" yol / {mc['hours']} sa): Hedef önce %{mc['tp'] * 100:.1f} | "
            f"Stop önce %{mc['stop'] * 100:.1f} | Nötr %{mc['neutral'] * 100:.1f} | beklenen {mc['ev_r']:+.2f}R")


def _init():
    with L._LOCK, L._db() as con:
        con.execute("CREATE TABLE IF NOT EXISTS mc (id INTEGER PRIMARY KEY, t REAL, sym TEXT, acct TEXT, p_tp REAL, "
                    "p_stop REAL, ev_r REAL, outcome TEXT, r REAL)")


def record(acct, sym, mc, reason, r):
    if not mc:
        return ""
    _init()
    outcome = "hedef" if reason == "HEDEF" else "stop" if reason == "STOP" else "notr"
    with L._LOCK, L._db() as con:
        con.execute("INSERT INTO mc (t,sym,acct,p_tp,p_stop,ev_r,outcome,r) VALUES (?,?,?,?,?,?,?,?)",
                    (time.time(), sym, acct, mc.get("raw_tp", mc["tp"]), mc.get("raw_stop", mc["stop"]), mc["ev_r"],
                     outcome, r))
    calibrate()
    surprise = (outcome == "stop" and mc["stop"] < 0.3) or (outcome == "hedef" and mc["tp"] < 0.2)
    txt = (f"Monte Carlo karnesi: {sym} için tahmin hedef %{mc['tp'] * 100:.0f} / stop %{mc['stop'] * 100:.0f} / beklenen "
           f"{mc['ev_r']:+.2f}R idi; gerçekleşen {outcome} ({r:+.2f}R).")
    if CAL["n"] >= 20:
        txt += (f" Son {CAL['n']} işlemde ortalama tahmin hedef %{CAL['pred_tp'] * 100:.0f}, gerçekleşen "
                f"%{CAL['real_tp'] * 100:.0f}; stop tahmini %{CAL['pred_stop'] * 100:.0f}, gerçekleşen "
                f"%{CAL['real_stop'] * 100:.0f} → sonraki simülasyonlar ×{CAL['f_tp']:.2f} / ×{CAL['f_stop']:.2f} ile "
                f"düzeltiliyor.")
    if surprise:
        L.add_lesson("mc", sym, acct, f"Monte Carlo yanıldı: {sym} {outcome}", txt, (), r)
    return txt


def calibrate():
    _init()
    with L._LOCK, L._db() as con:
        rows = con.execute("SELECT p_tp, p_stop, outcome FROM mc ORDER BY t DESC LIMIT 200").fetchall()
    n = len(rows)
    if n < 20:
        CAL.update(n=n, f_tp=1.0, f_stop=1.0)
        return CAL
    pt, ps = sum(r["p_tp"] for r in rows) / n, sum(r["p_stop"] for r in rows) / n
    rt, rs = sum(r["outcome"] == "hedef" for r in rows) / n, sum(r["outcome"] == "stop" for r in rows) / n
    f = lambda real, pred: min(max((real + 0.02) / (pred + 0.02), 0.5), 1.5)
    CAL.update(n=n, pred_tp=pt, real_tp=rt, pred_stop=ps, real_stop=rs, f_tp=f(rt, pt), f_stop=f(rs, ps))
    return CAL


def summary():
    try:
        calibrate()
    except Exception:
        pass
    return json.loads(json.dumps(CAL))
