"""Uygulama ici sohbet: Claude, botun canli verisini okuyup kendi kararlarini aciklar.
Rol bazli: normal kullanici sadece gercek (LIVE) hesabi gorur. Sohbetten emir verilmez."""
import json
import re
import time

import agent as A
import config as C
import dersler
import model as M
import quant as Q

CHAT_SHARE = 0.3  # gunluk Claude butcesinin sohbete ayrilabilecek en fazla payi


def context(b, role):
    """Claude'a giden kompakt ozet: hesap(lar), pozisyonlar, son kararlar, piyasa tablosu, gunluk."""
    admin = role == "admin"
    with b.lock:
        accts = [a for m, a in b.accounts.items() if admin or m == "LIVE"]
        L = [f"Zaman: {time.strftime('%Y-%m-%d %H:%M')} | Durum: {b.status} | Gercek islem izni: "
             f"{'acik' if C.LIVE_ALLOWED else 'kapali (admin onayi bekliyor)'}"]
        if not accts:
            L.append("Gercek Binance hesabi henuz bagli degil (Ayarlar'dan API anahtari girilmeli).")
        for a in accts:
            st, eq = a.st, a.equity()
            n = st["wins"] + st["losses"]
            op = open_pnl(a, b)
            L.append(f"\nHESAP {'SANAL (PAPER)' if a.mode == 'PAPER' else 'GERCEK (LIVE)'}: kasa {eq:.2f} USDT"
                     + (f" (baslangic {st['start_equity']:.2f}, fark {eq - st['start_equity']:+.2f})" if st["start_equity"] else "")
                     + f", bugun %{a.day_pct():+.2f}. KAPANAN: toplam {n} islem ({st['wins']} kazanc / {st['losses']} kayip), "
                     f"net {st['pnl']:+.2f} USDT (tum islemlerin toplami; asagida sadece son 6'si listelenir). ACIK "
                     f"pozisyonlarin anlik K/Z'si {op:+.2f} USDT (giris komisyonu dahil). Denklem: kasa farki = kapanan net + "
                     f"acik K/Z (cikis komisyonu henuz dusulmez). Yeni islem: {a.blocked() or 'acik'}.")
            for s, p in st["positions"].items():
                px = b.bid.get(s, p["entry"])
                L.append(f"- {s}: giris {p['entry']:g}, simdi {px:g} (%{(px / p['entry'] - 1) * 100:+.2f}), stop "
                         f"{p['sl']:.6g}, hedef {p['tp']:.6g}, kismi kar: {'alindi' if p.get('part') else 'yok'}, acilis "
                         f"{time.strftime('%d.%m %H:%M', time.localtime(p['t']))}. Gerekce: {p.get('why', '')[:200]}")
            for t in st["trades"][-6:]:
                L.append(f"  kapanan: {time.strftime('%d.%m %H:%M', time.localtime(t['t']))} {t['sym']} {t['reason']} "
                         f"{t['pnl']:+.2f} USDT (%{t['pct']:+.2f})")
        sc = b.last_scan or {}
        L.append(f"\nPiyasa rejimi: {M.REGIME_TR.get(sc.get('regime'), '-')} | genislik %{(sc.get('breadth') or 0) * 100:.0f}"
                 f" | {brain_counts(b)}"
                 + (f", Claude API harcamasi {b.ag['spent']:.2f}$ / {C.CLAUDE_DAILY_BUDGET:g}$ (CLI aboneligi ve yerel motor "
                    f"ucretsizdir)" if admin else ""))
        L.append("\nSON KARARLARIN:")
        for d in b.ag["decisions"][-4:]:
            L.append(f"- {time.strftime('%d.%m %H:%M', time.localtime(d['t']))} ({d['kind']}): {d['view']}")
            for r in d["actions"]:
                res = r.get("result", "") if admin else re.sub(r"sanal: [^;\n]*;? ?", "", r.get("result", ""))
                L.append(f"  • {r['label']}: {r['reason'][:200]} -> {res}")
        L.append("\nPIYASA (hacme gore ilk 20: fiyat, 24s%, teknik ozet 1s/4s, RSI, fonlama%):")
        rows = sorted(b.market.values(), key=lambda r: -(r.get("qvol") or 0))[:20]
        for r in rows:
            te = r.get("tech") or {}
            L.append(f"- {r['sym']}: {r.get('price')}, %{r.get('chg24') or 0:+.1f}, teknik {te.get('1h', '-')}/"
                     f"{te.get('4h', '-')}, RSI {r.get('rsi') if r.get('rsi') is None else round(r['rsi'])}, "
                     f"fonlama %{(r.get('funding') or 0) * 100:.3f}")
        radar = [o for o in b.tech.opps if o.get("full")][:8]
        if radar:
            L.append("\nRADAR (tum Binance USDT, en guclu 8: 24s%, BTC'ye gore, hacim patlamasi x, teknik 1s/4s):")
            for o in radar:
                te = o.get("tech") or {}
                L.append(f"- {o['sym']}: %{o.get('chg24') or 0:+.1f}, BTC'ye gore {o.get('rs24') or 0:+.1f}, hacim "
                         f"{o.get('vspike') or 0:.1f}x, teknik {te.get('1h', '-')}/{te.get('4h', '-')}")
        mk = b.macro.brief_line()
        if mk:
            L.append("\n" + mk)
        notes = [n for n in b.notes if admin or n.get("acct") in (None, "LIVE")][-10:]
    L.append("\nDERS DEFTERI:\n" + ("\n".join(dersler.brief_lines(admin)) or "henuz ders yok"))
    L.append("\nKARAR GUNLUGU (son 10):")
    for nt in notes:
        text = nt["text"] if admin else re.sub(r"sanal: [^;\n]*;? ?", "", nt["text"])
        L.append(f"- {time.strftime('%d.%m %H:%M', time.localtime(nt['t']))} [{nt['kind']}] {text[:300]}")
    return "\n".join(L)


