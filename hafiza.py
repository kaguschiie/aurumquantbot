"""Kalici islem hafizasi (trading_memory.md): her karar ve kapanan islem okunabilir Markdown olarak islenir; bir sonraki
kararda adaylarin BENZER gecmis durumlari (ayni sinyallerin cogu + ayni rejim) Ders Defteri veritabanindan bulunur ve
komiteye/Claude'a 'gecen sefer ne oldu' notu olarak verilir. Benzer gercek islemler tutarli zarar ettirmisse alim
risk yoneticisinde reddedilir (metin olarak kalmaz, davranisi degistirir)."""
import json
import os
import threading
import time

import config as C
import dersler as L

FILE = os.path.join(C.APP_DIR, "trading_memory.md")
MAX_BYTES = 1_500_000      # dosya bunu gecince en eski kayitlar arsive tasinir
SIM = 0.6                  # benzerlik: ortak sinyal / tum sinyaller (Jaccard)
VETO_N, VETO_AVG = 5, -0.3  # en az 5 benzer gercek islem ve ort <= -0.3R ise alim engellenir
_lock = threading.Lock()


def _write(text):
    with _lock:
        new = not os.path.exists(FILE)
        with open(FILE, "a", encoding="utf-8") as fh:
            if new:
                fh.write("# KriptoQuantBot · İşlem Hafızası (trading_memory.md)\n\nHer karar ve kapanan işlem buraya "
                         "işlenir. Bot bir sonraki kararda benzer durumları buradan ve Ders Defteri'nden okur.\n\n")
            fh.write(text)
        if os.path.getsize(FILE) > MAX_BYTES:  # en eski yarisi arsive
            with open(FILE, encoding="utf-8") as fh:
                data = fh.read()
            cut = data.find("\n## ", len(data) // 2)
            if cut > 0:
                with open(FILE.replace(".md", f"_arsiv_{time.strftime('%Y%m')}.md"), "a", encoding="utf-8") as fh:
                    fh.write(data[:cut])
                with open(FILE, "w", encoding="utf-8") as fh:
                    fh.write("# KriptoQuantBot · İşlem Hafızası (trading_memory.md)\n" + data[cut:])


def _ts(t=None):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t or time.time()))


def record_decision(rec):
    """Karar (komite kartlari dahil) hafizaya."""
    acts = [a for a in rec.get("actions") or [] if a.get("type") in ("buy", "close", "adjust")]
    if not acts:
        return
    out = [f"\n## {_ts(rec.get('t'))} · KARAR · {rec.get('model', '')}\n", f"- Piyasa: {rec.get('view', '')[:400]}\n"]
    for a in acts:
        c = a.get("card") or {}
        verdict = c.get("verdict") or ("ONAY" if "emir verildi" in (a.get("result") or "") else "RED")
        out.append(f"- **{a.get('label', '')}** ({c.get('kind_tr', '-')}) → {verdict}: {a.get('result', '')[:200]}\n")
        if c.get("bull"):
            out.append(f"  - Boğa: {'; '.join(c['bull'][:3])}\n")
        if c.get("bear"):
            out.append(f"  - Ayı: {'; '.join(c['bear'][:3])}\n")
        if c.get("memory"):
            out.append(f"  - Hafıza: {c['memory']}\n")
    _write("".join(out))


def record_trade(acct, sym, pos, price, reason, pnl, r, lesson):
    e = pos["entry"]
    hours = (time.time() - pos["t"]) / 3600
    _write(f"\n## {_ts()} · İŞLEM KAPANDI · {sym} · {acct}\n"
           f"- Sonuç: **{r:+.2f}R** ({pnl:+.2f} $, %{(price / e - 1) * 100:+.2f}), çıkış {reason}, {hours:.1f} sa, "
           f"en yüksek %{(pos.get('hi', e) / e - 1) * 100:+.1f}\n"
           f"- Kurulum: {pos.get('kind') or '-'}; sinyaller: {L.labels(pos.get('sigs') or []) or '-'}; rejim "
           f"{(pos.get('feats') or {}).get('regime') or '-'}\n- Ders: {lesson[:600]}\n")


def recall(sym, sigs, regime=None, kind=None):
    """Benzer gecmis durumlar: gercek islemler (Ders Defteri otopsileri) + ayni coinin son islemleri.
    Donus: {n, wins, avg, note, veto}"""
    sigs = set(sigs or [])
    with L._LOCK, L._db() as con:
        rows = con.execute("SELECT t, sym, sigs, r, feats, why FROM ornek WHERE src='islem' AND r IS NOT NULL "
                           "ORDER BY t DESC LIMIT 600").fetchall()
    sim, same = [], []
    for x in rows:
        sg = {s for s in (x["sigs"] or "").split(",") if s}
        try:
            reg = json.loads(x["feats"] or "{}").get("regime")
        except ValueError:
            reg = None
        if x["sym"] == sym:
            same.append(x)
        if sg and sigs and len(sg & sigs) / len(sg | sigs) >= SIM and (regime is None or reg in (None, regime)):
            sim.append(x)
    out = {"n": len(sim), "wins": sum(1 for x in sim if x["r"] > 0.25), "avg": None, "note": "", "veto": None}
    parts = []
    if sim:
        avg = sum(max(min(x["r"], 3), -1.5) for x in sim) / len(sim)
        out["avg"] = round(avg, 3)
        worst = min(sim, key=lambda x: x["r"])
        parts.append(f"benzer {len(sim)} gerçek işlem: {out['wins']} kazanç, ort {avg:+.2f}R "
                     f"(en kötü {worst['sym'].split('/')[0]} {worst['r']:+.2f}R, {_ts(worst['t'])[:10]})")
        if len(sim) >= VETO_N and avg <= VETO_AVG:
            out["veto"] = (f"Hafıza: benzer şartlardaki {len(sim)} gerçek işlemin ortalaması {avg:+.2f}R "
                           f"({out['wins']} kazanç) — aynı hata tekrarlanmaz")
    if same:
        last = same[0]
        parts.append(f"{sym.split('/')[0]} son işlem {last['r']:+.2f}R ({_ts(last['t'])[:10]})")
    out["note"] = "; ".join(parts) or "benzer geçmiş işlem yok (ilk kez)"
    return out


def tail(n_bytes=20000):
    try:
        with open(FILE, "rb") as fh:  # bayt ofseti cok baytli Turkce karakterin ortasina dusebilir
            fh.seek(max(0, os.path.getsize(FILE) - n_bytes))
            return fh.read().decode("utf-8", errors="ignore")
    except OSError:
        return ""


def brief_lines(cands):
    """Claude'a: adaylarin hafiza notlari + son kapanan islemlerin dersleri (trading_memory.md)."""
    out = []
    for s, sigs, regime in cands[:12]:
        m = recall(s, sigs, regime)
        out.append(f"- {s.split('/')[0]}: {m['note']}" + (" -> ENGELLI" if m["veto"] else ""))
    t = tail(4000)
    last = [b.strip() for b in t.split("\n## ") if "İŞLEM KAPANDI" in b][-5:]
    if last:
        out.append("SON KAPANAN ISLEMLER (hafizadan):\n" + "\n".join("## " + b[:420] for b in last))
    return (["HAFIZA (trading_memory.md: benzer gecmis durumlar; ayni hatayi tekrarlama):"] + out) if out else []
