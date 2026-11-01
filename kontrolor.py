"""Claude kontrolor (portfoy alimlari icin) + karnesi.
Gunluk dengelemede alinacak coinleri Claude inceler; SADECE alimi ertele / yariya indir diyebilir (satislara karisamaz).
Her itiraz 7 gun sonra puanlanir: coin o 7 gunde dustuyse itiraz dogruydu. Son 20 itirazin isabeti %50'nin altina
inerse itirazlar uygulanmaz, sadece kaydedilir (golge mod); tekrar ustune cikinca soz hakki geri gelir."""
import json
import os
import threading
import time

import config as C

FILE = os.path.join(C.APP_DIR, "kontrolor.json")
_LOCK = threading.Lock()
MIN_N, WINDOW = 5, 20
SYSTEM = ("Sen bir kripto portfoyunun kontrolorusun. Bot kanitlanmis bir kurala gore (trend + oynaklik, haftalik ML "
          "secimi) bugun bazi coinleri ALACAK. Gorevin: bu alimlardan biri bariz tehlikeliyse (dikey pompa sonrasi, "
          "cokus ortasinda, coinle ilgili cok kotu durum) ertele ya da yariya indir. Emin degilsen itiraz ETME; "
          "gereksiz itiraz karneni bozar ve soz hakkini kaybettirir. 'ozet' alanina bugunku plani hic bilmeyen birine "
          "anlatir gibi 1-2 kisa, sade Turkce cumleyle yaz (teknik terim yok).")
SCHEMA = {"type": "object", "additionalProperties": False, "required": ["ozet", "itirazlar"], "properties": {
    "ozet": {"type": "string"},
    "itirazlar": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["sym", "karar", "neden"],
        "properties": {"sym": {"type": "string"}, "karar": {"type": "string", "enum": ["ertele", "yarim"]},
                       "neden": {"type": "string"}}}}}}


def _load():
    try:
        with open(FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"items": []}


def _save(d):
    tmp = FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(d, fh, ensure_ascii=False)
    os.replace(tmp, FILE)


def card():
    """Karne: isabet, ortalama kacinilan dusus, soz hakki."""
    with _LOCK:
        it = _load()["items"]
    done = [x for x in it if x.get("dogru") is not None][-WINDOW:]
    hit = sum(x["dogru"] for x in done) / len(done) if done else None
    gain = sum(-x["ret7"] for x in done) / len(done) * 100 if done else None
    active = len(done) < MIN_N or hit >= 0.5
    return {"n": len(done), "bekleyen": sum(1 for x in it if x.get("dogru") is None), "isabet": hit,
            "fayda_pct": gain, "aktif": active, "son": it[-5:][::-1]}


def score(ex):
    """7 gunu dolan itirazlari puanla (gunluk mum: itirazdan 7 gun sonraki kapanis)."""
    with _LOCK:
        d = _load()
        todo = [x for x in d["items"] if x.get("dogru") is None and time.time() - x["t"] >= 7 * 86400]
    for x in todo:
        try:
            k = ex.fetch_ohlcv(x["sym"], "1d", since=int((x["t"] + 7 * 86400) * 1000), limit=1)
            if k:
                x["ret7"] = round(k[0][4] / x["px"] - 1, 4)
                x["dogru"] = x["ret7"] < 0  # ertelenen/kucultulen alim o hafta dustu: itiraz isabetli
        except Exception:
            continue
    if todo:
        with _LOCK:
            cur = _load()
            byid = {(x["t"], x["sym"]): x for x in todo}
            cur["items"] = [byid.get((x["t"], x["sym"]), x) for x in cur["items"]][-300:]
            _save(cur)


def review(ask, buys, lines):
    """buys: [(sym, hedef_agirlik, simdiki_agirlik, fiyat, aciklama)]. ask: agent.ask_json.
    Donus: (ozet, {sym: 'ertele'|'yarim'} uygulanacaklar, kayit notu)."""
    kc = card()
    hist = (f"Karnen: son {kc['n']} itirazin isabeti %{kc['isabet'] * 100:.0f}"
            if kc["isabet"] is not None else "Karnen: henuz puanlanmis itirazin yok")
    prompt = "\n".join(lines + ["", "BUGUN ALINACAKLAR:"] + [
        f"- {s}: kasadaki payi %{c * 100:.1f} -> %{t * 100:.1f}. {why}" for s, t, c, _, why in buys] + ["", hist])
    dec, usd, model = ask(SYSTEM, prompt, SCHEMA)
    known = {b[0]: b for b in buys}
    objs = [o for o in dec.get("itirazlar") or [] if o.get("sym") in known]
    now = time.time()
    with _LOCK:
        d = _load()
        d["items"] = (d["items"] + [{"t": now, "sym": o["sym"], "karar": o["karar"], "neden": str(o["neden"])[:300],
                                     "px": known[o["sym"]][3], "uygulandi": kc["aktif"], "model": model}
                                    for o in objs])[-300:]
        _save(d)
    apply = {o["sym"]: o["karar"] for o in objs} if kc["aktif"] else {}
    note = ("Claude itiraz etmedi." if not objs else
            ("; ".join(f"{o['sym'].split('/')[0]} {'ertelendi' if o['karar'] == 'ertele' else 'yarıya indi'}: {o['neden']}"
                       for o in objs) + ("" if kc["aktif"] else " (karnesi düşük: itiraz sadece kaydedildi, uygulanmadı)")))
    return str(dec.get("ozet") or "")[:400], apply, note, usd