def open_pnl(a, b):
    """Acik pozisyonlarin anlik net K/Z'si (kismi kar dahil, alis + satis komisyonu dusulmus) - arayuzle ayni formul."""
    return sum(p.get("realized", 0) + p["qty"] * (b.bid.get(s, p["entry"]) - p["entry"]) - p["fee"]
               - p["qty"] * b.bid.get(s, p["entry"]) * C.FEE for s, p in a.st["positions"].items())


def brain_counts(b):
    """Bugunku kararlarin hangi beyinden geldigi (Claude mu, yerel motor mu)."""
    day = time.strftime("%Y-%m-%d")
    ds = [d for d in b.ag["decisions"] if time.strftime("%Y-%m-%d", time.localtime(d["t"])) == day]
    cl = sum(1 for d in ds if "Claude" in (d.get("model") or ""))
    mode = "Kota koruma ACIK: tarama/olay kararlarini ucretsiz yerel motor verir, Claude elle ve seyrek strateji " \
           "incelemesinde" if C.QUOTA_SAVER else "Kota koruma kapali"
    return f"bugun {len(ds)} karar kaydi: Claude {cl}, Yerel Quant Motoru {len(ds) - cl} ({mode})"


def history_load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def history_save(path, h):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(h[-60:], fh, ensure_ascii=False)


def ask_stream(question, b, role, path):
    """Cevabi parca parca uretir. Beyin sirasi: Claude API > Claude CLI (abonelik) > yerel analist (ucretsiz).
    Gecmis diske, API maliyeti gunluk butceye yazilir."""
    history, answer, brain = history_load(path), "", A.brain()
    ag = b.ag
    if brain == "api" and ag["day"] == time.strftime("%Y-%m-%d", time.gmtime()) and (
            ag["spent"] >= C.CLAUDE_DAILY_BUDGET or ag.get("chat_spent", 0) >= C.CLAUDE_DAILY_BUDGET * CHAT_SHARE):
        brain = "cli" if A.cli_ready() else "local"  # API sohbet butcesi doldu: ucretsiz beyne gec
    gen = None
    if brain == "api":
        gen = A.chat_stream(question, context(b, role), history, on_cost=lambda usd: b.add_cost(usd, chat=True))
    elif brain == "cli":
        gen = A.cli_chat_stream(question, context(b, role), history)
    if gen is not None:
        try:
            for part in gen:
                answer += part
                yield part
        except A.AgentError as e:
            if answer:
                yield f"\n[{e}]"
            else:
                brain = "local"
                yield f"_({e}; yerel analist cevaplıyor)_\n\n"
    if brain == "local":
        answer = local_answer(question, b, role)
        yield answer
    if answer.strip() and not answer.lstrip().startswith("["):
        history_save(path, history + [{"role": "user", "content": question, "t": time.time()},
                                      {"role": "assistant", "content": answer.strip(), "t": time.time()}])


