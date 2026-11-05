"""Gercek paraya hazirlik raporu (sadece admin): sanal gozlem sonucu, dengeleme sureklligi, anahtar ve sinir kontrolleri, felaket stopunun Binance filtresi.
Kontrol gercek emir vermez: /api/v3/order/test emri eslesme motoruna gondermez. Karar kullanicinindir; bir haftalik gozlem stratejiyi kanitlamaz."""
import csv
import datetime
import json
import logging
import os
import time

import ccxt

import config as C
import portfoy as PF

log = logging.getLogger("kqb")
BAS = "2026-11-03"     # btc_sma200 duzeltmesi 2 Kasim'da sunuculara ulasti: oncesi sayilmaz
GUN_MIN = 7            # hazir saymak icin en az gozlem gunu
DUSUS_MAX = 28.0       # gozlem dususu test dususunu (7 gun ortalamasi -%28) gecmesin
EN_BUYUK_PAY = 0.41    # tek coinde ilk alim: cekirdek %35 + sepet %6
SATIR = 20000
KOINLER = ("BTC/USDT", "ETH/USDT")


def gozlem(path=None, bas=BAS):
    """gozlem.csv'den sanal hesabin BAS sonrasi seyri. Veri yoksa None."""
    try:
        with open(path or os.path.join(C.APP_DIR, "gozlem.csv"), encoding="utf-8", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r.get("hesap") == "PAPER" and (r.get("zaman") or "") >= bas][-SATIR:]
    except OSError:
        return None
    pts = []
    for r in rows:
        try:
            pts.append((r["zaman"], float(r["kasa_net"]), float(r["btc"]), r))
        except (KeyError, TypeError, ValueError):
            continue
    if len(pts) < 2:
        return None
    t = lambda z: time.mktime(time.strptime(z, "%Y-%m-%d %H:%M"))
    peak, dd, uyari = 0.0, 0.0, {}
    for _, eq, _, r in pts:
        peak = max(peak, eq)
        dd = max(dd, (1 - eq / peak) * 100 if peak else 0.0)
        for w in filter(None, (x.strip() for x in (r.get("uyarilar") or "").split(" | "))):
            uyari[w] = uyari.get(w, 0) + 1
    a, z = pts[0], pts[-1]
    saat = (t(z[0]) - t(a[0])) / 3600
    btc = (z[2] / a[2] - 1) * 100 if a[2] > 0 and z[2] > 0 else None
    return {"bas": a[0], "son": z[0], "gun": round(saat / 24, 1), "kapsam": round(min(1.0, (len(pts) - 1) / saat) * 100) if saat > 0 else 0,
            "kasa_bas": a[1], "kasa_son": z[1], "kasa_pct": round((z[1] / a[1] - 1) * 100, 2) if a[1] > 0 else None,
            "btc_pct": round(btc, 2) if btc is not None else None, "dusus": round(dd, 2), "engel_saat": sum(bool(p[3].get("engel")) for p in pts),
            "uyarilar": [{"m": k, "n": v} for k, v in sorted(uyari.items(), key=lambda x: -x[1])[:5]]}


def dengelemeler(klasor=None, bas=BAS, bugun=None):
    """Dengeleme defterinden gun gun sanal hesap sonucu. Bugun 00:05 UTC'den once henuz beklenmez."""
    klasor = klasor or C.REBAL_DIR
    bugun = bugun or time.strftime("%Y-%m-%d", time.gmtime())
    out, g = [], datetime.date.fromisoformat(bas)
    while g.isoformat() <= bugun:
        d = g.isoformat()
        g += datetime.timedelta(days=1)
        ok, hata = False, []
        try:
            with open(os.path.join(klasor, d + ".jsonl"), encoding="utf-8") as fh:
                for line in fh:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue
                    if r.get("hesap") != "PAPER" or r.get("asama") != "sonuc":
                        continue
                    son = r.get("sonuc")
                    ok = ok or son is not None
                    hata = [str(x)[:120] for x in son or [] if "hatası" in str(x) or "reddetti" in str(x)]
        except OSError:
            pass
        if d == bugun and not ok and time.time() % 86400 < 1800:
            continue
        out.append({"g": d, "ok": ok, "hata": hata})
    return out


