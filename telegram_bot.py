"""Telegram mobil kumanda: sik bildirim kartlari + butonlu menu (inline keyboard) + Claude sohbeti.
Admin Ayarlar'dan @BotFather token'ini girer. Her kullanici (admin/normal) telefonundan bota /start yazar, uygulamada
Ayarlar > Telegram'da adinin yanindaki "Eslestir"e basar. Admin her seyi; normal kullanici sadece gercek hesabi
gorur ve yonetir. Eslesmemis sohbetlere hicbir veri gonderilmez."""
import html
import json
import logging
import os
import re
import threading
import time

import requests

import config as C

log = logging.getLogger("bot")
BOT = None                     # app.py motoru baglar
LINKS = os.path.join(C.APP_DIR, "telegram.json")
PENDING = {}                   # chat_id -> {"name", "username", "t"}: /start yazmis, henuz eslesmemis
ME = {"token": None, "username": None, "error": "", "t": 0, "polling": False}
_lock = threading.Lock()
esc = html.escape


# ---------------- Telegram API ----------------
def tg(method, **data):
    if not C.TELEGRAM_TOKEN:
        return None
    try:
        r = requests.post(f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/{method}", json=data, timeout=20).json()
        if not r.get("ok"):
            log.warning("Telegram %s: %s", method, r.get("description"))
        return r
    except (requests.RequestException, ValueError) as e:
        log.warning("Telegram %s: %s", method, e)
        return None


def bot_info():
    """@kullanici_adi (token gecerli mi?). 10 dk onbellek."""
    if not C.TELEGRAM_TOKEN:
        return {"username": None, "error": ""}
    if ME["token"] != C.TELEGRAM_TOKEN or time.time() - ME["t"] > 600:
        r = tg("getMe") or {}
        ME.update(token=C.TELEGRAM_TOKEN, t=time.time(), username=(r.get("result") or {}).get("username"),
                  error="" if r.get("ok") else (r.get("description") or "Telegram'a ulaşılamadı"))
        if r.get("ok"):
            tg("setMyCommands", commands=[{"command": c, "description": d} for c, d in (
                ("menu", "Kumanda menüsü"), ("kasa", "Kasa ve pozisyonlar"), ("radar", "Canlı radar (ilk 5)"),
                ("makro", "Borsa / makro özeti"), ("karar", "Son karar ve Ders Defteri"), ("demo", "Örnek bildirimler"))])
    return {"username": ME["username"], "error": ME["error"]}


def send(chat_id, text, kb=None):
    """HTML mesaj; Telegram 4096 sinirina gore bolunur. Bicim hatasinda duz metinle tekrar dener."""
    parts = [text[i:i + 3900] for i in range(0, len(text), 3900)] or [""]
    for i, p in enumerate(parts):
        extra = {"reply_markup": {"inline_keyboard": kb}} if kb and i == len(parts) - 1 else {}
        r = tg("sendMessage", chat_id=chat_id, text=p, parse_mode="HTML", disable_web_page_preview=True, **extra)
        if r is not None and not r.get("ok"):
            tg("sendMessage", chat_id=chat_id, text=re.sub(r"<[^>]+>", "", p), **extra)


# ---------------- eslesme ----------------
def links():
    with _lock:
        try:
            with open(LINKS, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = {}
    if not d and C.TELEGRAM_CHAT_ID:  # eski surum: tek admin sohbeti -> ilk admin hesabina bagla
        import auth
        adm = next((u["name"] for u in auth.users() if u["role"] == "admin"), None)
        if adm:
            d = {adm.lower(): {"user": adm, "chat_id": int(C.TELEGRAM_CHAT_ID), "name": "", "t": time.time()}}
            _save(d)
    return d


def _save(d):
    with _lock:
        tmp = LINKS + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False)
        os.replace(tmp, LINKS)


def link(user, chat_id):
    """Uygulamadaki oturum sahibi, /start yazmis sohbeti kendi hesabina baglar."""
    p = PENDING.get(int(chat_id))
    if not p:
        return "Bu sohbet bulunamadı. Telefondan bota tekrar /start yazın."
    d = {k: v for k, v in links().items() if v["chat_id"] != int(chat_id)}  # bir sohbet tek hesaba
    d[user.lower()] = {"user": user, "chat_id": int(chat_id), "name": p["name"], "t": time.time()}
    _save(d)
    PENDING.pop(int(chat_id), None)
    send(chat_id, f"✅ <b>Eşleşme tamam</b>\nBu Telegram artık <b>{esc(user)}</b> hesabına bağlı. Bildirimler buraya "
                  f"düşecek; aşağıdaki menüden botu yönetebilir veya normal mesaj yazıp Claude ile sohbet edebilirsiniz.",
         menu_kb(_role(user)))
    return None


def unlink(user):
    d = links()
    old = d.pop(user.lower(), None)
    _save(d)
    if old:
        send(old["chat_id"], "🔌 Bu Telegram'ın KriptoQuantBot bağlantısı kaldırıldı.")


def _role(user):
    import auth
    return next((u["role"] for u in auth.users() if u["name"].lower() == user.lower()), None)


def who(chat_id):
    """(kullanici, rol) veya (None, None)."""
    for v in links().values():
        if v["chat_id"] == chat_id:
            role = _role(v["user"])
            return (v["user"], role) if role else (None, None)
    return None, None


def status(user):
    info = bot_info()
    now = time.time()
    return {"token": bool(C.TELEGRAM_TOKEN), "bot": info["username"], "error": info["error"], "polling": ME["polling"],
            "linked": links().get(user.lower()),
            "pending": [{"chat_id": c, **p} for c, p in sorted(PENDING.items(), key=lambda x: -x[1]["t"])
                        if now - p["t"] < 1800][:8]}


# ---------------- bildirim kartlari ----------------
KIND = {"islem": ("💹", "İşlem"), "kalkan": ("🛡", "Sıfır Zarar Kalkanı"), "radar": ("📡", "Radar · Güçlü kırılım"),
        "karar": ("🧠", "Yeni strateji kararı"), "oz": ("📚", "Ders Defteri"), "uyari": ("⚠️", "Uyarı"),
        "sistem": ("⚙️", "Sistem"), "bekleme": ("⏳", "Karar")}
hide_paper = lambda t: re.sub(r"sanal: [^;\n]*;? ?", "", t)


def card(msg, kind, acct=None, foot=None):
    first = msg.split("\n")[0]
    sym = (re.search(r"[A-Z0-9]+/USDT", first) or [None])[0]
    emoji, title = KIND.get(kind, ("🔔", "Bildirim"))
    body = esc(msg)
    if kind == "islem" and sym:
        m = re.search(r"ALINDI ([\d.e+-]+) @ ([\d.e+-]+) \| (.*)", first)
        n = re.search(r"SATILDI \((\w+)\) @ ([\d.e+-]+) \| net ([+-][\d.]+) USDT \(([+-][\d.]+)%(?:, ([+-][\d.]+)R)?\)", first)
        if m:
            emoji, title = "🟢", f"ALIŞ · {sym}"
            body = f"Miktar <b>{m[1]}</b> @ <b>{m[2]}</b>\n" + "\n".join(
                f"• {esc(x.strip())}" for x in m[3].replace("|", ",").split(",") if x.strip())
            rest = msg.split("\n")[1:]  # Monte Carlo ihtimalleri (ikinci satir)
            if rest:
                body += "\n🎲 " + esc(" ".join(rest))
        elif n:
            win = float(n[3]) > 0
            emoji, title = ("✅", f"KÂRLA KAPANDI · {sym}") if win else ("🔻", f"ZARARLA KAPANDI · {sym}")
            body = (f"Çıkış <b>{n[2]}</b> · sebep <b>{esc(n[1])}</b>\nNet <b>{n[3]} USDT ({n[4]}%"
                    + (f", {n[5]}R" if n[5] else "") + ")</b>")
        elif "ALIŞ EMRİ" in first:
            emoji, title = "🕓", f"Pasif alış emri · {sym}"
        elif "kademeli kâr" in first:
            emoji, title = "💰", f"Kademeli kâr · {sym}"
    elif kind == "kalkan" and sym:
        title = f"{'Kâr kilidi yükseldi' if 'kilidi' in first else 'Başabaş kalkanı devrede'} · {sym}"
    elif kind == "radar" and sym:
        title = f"Radar · Güçlü kırılım · {sym}"
    elif kind == "karar":
        lines = msg.split("\n")
        body = f"<i>{esc(lines[0])}</i>\n" + "\n".join(esc(x) for x in lines[1:])
    elif kind == "oz":
        if first.startswith("Otopsi"):
            emoji, title = ("📚", "Ders Defteri · Otopsi" + (f" · {sym}" if sym else ""))
        elif "TUZAK" in first:
            emoji, title = "🚫", "Ders Defteri · Yeni tuzak kuralı"
        elif "ALTIN" in first:
            emoji, title = "🏆", "Ders Defteri · Altın kural"
        elif first.startswith("Portföy"):
            emoji, title = "📊", "Akıllı Portföy"
        elif "alınmadı" in first:
            emoji, title = "👀", "Ders Defteri · Kaçan fırsat"
    badge = {"PAPER": "🧪 Sanal kasa", "LIVE": "💵 Gerçek hesap"}.get(acct, "")
    return (f"{emoji} <b>{esc(title)}</b>" + (f"\n<code>{badge}</code>" if badge else "") + f"\n\n{body}\n\n"
            f"<i>{foot or time.strftime('%d.%m %H:%M')} · KriptoQuantBot</i>")


def notify(msg, kind, acct=None):
    """Bot.say'den: eslesmis her sohbete rolune gore kart gonderir."""
    if not C.TELEGRAM_TOKEN:
        return
    for v in links().values():
        role = _role(v["user"])
        if role == "admin":
            send(v["chat_id"], card(msg, kind, acct))
        elif role == "user" and acct in (None, "LIVE"):
            send(v["chat_id"], card(hide_paper(msg), kind, acct))


def decision_card(rec, role="admin"):
    """Ajan Komitesi karari: her oneri icin Analist Notu, Boga/Ayi, Risk Onayi, Ders Defteri/Hafiza notu."""
    head = {"saatlik": "Saatlik karar", "olay": "Olay kararı", "baslangic": "İlk karar", "elle": "Elle istenen karar",
            "tarama": "Tarama kararı", "strateji": "Strateji incelemesi", "portfoy": "Portföy stratejisi",
            "gunluk": "Günlük trend kolu"}.get(rec.get("kind"), "Karar")
    out = [f"🏛 <b>Ajan Komitesi · {esc(head)}</b>\n<i>{esc(rec.get('model', ''))} · "
           f"{time.strftime('%d.%m %H:%M', time.localtime(rec.get('t', time.time())))}</i>\n{esc(rec.get('view', '')[:500])}"]
    acts = rec.get("actions") or []
    if role != "admin":  # normal kullanici: sadece gercek hesabi ilgilendiren oneriler
        acts = [a for a in acts if "gerçek" in (a.get("result") or "") or a.get("type") != "buy"]
    if not acts:
        return None
    size, more = len(out[0]), 0
    for a in acts:
        c = a.get("card") or {}
        res = a.get("result", "") if role == "admin" else hide_paper(a.get("result", ""))
        v = c.get("verdict") or ("ONAY" if "emir verildi" in res else "RED")
        badge = "✅ ONAY" if v == "ONAY" else "⛔ VETO" if v == "VETO" else "⚪ RED"
        typ = {"buy": "AL", "close": "KAPAT", "adjust": "GÜNCELLE"}.get(a.get("type"), str(a.get("type")).upper())
        veto = str(c.get("veto") or "") if role == "admin" else hide_paper(str(c.get("veto") or ""))
        lines = [f"\n━━ <b>{typ} {esc(a.get('sym', '').split('/')[0])}</b> · {esc(c.get('kind_tr') or '-')} ━━",
                 f"{badge}" + (f": {esc(veto[:200])}" if v != "ONAY" and veto else "")]
        an = c.get("analyst") or {}
        if an:
            lines.append("🔎 <b>Analist</b>: " + esc(" · ".join(x for x in (an.get("teknik"), an.get("zincir"), an.get("makro"))
                                                               if x)[:600]))
        if c.get("bull"):
            lines.append("🐂 <b>Boğa</b>: " + esc("; ".join(c["bull"][:4])[:400]))
        if c.get("bear"):
            lines.append("🐻 <b>Ayı</b>: " + esc("; ".join(c["bear"][:4])[:400]))
        mc = c.get("mc")
        if mc:
            lines.append(f"🎲 <b>Monte Carlo</b> ({mc['n']} yol): 🟩 Hedef önce %{mc['tp'] * 100:.1f} · 🟥 Stop önce "
                         f"%{mc['stop'] * 100:.1f} · ⬜ Nötr %{mc['neutral'] * 100:.1f} · beklenen {mc['ev_r']:+.2f}R")
        if c.get("checks"):
            lines.append("🛡 <b>Risk</b>: " + " · ".join(("✓ " if k["ok"] else "✗ ") + esc(k["name"]) for k in c["checks"]))
        if c.get("memory"):
            lines.append("📚 <b>Hafıza</b>: " + esc(c["memory"][:300]))
        lines.append(f"→ {esc(res[:200])}")
        blk = "\n".join(lines)
        if size + len(blk) > 3700:  # Telegram 4096 sinir: HTML etiketini ortadan kesme, blok sinirinda dur
            more += 1
            continue
        out.append(blk)
        size += len(blk) + 1
    if more:
        out.append(f"\n… +{more} öneri daha (uygulamada Kararlar sayfası)")
    return "\n".join(out)


def notify_decision(rec):
    if not C.TELEGRAM_TOKEN:
        return
    for v in links().values():
        role = _role(v["user"])
        if role in ("admin", "user"):
            txt = decision_card(rec, role)
            if txt:
                send(v["chat_id"], txt)


DEMO = [  # sunum icin: tum bildirim tipleri (gercek bicimle, DEMO etiketli)
    ("{s} ALINDI 0.42 @ {p} | stop {sl}, kısmi kâr {t1}, hedef {t2}, en fazla 24 sa\nMonte Carlo (10.000 yol / 24 sa): "
     "Hedef önce %38.2 | Stop önce %41.5 | Nötr %20.3 | beklenen +0.06R", "islem", "LIVE"),
    ("{s} kâra geçti: stop başabaşın üstüne ({be}) çekildi. Bu işlem artık zararla kapanmaz (ani fiyat boşluğu hariç); "
     "kâr ilerledikçe stop arkadan kilitleyerek izleyecek.", "kalkan", "LIVE"),
    ("{s} kâr kilidi yükseldi: stop {lk}, girişe göre %+1.20 kâr kilitli.", "kalkan", "LIVE"),
    ("{s} SATILDI (IZ_STOP) @ {ex} | net +6.84 USDT (+2.31%)", "islem", "LIVE"),
    ("ARK/USDT güçlü kırılım: 24 saatlik zirve kırıldı, hacim 5.9x, BTC'ye göre +27.4 puan, 24s %+29.6, 1s/4s teknik "
     "+0.71/+0.62, fiyat 0.2338.", "radar", None),
    ("Olay kararı (Claude CLI (opus), 18 sn): Piyasa genelinde zayıflık var ama {s} BTC'den pozitif ayrışıyor; hacim "
     "patlaması ve 4s trend uyumlu.\nTetik: RADAR: {s} hacim 4.2x\n• BUY {s}: Kırılım sonrası hacimli devam, stop yapı "
     "altında -> gerçek: emir verildi", "karar", None),
    ("Otopsi: ETH/USDT zararla kapandı: %-0.82 (stop, 1.1 sa, -0.95R). Girişte teknik 15dk/1s/4s/1g +0.30/+0.55/+0.05/"
     "-0.20, RSI 73. Sebep: aşırı alım bölgesinde girildi; üst zaman dilimi trendi desteklemiyordu. Ders: benzer "
     "kurulumda risk yarıya iner; aynı kombinasyon tekrar zarar ettirirse alım engellenir.", "oz", "LIVE"),
    ("TUZAK kuralı öğrenildi: aşırı alım (RSI 70+) + 4s trend zayıf birlikteyken 12 örnekte kazanma %8, ortalama "
     "-0.71R. Bu kombinasyonda yeni alım artık risk yöneticisi tarafından engelleniyor.", "oz", None),
    ("Portföy stratejisi (yerel, 60 sn): Çekirdek (%70): BTC trend 75% → %21, ETH trend 50% → %14 | ML sepeti "
     "(%30): BTC, ETH, LTC, BNB, ADA | Hedef: BTC %27, ETH %20, LTC %6, BNB %6, ADA %6, nakit %35\n• Yeniden dengele"
     ": hedef ağırlıklar -> sanal: BTC/USDT al: ağırlık %21.0 → %27.0", "karar", "PAPER")]


def send_demo(chat_id):
    s = "SOL/USDT"
    p = (BOT.bid.get(s) if BOT else None) or 180.0
    v = {"s": s, "p": f"{p:.4g}", "sl": f"{p * 0.982:.4g}", "t1": f"{p * 1.018:.4g}", "t2": f"{p * 1.038:.4g}",
         "be": f"{p * 1.004:.4g}", "lk": f"{p * 1.012:.4g}", "ex": f"{p * 1.023:.4g}"}
    send(chat_id, "🎬 <b>Örnek bildirimler</b>\nAşağıdaki kartlar demo amaçlıdır; gerçek işlem değildir. Gerçek "
                  "bildirimler birebir bu biçimde düşer.")
    for text, kind, acct in DEMO:
        send(chat_id, card(text.format(**v), kind, acct, foot="🎬 DEMO · örnek bildirim"))
        time.sleep(0.6)
    send(chat_id, "Kumanda menüsü:", menu_kb(who(chat_id)[1] or "user"))


# ---------------- butonlu kumanda ----------------
b = lambda t, d: {"text": t, "callback_data": d}


def _accts(role):
    return [a for m, a in BOT.accounts.items() if role == "admin" or m == "LIVE"] if BOT else []


def _paused(role):
    ms = ("PAPER", "LIVE") if role == "admin" else ("LIVE",)
    return all(os.path.exists(C.pause_flag(m)) for m in ms)


def menu_kb(role):
    rows = [[b("💰 Kasa & Pozisyonlar", "kasa")], [b("📡 Canlı Radar (İlk 5)", "radar"), b("🌍 Borsa / Makro", "makro")],
            [b("🧠 Son Karar & Ders Defteri", "karar")]]
    if role == "admin":
        rows.append([b("⚡ Şimdi Analiz Et", "analiz")])
    rows += [[b("▶️ Botu Başlat" if _paused(role) else "⏸ Botu Duraklat", "pause"), b("🚨 Acil Tümünü Kapat", "panik")],
             [b("🎬 Örnek Bildirimler (Demo)", "demo")]]
    return rows


def menu_text(user, role):
    brain = ""
    if BOT:
        import agent as A
        brain = {"api": "Claude API", "cli": "Claude (abonelik)", "local": "Yerel Quant Motoru"}.get(A.brain(), "")
    return (f"🤖 <b>KriptoQuantBot · Mobil Kumanda</b>\n{esc(user)} · {'admin' if role == 'admin' else 'kullanıcı'}"
            + (f" · beyin: {brain}" if brain else "") + f"\nDurum: {esc(BOT.status if BOT else 'motor başlatılıyor')}"
            + (" · ⏸ duraklatıldı" if _paused(role) else "")
            + "\n\nNormal mesaj yazarsanız Claude ile sohbet edersiniz.")


def view_kasa(role):
    if not BOT:
        return "Motor başlatılıyor..."
    out = []
    with BOT.lock:
        accts = _accts(role)
        if not accts:
            return "💵 Gerçek hesap bağlı değil. Uygulamada Ayarlar → Binance hesabım."
        for a in accts:
            st, eq = a.st, a.equity()
            mult, why = a.risk_mult()
            n = st["wins"] + st["losses"]
            out.append(f"{'🧪 <b>Sanal kasa</b>' if a.mode == 'PAPER' else '💵 <b>Gerçek hesap</b>'}\n"
                       f"Kasa <b>{eq:,.2f} $</b> · bugün {a.day_pct():+.2f}% · net {st['pnl']:+.2f} $\n"
                       f"{n} işlem · kazanma %{st['wins'] / n * 100 if n else 0:.0f} · kartopu ×{mult} ({esc(why)})"
                       + (f"\n⛔ {esc(a.blocked())}" if a.blocked() else ""))
            for s, p in st["positions"].items():
                px = BOT.bid.get(s, p["entry"])
                lock = (p["sl"] / p["entry"] - 1) * 100
                sh = "🔒 manuel kilit" if p.get("manual") else f"🛡 kâr kilitli %{lock:+.2f}" if p.get("be") and lock > 0.35 \
                    else "🛡 başabaş" if p.get("be") else "açık"
                out.append(f"  • <b>{s.split('/')[0]}</b> {px:.6g} ({(px / p['entry'] - 1) * 100:+.2f}%) · stop "
                           f"{p['sl']:.6g} · {sh}")
            for s in st["pending"]:
                out.append(f"  • {s.split('/')[0]} pasif alış emri bekliyor")
            if not st["positions"] and not st["pending"]:
                out.append("  • açık pozisyon yok")
    return "💰 <b>Kasa & Pozisyonlar</b>\n\n" + "\n".join(out)


def view_radar():
    if not BOT:
        return "Motor başlatılıyor..."
    t = BOT.tech
    rows = [o for o in t.opps if o.get("full")][:5]
    if not rows:
        return "📡 Radar ilk taramayı yapıyor (1-2 dk)."
    f = lambda v: "-" if v is None else f"{v:+.2f}"
    return (f"📡 <b>Canlı Radar · İlk 5</b>\n{t.n_all} parite tarandı · {t.n_liquid} likit\n\n" + "\n\n".join(
        f"<b>{i + 1}. {o['sym'].split('/')[0]}</b>{' 🚀 kırılım' if o.get('breakout') else ''}\n24s {o.get('chg24') or 0:+.1f}% · "
        f"BTC'ye göre {o.get('rs24') or 0:+.1f} · hacim {o.get('vspike') or 0:.1f}x\nteknik 1s/4s {f((o.get('tech') or {}).get('1h'))}"
        f"/{f((o.get('tech') or {}).get('4h'))} · RSI {o.get('rsi') or 0:.0f} · puan {o.get('score')}" for i, o in enumerate(rows)))


def view_makro():
    from makro import Makro
    s = Makro.shared().snapshot()
    if not s["rows"]:
        return "🌍 Makro veri bekleniyor."
    R = {"riskoff": "🔴 RİSK-OFF", "riskon": "🟢 RİSK-ON", "notr": "⚪ NÖTR"}.get(s["risk"], "-")
    pick = {r["sym"]: r for r in s["rows"]}
    line = lambda k: (f"{pick[k]['name']}: <b>{pick[k]['price']:,.2f}</b> ({pick[k]['day']:+.2f}%)"
                      if k in pick and pick[k].get("price") and pick[k].get("day") is not None else "")
    ks = ("^GSPC", "^NDX", "XU100.IS", "^VIX", "DX-Y.NYB", "GC=F", "CL=F", "NVDA", "TSLA")
    g = s["gold"]
    return (f"🌍 <b>Borsa / Makro</b>\nRisk iştahı: <b>{R}</b> · {esc(s['why'])}\n\n" + "\n".join(x for x in map(line, ks) if x)
            + f"\n\n🥇 Altın haftalık {g.get('w1') or 0:+.1f}% · "
              + ("PAXG adayı (altın güçlü)" if g.get("up") else "PAXG alımı yok (altın zayıf)"))


def view_karar(role):
    import dersler
    out = []
    if BOT and BOT.ag["decisions"]:
        d = BOT.ag["decisions"][-1]
        withc = [x for x in BOT.ag["decisions"][-20:] if x.get("actions")]
        card = decision_card(withc[-1], role) if withc else None
        if card:  # son komite karari (kartli)
            out.append(card)
        if d is not (withc[-1] if withc else None):
            out.append(f"🧠 <b>Son değerlendirme</b> · {time.strftime('%d.%m %H:%M', time.localtime(d['t']))} · "
                       f"{esc(d.get('model', ''))}\n<i>{esc(d['view'][:400])}</i>\n• Bu değerlendirmede yeni işlem yok.")
    else:
        out.append("🧠 Henüz karar yok.")
    s = dersler.summary(role == "admin")
    pf = (BOT.ag.get("pf") or {}) if BOT else {}
    extra = [f"\n📚 <b>Ders Defteri</b> · {s['n']} örnek · {len(s['traps'])} tuzak · {len(s['golden'])} altın kural"] + \
        [f"• {esc(x['text'][:260])}" for x in s["lessons"][:3]]
    if pf.get("targets"):
        extra.append("\n📊 <b>Portföy hedefi</b> · " + ", ".join(
            f"{k.split('/')[0]} %{v * 100:.1f}" for k, v in sorted(pf["targets"].items(), key=lambda x: -x[1]) if v > 0))
    size = sum(len(x) + 1 for x in out)
    for x in extra:  # Telegram 4096 karakter siniri
        if size + len(x) < 4000:
            out.append(x)
            size += len(x) + 1
    return "\n".join(out)


def do_pause(role):
    on = not _paused(role)
    for m in (("PAPER", "LIVE") if role == "admin" else ("LIVE",)):
        f = C.pause_flag(m)
        if on:
            open(f, "w").close()
        elif os.path.exists(f):
            os.remove(f)
    return "⏸ <b>Duraklatıldı</b>: yeni işlem açılmayacak, açık pozisyonlar korunuyor." if on else \
        "▶️ <b>Bot yeniden başladı</b>: yeni işlem açabilir."


def do_panic(user, role):
    import ccxt
    if not BOT:
        return "Motor başlatılıyor..."
    done, errs = 0, []
    with BOT.lock:
        for a in _accts(role):
            for s in list(a.st["positions"].keys() | a.st["pending"].keys()):
                try:
                    a.close(s, "ELLE_KAPATILDI")
                    done += 1
                except ccxt.BaseError as e:
                    errs.append(f"{s}: {str(e)[:80]}")
    BOT.say(f"Telegram'dan acil kapatma ({user}): {done} pozisyon/emir kapatıldı.", kind="islem")
    return f"🚨 <b>Acil kapatma</b>: {done} pozisyon/emir kapatıldı." + (f"\nHata: {esc('; '.join(errs))}" if errs else "")


def do_analyze(chat_id):
    if not BOT:
        return send(chat_id, "Motor başlatılıyor...")
    n0 = len(BOT.ag["decisions"])

    def run():
        BOT.decide("elle")
        if len(BOT.ag["decisions"]) > n0:
            send(chat_id, view_karar("admin"), menu_kb("admin"))
    if BOT._background("Telegram analizi", run):
        send(chat_id, "⚡ <b>Analiz başladı</b>: piyasa, radar ve Ders Defteri taranıyor; karar gelince buraya düşecek.")
    else:
        send(chat_id, "⏳ Motor şu an başka bir analizle meşgul; birazdan tekrar deneyin.")


def on_callback(q):
    chat_id, mid, data = q["message"]["chat"]["id"], q["message"]["message_id"], q.get("data") or ""
    user, role = who(chat_id)
    tg("answerCallbackQuery", callback_query_id=q["id"])
    if not user:
        return
    views = {"kasa": lambda: view_kasa(role), "radar": view_radar, "makro": view_makro, "karar": lambda: view_karar(role)}
    if data == "menu":
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=menu_text(user, role), parse_mode="HTML",
           reply_markup={"inline_keyboard": menu_kb(role)})
    elif data in views or data.startswith("r:"):
        key = data[2:] if data.startswith("r:") else data
        tg("editMessageText", chat_id=chat_id, message_id=mid, text=views[key]()[:3900], parse_mode="HTML",
           disable_web_page_preview=True, reply_markup={"inline_keyboard": [[b("◀️ Menü", "menu"), b("🔄 Yenile", "r:" + key)]]})
    elif data == "pause":
        send(chat_id, do_pause(role), menu_kb(role))
    elif data == "panik":
        send(chat_id, "🚨 <b>Tüm açık pozisyonlar piyasa fiyatından kapatılsın ve bekleyen emirler iptal edilsin mi?</b>",
             [[b("✅ Evet, hepsini kapat", "panik_ok"), b("Vazgeç", "menu_new")]])
    elif data == "panik_ok":
        send(chat_id, do_panic(user, role), menu_kb(role))
    elif data == "menu_new":
        send(chat_id, menu_text(user, role), menu_kb(role))
    elif data == "analiz" and role == "admin":
        do_analyze(chat_id)
    elif data == "demo":
        send_demo(chat_id)