# ---------------- yerel analist (ucretsiz, Claude yokken) ----------------
def local_answer(question, b, role):
    """Botun canli verisinden kurala dayali Turkce cevap. Soru anahtar kelimelerine gore bolum secer."""
    q = question.lower()
    admin = role == "admin"
    with b.lock:
        accts = [a for m, a in b.accounts.items() if admin or m == "LIVE"]
        rows = {s: dict(r) for s, r in b.market.items()}
        dec = b.ag["decisions"][-1] if b.ag["decisions"] else None
        sc = dict(b.last_scan or {})
        parts = ["**Yerel analist** (ücretsiz mod; Claude bağlı değil, bilgiler botun canlı verisinden):"]
        sym = next((s for s in rows if s.split("/")[0].lower() in q.replace("?", " ").split()), None)
        want = lambda *ks: any(k in q for k in ks)
        if sym:
            parts.append(_coin(sym, rows[sym]))
        if want("pozisyon", "açık", "elimde", "portföy", "kasa", "bakiye") or not (sym or want("piyasa", "neden", "niye", "bugün", "karar")):
            for a in accts:
                parts.append(_account(a, b))
        if want("neden", "niye", "karar", "bekli", "açmıyor", "almıyor", "işlem aç"):
            parts.append(_why(b, accts, rows, dec))
        if want("piyasa", "durum", "nasıl", "rejim", "fırsat", "hangi", "öneri") or not sym:
            parts.append(_market(sc, rows))
        if want("bugün", "ne yaptın", "özet", "sonuç"):
            for a in accts:
                parts.append(_today(a))
        if want("radar", "fırsat", "ayrış", "hacim"):
            parts.append(_radar(b))
        if want("altın", "borsa", "makro", "nasdaq", "dolar", "hisse", "bist"):
            parts.append(_macro(b))
    if want("ders", "öğren", "otopsi", "hata"):
        parts.append(_lessons(admin))
    parts.append("_Bu bir yazılımdır, yatırım tavsiyesi değildir._")
    return "\n\n".join(p for p in parts if p)


def _coin(s, r):
    te = r.get("tech") or {}
    f = lambda v: "-" if v is None else f"{v:+.2f}"
    return (f"**{s}**: fiyat {r.get('price')}, 24s %{r.get('chg24') or 0:+.1f}, RSI {r.get('rsi') or 0:.0f}. "
            f"Teknik özet 15dk/1s/4s/1g: {f(te.get('15m'))} / {f(te.get('1h'))} / {f(te.get('4h'))} / {f(te.get('1d'))}. "
            f"Emir defteri alıcı payı %{(r.get('ob') or 0) * 100:.0f}. Değerlendirme: {Q.explain(te, r)}.")


def _account(a, b):
    st, eq = a.st, a.equity()
    n = st["wins"] + st["losses"]
    name = "Sanal kasa" if a.mode == "PAPER" else "Gerçek hesap"
    L = [f"**{name}**: {eq:.2f} USDT, bugün %{a.day_pct():+.2f}, kapanan {n} işlem "
         f"(kazanma %{st['wins'] / n * 100 if n else 0:.0f}) net {st['pnl']:+.2f} USDT + açık pozisyonlar "
         f"{open_pnl(a, b):+.2f} USDT = kasa farkı {eq - (st['start_equity'] or eq):+.2f} USDT. "
         f"Yeni işlem: {a.blocked() or 'açık'}."]
    for s, p in st["positions"].items():
        px = b.bid.get(s, p["entry"])
        L.append(f"- **{s}**: giriş {p['entry']:g}, şimdi {px:g} (%{(px / p['entry'] - 1) * 100:+.2f}), stop {p['sl']:.6g}, "
                 f"hedef {p['tp']:.6g}{', kısmi kâr alındı' if p.get('part') else ''}. Gerekçe: {p.get('why', '')[:160]}")
    if not st["positions"]:
        L.append("- Açık pozisyon yok.")
    return "\n".join(L)