def binance_kontrol(acct, bid):
    """Felaket stopu Binance filtresinden gecer mi? Gercek emir gitmez. Reddedilirse duz STOP_LOSS da denenir (yedek yol)."""
    ex, sonuc = acct.ex, []
    for s in KOINLER:
        px = bid.get(s)
        try:
            px = px or float(ex.fetch_ticker(s)["last"])
            mk = ex.market(s)
            en_az = (mk.get("limits", {}).get("cost", {}).get("min") or 5) * 2
            sp = float(ex.price_to_precision(s, px * PF.STOP_FRAC))
            taban = {"symbol": mk.get("id") or s.replace("/", ""), "side": "SELL", "quantity": ex.amount_to_precision(s, en_az / sp)}
            satir = {"s": s, "fiyat": px, "stop": sp}
            try:
                ex.private_post_order_test({**taban, "type": "STOP_LOSS_LIMIT", "timeInForce": "GTC", "stopPrice": ex.price_to_precision(s, sp),
                                            "price": ex.price_to_precision(s, sp * 0.995)})
                satir["ok"] = True
            except ccxt.AuthenticationError:
                raise
            except ccxt.BaseError as e:
                satir.update(ok=False, hata=str(e)[:160])
                try:
                    ex.private_post_order_test({**taban, "type": "STOP_LOSS", "stopPrice": ex.price_to_precision(s, sp)})
                    satir["yedek"] = True
                except ccxt.BaseError as e2:
                    satir.update(yedek=False, yedek_hata=str(e2)[:160])
            sonuc.append(satir)
        except ccxt.AuthenticationError as e:
            return {"t": time.time(), "ok": False, "hata": f"Binance anahtarı reddedildi: {str(e)[:120]}", "koin": sonuc}
        except (ccxt.BaseError, AttributeError, KeyError, TypeError, ValueError) as e:
            log.warning("hazirlik binance: %s", e)
            sonuc.append({"s": s, "ok": False, "hata": f"Kontrol yapılamadı: {str(e)[:120]}", "belirsiz": True})
    return {"t": time.time(), "ok": bool(sonuc) and all(x.get("ok") for x in sonuc), "koin": sonuc}


def rapor(bot, kontrol=None):
    """Ekranin tum verisi. Madde durumu: ok / uyari / bekle. Hazir: hepsi ok."""
    gz, dg = gozlem(), dengelemeler()
    lv = bot.accounts.get("LIVE")
    M = []
    add = lambda ad, d, n: M.append({"ad": ad, "durum": d, "not": n})
    if not gz:
        add("Sanal gözlem", "bekle", f"{BAS} sonrası gözlem kaydı henüz yok. Kayıt her saat yazılır.")
    else:
        add("Sanal gözlem süresi", "ok" if gz["gun"] >= GUN_MIN else "bekle", f"{gz['gun']:g} gün (en az {GUN_MIN}), kayıt kapsamı %{gz['kapsam']}.")
        add("Düşüş", "ok" if gz["dusus"] <= DUSUS_MAX else "uyari", f"Dönemin en derin düşüşü %{gz['dusus']:g}. Test ortalaması yaklaşık %28.")
    eksik = [x["g"] for x in dg if not x["ok"]]
    hatali = [x["g"] for x in dg if x["hata"]]
    add("Günlük dengeleme", "uyari" if eksik or hatali else "ok" if dg else "bekle",
        ("Eksik gün: " + ", ".join(eksik) + ". " if eksik else "") + ("Hatalı gün: " + ", ".join(hatali) + ". " if hatali else "") + ("" if eksik or hatali else f"{len(dg)} günün hepsi tamamlandı."))
    if not C.API_KEY or not lv:
        add("Binance anahtarı", "bekle", "Gerçek hesap anahtarı girilmedi. Anahtarı Ayarlar'dan girin, sohbete yazmayın.")
    else:
        eq = lv.equity() if lv.synced else 0.0
        add("Binance bağlantısı", "ok" if lv.synced and not lv.error else "uyari", lv.error or (f"Kasa {eq:.2f} USDT." if lv.synced else "Bakiye henüz okunmadı."))
        add("Para çekme izni", "ok" if lv.key_ok and not lv.key_na else "uyari" if lv.key_ok is False else "bekle",
            lv.key_err or ("Binance izinleri bu hesapta okunamıyor: çekim iznini API Yönetimi'nden kendiniz doğrulayın." if lv.key_na else "Kapalı." if lv.key_ok else "Henüz okunmadı."))
        en = eq * EN_BUYUK_PAY
        add("Tek emir sınırı", ("ok" if en <= C.LIVE_MAX_USDT else "uyari") if eq > 0 else "bekle",
            f"En büyük alım yaklaşık {en:.0f} USDT olabilir, sınır {C.LIVE_MAX_USDT:g} USDT." + (" Aşan emir reddedilir: kasaya göre yükseltin." if en > C.LIVE_MAX_USDT else ""))
        add("Felaket stopu (Binance)", "bekle" if not kontrol else "ok" if kontrol.get("ok") else "uyari",
            "Henüz kontrol edilmedi. 'Binance'te kontrol et' düğmesi gerçek emir vermez." if not kontrol else "İki coinde de filtreden geçti." if kontrol.get("ok") else
            kontrol.get("hata") or "Reddedildi: gerçek hesapta stop emri kurulamaz, kod değişikliği gerekir. Sonucu geliştiriciye iletin.")
    if EN_BUYUK_PAY > C.LIVE_MAX_EQUITY:
        add("Kasa payı sınırı", "uyari", f"Tek coinde ilk alım kasanın %{EN_BUYUK_PAY * 100:.0f}'ine çıkabilir, sınır %{C.LIVE_MAX_EQUITY * 100:.0f}. Bu olursa o alım reddedilir, küçültülmez.")
    return {"bas": BAS, "gozlem": gz, "dengeleme": dg, "maddeler": M, "kontrol": kontrol, "hazir": bool(M) and all(m["durum"] == "ok" for m in M),
            "ayar": {"LIVE_ALLOWED": C.LIVE_ALLOWED, "PORTFOY_LIVE": C.PORTFOY_LIVE, "EARN_USDT": C.EARN_USDT, "HABER_LIVE": C.HABER_LIVE, "LIVE_MAX_USDT": C.LIVE_MAX_USDT}}
