"""Yerel Quant karar motoru: Claude (API/CLI) yokken ayni karar bicimini ucretsiz uretir.
Kurallar: 4 zaman diliminde teknik ozet (TradingView yontemi) uyumu + emir defteri + rejim + asiri alim/pompa filtresi,
tum piyasa radari (BTC dusse de pozitif ayrisan, hacmi patlayan coinler), altin (PAXG) ve Ders Defteri'nin ogrendigi
sinyal agirliklari. Uretilen emirler Claude'unkiler gibi yerel risk yoneticisinden (bot.vet_buy) gecer."""
import config as C
import dersler as L
import gecmis as G
import model as M
from teknik import radar_score

MAX_NEW = 2  # bir kararda en fazla yeni alis
MIN_RADAR = 0.5  # radar puani esigi (eski hibrit deneyinin terfi ettirdigi deger)


def score(te, r):
    """Teknik uyum puani (yuksek = guclu yukselis kurulumu). None = uygun degil."""
    m15, h1, h4, d1 = (te.get(k) for k in ("15m", "1h", "4h", "1d"))
    if None in (m15, h1, h4):
        return None
    if h1 < 0.5 or h4 < 0.1 or m15 < 0.1 or (d1 is not None and d1 < -0.1):
        return None
    rsi, chg = r.get("rsi"), r.get("chg24") or 0
    if (rsi is not None and rsi > 72) or chg > 15:  # asiri alim / dikey pompa: kovalama
        return None
    if (r.get("ob") is not None and r["ob"] < 0.45) or (r.get("funding") or 0) > 0.0005:
        return None
    return h1 + 0.7 * h4 + 0.3 * m15 + 0.3 * (d1 or 0) + ((r.get("ob") or 0.5) - 0.5) + ((r.get("p") or 0.4) - 0.4)


def decouple_score(te, r):
    """BTC'den pozitif ayrisan coin: piyasa dusse de hacmi patlamis, BTC'yi 24 saatte en az 5 puan gecmis ve
    1s/4s trendi guclu. Dikey pompa (24s %30+), asiri alim ve satici defteri elenir."""
    m15, h1, h4 = (te.get(k) for k in ("15m", "1h", "4h"))
    if None in (m15, h1, h4) or h1 < 0.5 or h4 < 0.3 or m15 < 0.1:
        return None
    if (r.get("rs24") or 0) < 5 or (r.get("vspike") or 0) < 2:
        return None
    if (r.get("rsi") or 0) > 75 or (r.get("chg24") or 0) > 30 or (r.get("ob") is not None and r["ob"] < 0.45):
        return None
    return h1 + 0.7 * h4 + min(r["vspike"], 6) / 3 + min(r["rs24"], 30) / 10


def momentum_score(te, r):
    """Momentum / kirilim avcisi (BTC dusse de): hacim 3x+, BTC'den pozitif ayrisma, saglikli RSI (50-74), pozitif
    alici akisi ve yukari 1s trend. Balina satisi ve satici defteri elenir. Sifir zarar kalkani riski sinirlar."""
    h1, h4 = te.get("1h"), te.get("4h")
    rsi, vs, rs = r.get("rsi"), r.get("vspike") or 0, r.get("rs24") or 0
    if h1 is None or h1 < 0.1 or rsi is None or not 50 <= rsi <= 74 or vs < 3 or rs < 3:
        return None
    if (r.get("flow3") is not None and r["flow3"] <= 0) or (r.get("ob") is not None and r["ob"] < 0.4):
        return None
    if (r.get("whale") or 0) <= -0.5:
        return None
    return h1 + 0.5 * (h4 or 0) + min(vs, 10) / 4 + min(rs, 40) / 15 + (0.3 if r.get("breakout") else 0) + \
        max(r.get("whale") or 0, 0) * 0.5