def _why(b, accts, rows, dec):
    L = ["**Neden işlem açtım / bekliyorum:**"]
    if dec:
        L.append(f"Son karar ({time.strftime('%H:%M', time.localtime(dec['t']))}, {dec.get('model', '')}): {dec['view']}")
        for r in dec["actions"]:
            L.append(f"- {r['label']}: {r['reason'][:200]} → {r.get('result', '')}")
    for a in accts:
        if a.blocked():
            L.append(f"- {'Sanal' if a.mode == 'PAPER' else 'Gerçek'} hesapta yeni işlem kapalı: {a.blocked()}.")
    if b.regime == "dusus":
        L.append("- Piyasa sert düşüş rejiminde; yerel motor bu rejimde yeni alım yapmaz.")
    near = sorted((r["tech"]["1h"], s) for s, r in rows.items() if (r.get("tech") or {}).get("1h") is not None)[-3:]
    if not near:
        L.append("Teknik özet açılıştan sonraki ilk 2 dakikada hesaplanıyor; ardından adaylar değerlendirilecek.")
    else:
        L.append("En güçlü 3 coin ve neden henüz alınmadıkları:")
        for _, s in reversed(near):
            L.append(f"- {s}: {Q.explain(rows[s].get('tech') or {}, rows[s])}")
    return "\n".join(L)


def _market(sc, rows):
    strong = sorted(((r.get("tech") or {}).get("1h") or -9, s) for s, r in rows.items())
    up = sum(1 for v, _ in strong if v > 0.1)
    top = ", ".join(f"{s.split('/')[0]} ({v:+.2f})" for v, s in reversed(strong[-5:]) if v > 0.1)
    return (f"**Piyasa:** rejim {M.REGIME_TR.get(sc.get('regime'), '-')}, genişlik %{(sc.get('breadth') or 0) * 100:.0f}, "
            f"BTC 24s %{(sc.get('btc_r24') or 0) * 100:+.1f}. İzlenen {len(rows)} coinin {up} tanesinde 1 saatlik teknik "
            f"özet alım yönünde. En güçlüler: {top or '-'}.")


def _radar(b):
    rows = [o for o in b.tech.opps if o.get("full")][:5]
    if not rows:
        return "**Radar:** ilk tüm piyasa taraması sürüyor (2 dk)."
    return (f"**Radar** ({b.tech.n_all} parite tarandı, {b.tech.n_liquid} likit). En güçlüler: " + "; ".join(
        f"{o['sym'].split('/')[0]} 24s %{o.get('chg24') or 0:+.1f}, BTC'ye göre {o.get('rs24') or 0:+.1f} puan, hacim "
        f"{o.get('vspike') or 0:.1f}x" for o in rows) + ".")


def _macro(b):
    snap = b.macro.snapshot()
    if not snap["rows"]:
        return "**Borsa/Makro:** veri henüz gelmedi."
    pick = {r["sym"]: r for r in snap["rows"]}
    items = [f"{pick[s]['name']} %{pick[s]['day']:+.1f}" for s in ("^GSPC", "^NDX", "XU100.IS", "GC=F", "DX-Y.NYB")
             if s in pick and pick[s].get("day") is not None]
    g = snap["gold"]
    return (f"**Borsa/Makro:** {', '.join(items)}. Küresel risk iştahı: {snap['risk'] or '-'} ({snap['why']}). "
            f"Altın haftalık %{g.get('w1') or 0:+.1f}; bot altına PAXG/USDT ile girebilir.")


def _lessons(admin):
    s = dersler.summary(admin)
    L = [f"**Ders Defteri:** {s['n']} sonuçlanmış örnek, {s['counts']['real']} işlem otopsisi."]
    for t in s["traps"][:3]:
        L.append(f"- Tuzak: {t['label']} ({t['n']} örnek, ort {t['avg']:+.2f}R) → alım engelli")
    for g in s["golden"][:3]:
        L.append(f"- Altın kural: {g['label']} ({g['n']} örnek, ort {g['avg']:+.2f}R)")
    for x in s["lessons"][:3]:
        L.append(f"- {x['text'][:220]}")
    return "\n".join(L)


def _today(a):
    day = time.strftime("%Y-%m-%d")
    tr = [t for t in a.st["trades"] if time.strftime("%Y-%m-%d", time.localtime(t["t"])) == day]
    if not tr:
        return f"Bugün {'sanal' if a.mode == 'PAPER' else 'gerçek'} hesapta kapanan işlem yok."
    net = sum(t["pnl"] for t in tr)
    return (f"Bugün {'sanal' if a.mode == 'PAPER' else 'gerçek'} hesapta {len(tr)} işlem kapandı, net {net:+.2f} USDT: "
            + "; ".join(f"{t['sym']} %{t['pct']:+.2f} ({t['reason']})" for t in tr[-6:]))
