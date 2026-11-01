"""Kurumsal Ajan Komitesi (TradingAgents mimarisi; yerel, ucretsiz). Her alis onerisi dort asamadan gecer ve karar
karti olarak kaydedilir (arayuz Kararlar sayfasi + Telegram):
1) ANALISTLER: teknik (4 zaman dilimi, RSI, ATR, hacim, BTC'den ayrisma), zincir (balina akisi, likidasyon
   kumeleri), makro/haber (risk istahi, VIX/DXY, altin).
2) BOGA / AYI TARTISMASI: her iki tarafin tezleri sinyallerden uretilir ve kanitla TARTILIR: Gecmis Okulu (2 yil,
   binlerce ornek) ve Ders Defteri (canli sonuc) o sinyalin gercek ortalama R'sini yazar; kanitsiz tez agirlik almaz.
3) TRADER: oneri (yerel motor veya Claude): giris, stop, hedef, risk.
4) RISK YONETIMI: bot.vet_buy kontrolleri -> ONAY ya da VETO (sebebiyle). Veto edilen oneri borsaya gitmez."""
import dersler as L
import gecmis as G

KIND_TR = {**G.KIND_TR, "balina": "Balina refleksi", "elle": "Elle", "gunluk_trend": "Günlük trend kolu (Donchian-20)"}


def _f(v, fmt="+.2f", d="-"):
    return d if v is None else format(v, fmt)


def analysts(row, ctx, whale=None, liq=None, macro=None):
    """Uc analistin notu (sadece eldeki veriden; ag cagrisi yok)."""
    te = row.get("tech") or {}
    tek = (f"Teknik 15dk/1s/4s/1g {_f(te.get('15m'))}/{_f(te.get('1h'))}/{_f(te.get('4h'))}/{_f(te.get('1d'))}"
           f" · RSI {_f(row.get('rsi'), '.0f')} · ATR %{_f((row.get('atr_pct') or 0) * 100, '.2f')}"
           f" · hacim {_f(row.get('vspike'), '.1f')}x · 24s %{_f(row.get('chg24'), '+.1f')}"
           f" (BTC'ye göre {_f(row.get('rs24'), '+.1f')}) · alıcı akışı {_f((row.get('flow3') or 0) * 100, '+.1f')}%")
    if whale:
        zin = (f"Balina akışı {whale.get('ratio', 0):+.2f} (al {whale.get('buy', 0) / 1e6:.2f}M$ / sat "
               f"{whale.get('sell', 0) / 1e6:.2f}M$, {whale.get('n', 0)} büyük işlem)")
    else:
        zin = "Balina akışı: anlamlı büyük işlem yok"
    if liq and not liq.get("none"):
        zin += (f" · likidasyon long {'/'.join(f'{p:.6g}' for p, _, _ in liq.get('long', [])[:2]) or '-'}, short "
                f"{'/'.join(f'{p:.6g}' for p, _, _ in liq.get('short', [])[:2]) or '-'}")
    mak = f"Rejim {ctx.get('regime') or '-'} · volatilite {ctx.get('vol') or '-'}"
    if macro and macro[0]:
        mak += f" · küresel risk {macro[0]} ({str(macro[1])[:120]})"
    return {"teknik": tek, "zincir": zin, "makro": mak}


def _evidence(sig, regime):
    """Sinyalin kaniti: Gecmis Okulu (oncelik, binlerce ornek) yoksa Ders Defteri. Donus: (ort R, n, kaynak) | None."""
    st = G.sig_stat((sig,))
    if st:
        return st["avg"], st["n"], "2 yıl"
    w = L.STATE["weights"].get(sig)
    if w and w.get("n", 0) >= 20:
        return w["avg"], w["n"], "canlı"
    return None


def debate(sigs, regime, row, kind=None):
    """Boga/ayi tezleri, kanit agirlikli. Donus: {bull, bear, edge}"""
    bull, bear, edge = [], [], 0.0
    for s in sorted(sigs):
        ev = _evidence(s, regime)
        txt = L.label(s)
        if ev:
            avg, n, src = ev
            txt += f" ({src}: {avg:+.2f}R, {n} örnek)"
            edge += avg
            (bull if avg > 0.02 else bear if avg < -0.02 else bear if s in BEARISH else bull).append(txt)
        else:
            (bear if s in BEARISH else bull).append(txt)
    if kind:
        ks = G.kind_stat(kind, regime)
        if ks:
            (bull if ks["avg"] > 0 else bear).append(
                f"{KIND_TR.get(kind, kind)} kurulumu bu rejimde (2 yıl, test dönemi) {ks['avg']:+.2f}R, {ks['n']} örnek")
    if (row.get("rsi") or 0) >= 70:
        bear.append(f"RSI {row['rsi']:.0f}: aşırı alım, geri çekilme riski")
    if (row.get("whale") or 0) <= -0.3:
        bear.append(f"balinalar satıyor ({row['whale']:+.2f})")
    elif (row.get("whale") or 0) >= 0.3:
        bull.append(f"balinalar alıyor ({row['whale']:+.2f})")
    return {"bull": bull[:6], "bear": bear[:6], "edge": round(edge, 3)}


BEARISH = {"zayif_4s", "gunluk_asagi", "rsi_asiri", "pompa", "defter_satici", "fonlama_yuksek", "balina_satis",
           "rejim_dusus", "makro_riskoff"}


def card(a, out, row, ctx, sigs, checks, memory, whale=None, liq=None, macro=None):
    """Tek oneri icin komite karti (arayuz + Telegram + hafiza)."""
    kind = a.get("kind") or ("elle" if a.get("manual") else None)
    d = debate(sigs, ctx.get("regime"), row, kind) if a.get("type") == "buy" else {"bull": [], "bear": [], "edge": 0}
    if a.get("bull_case"):
        d["bull"].insert(0, "Trader: " + str(a["bull_case"])[:200])
    if a.get("bear_case"):
        d["bear"].insert(0, "Trader: " + str(a["bear_case"])[:200])
    ok = "emir verildi" in (out.get("result") or "") or out.get("result") in ("kapatıldı", "güncellendi")
    veto = next((c for c in checks if not c[1]), None)
    return {"kind": kind, "kind_tr": KIND_TR.get(kind, "-"), "analyst": analysts(row, ctx, whale, liq, macro),
            "bull": d["bull"], "bear": d["bear"], "edge": a.get("_edge"),  # risk yoneticisinin kullandigi tek avantaj
            "checks": [{"name": n, "ok": bool(o), "detail": t} for n, o, t in checks],
            "verdict": "ONAY" if ok else "VETO" if veto else "RED",
            "veto": veto[2] if veto else (None if ok else out.get("result")),
            "memory": memory or "", "mc": a.get("_mc")}