def gold_score(te, r, gold):
    """Altin (PAXG): kripto zayifken guvenli liman. 1s ve 4s teknik al + ons altin haftalik pozitif."""
    h1, h4, m15 = (te.get(k) for k in ("1h", "4h", "15m"))
    if None in (h1, h4) or h1 < 0.3 or h4 < 0.3 or (m15 is not None and m15 < 0):
        return None
    if gold.get("w1") is not None and not gold.get("up"):
        return None
    return h1 + h4


def classify(r, regime=None):
    """Bir oneri (or. Claude'un) hangi kuruluma uyuyor? En yuksek puanli tur veya None (serbest fikir)."""
    te = r.get("tech") or {}
    if str(r.get("sym", "")).split("/")[0] in ("PAXG", "XAUT"):
        return "altin"
    sc = {k: f(te, r) for k, f in (("momentum", momentum_score), ("ayrisma", decouple_score), ("trend", score))}
    sc = {k: v for k, v in sc.items() if v is not None}
    return max(sc, key=sc.get) if sc else None


def explain(te, r):
    """Coin neden aday degil? (yerel analist sohbeti icin)"""
    m15, h1, h4, d1 = (te.get(k) for k in ("15m", "1h", "4h", "1d"))
    if None in (m15, h1, h4):
        return "teknik özet henüz hesaplanmadı"
    if h1 < 0.5:
        return f"1 saatlik teknik özet güçlü al değil ({h1:+.2f} < +0,50)"
    if h4 < 0.1:
        return f"4 saatlik trend desteklemiyor ({h4:+.2f})"
    if m15 < 0.1:
        return f"15 dakikalık ivme zayıf ({m15:+.2f})"
    if d1 is not None and d1 < -0.1:
        return f"günlük trend aşağı ({d1:+.2f})"
    if (r.get("rsi") or 0) > 72:
        return f"aşırı alım (RSI {r['rsi']:.0f})"
    if (r.get("chg24") or 0) > 15:
        return f"24 saatte %{r['chg24']:.0f} yükselmiş, pompa kovalanmaz"
    if r.get("ob") is not None and r["ob"] < 0.45:
        return f"emir defterinde satıcı baskın (alıcı %{r['ob'] * 100:.0f})"
    if (r.get("funding") or 0) > 0.0005:
        return "fonlama çok yüksek (kalabalık long)"
    return "kurulum uygun"