def on_message(msg):
    chat_id, text = msg["chat"]["id"], (msg.get("text") or "").strip()
    user, role = who(chat_id)
    if not user:
        frm = msg.get("from") or {}
        PENDING[chat_id] = {"name": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x) or "Telegram",
                            "username": frm.get("username"), "t": time.time()}
        send(chat_id, "👋 <b>KriptoQuantBot</b>\nBu Telegram henüz bir hesaba bağlı değil.\n\nBilgisayardaki uygulamada "
                      "<b>Ayarlar → Telegram bağlantısı</b> bölümünde adınızın yanındaki <b>Eşleştir</b> düğmesine basın. "
                      f"Görünen ad: <code>{esc(PENDING[chat_id]['name'])}</code>")
        return
    cmd = text.split()[0].lower().split("@")[0] if text.startswith("/") else ""
    views = {"/kasa": lambda: view_kasa(role), "/radar": view_radar, "/makro": view_makro, "/karar": lambda: view_karar(role),
             "/durum": lambda: view_kasa(role)}
    if cmd in ("/start", "/menu", "/yardim"):
        send(chat_id, menu_text(user, role), menu_kb(role))
    elif cmd in views:
        send(chat_id, views[cmd](), [[b("◀️ Menü", "menu_new")]])
    elif cmd == "/demo":
        send_demo(chat_id)
    elif cmd in ("/durdur", "/devam"):
        if (cmd == "/durdur") != _paused(role):
            send(chat_id, do_pause(role))
    elif cmd:
        send(chat_id, "Komutlar: /menu /kasa /radar /makro /karar /demo — ya da normal bir soru yazın.")
    elif BOT and text:
        import chat
        tg("sendChatAction", chat_id=chat_id, action="typing")
        path = os.path.join(C.APP_DIR, f"sohbet_tg_{re.sub(r'[^a-z0-9_.-]', '_', user.lower())}.json")
        ans = "".join(chat.ask_stream(text[:2000], BOT, role, path))
        send(chat_id, md(ans) or "Cevap gelmedi.")