def decide(bot):
    """Claude'un karar semasiyla ayni sozluk (+ '_why': secilmeyen adaylarin sebebi, Ders Defteri icin)."""
    acct = bot.ref()
    st, regime, ctx = acct.st, bot.regime, bot.ctx()
    rows, gold = bot.rows(), bot.macro.gold()
    actions, why = [], {}
    # acik pozisyonlar: 1s ve 15dk birlikte satisa dondu -> tez bozuldu
    for s, p in st["positions"].items():
        if p.get("manual") or p.get("kind") in ("gunluk_trend", "portfoy"):  # Manuel Kilit / gunluk kol: kendi kuraliyla
            continue
        te = (rows.get(s) or {}).get("tech") or {}
        if (te.get("1h") or 0) <= -0.3 and (te.get("15m") or 0) <= -0.3:
            actions.append(_act("close", s, reason=f"Teknik özet tersine döndü (1s {te['1h']:+.2f}, 15dk {te['15m']:+.2f}); "
                                                   f"kâr/zarar beklemeden çıkıyorum."))
    busy = st["positions"].keys() | st["pending"].keys()
    cands = []
    for s, r in rows.items():
        if s in busy or not r.get("price") or acct.locked(s) or (r.get("qvol") or 0) < C.MIN_QUOTE_VOL:
            continue
        te, kind = r.get("tech") or {}, "trend"
        if s.split("/")[0] in ("PAXG", "XAUT"):
            sc, kind = gold_score(te, r, gold), "altin"
        else:
            dc, mo = decouple_score(te, r), momentum_score(te, r)
            ds = max((x for x in (dc, mo) if x is not None), default=None)  # piyasaya karsi: ayrisma / momentum
            sc = ds if regime == "dusus" else max((x for x in (score(te, r), ds) if x is not None), default=None)
            kind = ("momentum" if ds == mo else "ayrisma") if ds is not None and sc == ds else "trend"
        if sc is None:
            why[s] = "yerel motor: " + (explain(te, r) if regime != "dusus" or kind == "altin" else
                                        f"sert düşüşte sadece momentum/ayrışma kurulumu alınır (BTC'ye göre "
                                        f"{r.get('rs24') or 0:+.1f} puan, hacim {r.get('vspike') or 0:.1f}x, RSI "
                                        f"{r.get('rsi') or 0:.0f}; gereken +3 puan, 3x hacim, RSI 50-74, alıcı akışı)")
            continue
        if kind != "altin" and radar_score(r) < MIN_RADAR:
            why[s] = f"eşik: radar puanı {radar_score(r):.2f} < {MIN_RADAR:g}"
            continue
        al, gwhy = G.allowed(kind, regime)  # 2 yillik test: bu kurulum bu rejimde kazandirdi mi?
        if not al:
            why[s] = gwhy
            continue
        tk = ((bot.ex.markets.get(s) or {}).get("precision") or {}).get("price")
        if tk and tk < 1 and tk / r["price"] > 0.002:  # risk yoneticisi reddedecek: alis hakkini bosa harcama
            why[s] = f"fiyat adımı kaba (%{tk / r['price'] * 100:.2f})"
            continue
        ex = G.exit_params(kind)
        atr = min(max(r.get("atr_pct") or 0.012, 0.004), 0.06)
        ks = G.kind_stat(kind, regime)
        proven = bool(ks and ks["allowed"] and ks["avg"] > 0)  # 2 yillik test maliyet dahil pozitif: maliyet onsezgisi gereksiz
        if not proven and (2 * C.FEE + 2 * C.SLIPPAGE) / (ex["stop_atr"] * atr) > C.MAX_COST_R + 1e-9:
            why[s] = (f"stop ({ex['stop_atr']:g} ATR = %{ex['stop_atr'] * atr * 100:.2f}) komisyon+kaymaya göre çok dar: "
                      f"işlem maliyete yenir")
            continue
        sigs = L.signals(r, ctx)
        veto, mult, edge = L.check(sigs, regime)
        if veto:
            why[s] = veto
            continue
        cands.append((sc + edge, s, r, mult, sigs, kind, edge))
    cands.sort(key=lambda x: x[0], reverse=True)
    slots = 0 if acct.blocked() else MAX_NEW
    for sc, s, r, mult, sigs, kind, edge in cands[:slots]:
        px, atr = r["price"], min(max(r.get("atr_pct") or 0.012, 0.004), 0.06)
        te, ex, ks = r.get("tech") or {}, G.exit_params(kind), G.kind_stat(kind, regime)
        proven = bool(ks and ks["avg"] > 0 and ks["n"] >= 100)  # 2 yillik testte pozitif beklenti
        conv = round(min(0.9 if proven else 0.6, 0.45 + sc / 6), 2)
        risk = 0.4 + 0.6 * conv  # Ders Defteri ve adaptif carpanlar risk yoneticisinde (vet_buy) bir kez uygulanir
        if kind in ("ayrisma", "momentum") and regime == "dusus":
            risk *= 0.6  # piyasaya karsi islem: kucuk risk
        if ctx.get("macro") == "riskoff" and kind != "altin":
            risk *= 0.7
        head = G.KIND_TR.get(kind, kind)
        tp_atr = ex["tp_atr"] or 6.0  # hedefsiz (sadece iz) politikada uzak emniyet hedefi
        plan_txt = (f"Stop {ex['stop_atr']:g} ATR, hedef {tp_atr:g} ATR"
                    + (f", %{ex['part_frac'] * 100:.0f} kısmi kâr {ex['part_r']:g}R'de" if ex.get("part_frac") else "")
                    + (f", başabaş {ex['be_r']:g}R'de" if ex.get("be_r") else ", erken başabaş yok (gürültü)")
                    + (f", {ex['ts_h']} saatte ilerlemezse çıkış" if ex.get("ts_h") else "")
                    + f", en fazla {ex['max_h']} sa (Geçmiş Okulu şampiyon çıkışı).")
        extra = ""
        if kind in ("ayrisma", "momentum"):
            extra = (f" Hacim {r.get('vspike') or 0:.1f}x, BTC'ye göre 24s %{r.get('rs24') or 0:+.1f}, alıcı akışı "
                     f"{(r.get('flow3') or 0) * 100:+.1f}%" + (f", balina {r['whale']:+.2f}" if r.get("whale") is not None else "") + ".")
        elif kind == "altin":
            extra = f" Ons altın haftalık %{gold.get('w1') or 0:+.1f}."
        actions.append(_act(
            "buy", s, conviction=conv, risk_pct=round(risk, 2), stop=px * (1 - ex["stop_atr"] * atr),
            tp1=0, tp2=px * (1 + tp_atr * atr), max_hours=ex["max_h"], kind=kind,
            bull=f"Teknik özet 15dk/1s/4s {te.get('15m') or 0:+.2f}/{te.get('1h') or 0:+.2f}/{te.get('4h') or 0:+.2f}.{extra}",
            bear=f"RSI {r.get('rsi') or 0:.0f}, 24s %{r.get('chg24') or 0:+.1f}; geri çekilme stopu tetikleyebilir."
                 + ("" if proven else " Bu kurulumun 2 yıllık testte kanıtlanmış pozitif beklentisi yok."),
            reason=f"{s.split('/')[0]}: {head} (puan {sc:.2f}, Ders Defteri {edge:+.2f}R). Sinyaller: "
                   f"{L.labels(sorted(sigs)) or '-'}. {plan_txt}",
            entry="taker" if kind in ("momentum", "ayrisma") else "maker"))  # momentum kacar: hemen al
    for sc, s, *_ in cands[slots:]:
        why[s] = f"yerel motor: daha güçlü adaylar seçildi (puan {sc:.2f})"
    n_dec = sum(1 for c in cands if c[5] in ("ayrisma", "momentum"))
    view = (f"Rejim {M.REGIME_TR.get(regime, '-')}"
            + (f", küresel piyasa {ctx['macro']}" if ctx.get("macro") else "")
            + f". Yerel quant motoru: {len(rows)} coin (radar dahil) teknik uyum, BTC'den ayrışma, altın ve Ders Defteri "
              f"ağırlıklarıyla tarandı.")
    n_hist = sum(1 for w in why.values() if w.startswith("Geçmiş Okulu"))
    if regime == "dusus":
        view += (f" Sert düşüşte momentum/ayrışma: {n_dec} aday küçük riskle değerlendirildi." if cands else
                 " Sert düşüşte şu an kurallara uyan ve 2 yıllık testte kanıtlı kurulum yok.")
    elif not cands:
        view += " Şu an kanıtlı ve uyumlu kurulum yok; beklemedeyim (beklemek de bir karar)."
    if n_hist:
        view += (f" {n_hist} aday, kurulumu bu rejimde 2 yıllık testte zarar ettirdiği için Geçmiş Okulu kuralıyla "
                 f"elendi.")
    return {"market_view": view, "actions": actions, "memo": "", "_why": why}


def _act(t, s, conviction=0, risk_pct=0, stop=0, tp1=0, tp2=0, max_hours=0, bull="", bear="", reason="", entry="maker",
         kind=None):
    return {"type": t, "symbol": s, "conviction": conviction, "risk_pct": risk_pct, "stop": stop, "tp1": tp1, "tp2": tp2,
            "max_hours": max_hours, "entry": entry, "bull_case": bull, "bear_case": bear, "reason": reason, "kind": kind}