def md(t):
    """Sohbet cevabindaki kucuk markdown -> Telegram HTML."""
    t = esc(t)
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    t = re.sub(r"(?m)^#{1,4}\s+(.*)$", r"<b>\1</b>", t)
    t = re.sub(r"(?m)^\s*[-*]\s+", "• ", t)
    return re.sub(r"(?<![\w*])_(\(.+?\))_", r"<i>\1</i>", t)


def poll_forever():
    """Uzun sorgu (getUpdates): token degisirse kendiliginden yeni bota gecer. Her mesaj ayri thread'de islenir."""
    offset, tok = None, None
    while True:
        if not C.TELEGRAM_TOKEN:
            ME["polling"] = False
            time.sleep(5)
            continue
        if tok != C.TELEGRAM_TOKEN:
            tok, offset = C.TELEGRAM_TOKEN, None
            bot_info()
        try:
            r = requests.get(f"https://api.telegram.org/bot{tok}/getUpdates", timeout=40,
                             params={"timeout": 25, "offset": offset,
                                     "allowed_updates": json.dumps(["message", "callback_query"])}).json()
            if not r.get("ok"):
                ME.update(polling=False, error=r.get("description") or "Telegram hatası")
                time.sleep(10)
                continue
            ME["polling"] = True
            for u in r["result"]:
                offset = u["update_id"] + 1
                fn, arg = (on_callback, u["callback_query"]) if "callback_query" in u else (on_message, u.get("message"))
                if arg and (fn is on_callback or "text" in arg):
                    threading.Thread(target=_safe, args=(fn, arg), daemon=True).start()
        except (requests.RequestException, ValueError) as e:
            ME["polling"] = False
            log.warning("Telegram dinleme: %s", e)
            time.sleep(10)


def _safe(fn, arg):
    try:
        fn(arg)
    except Exception:
        log.exception("Telegram islemi")
