"""Elle alim satis secimleri: sure, suresiz, sadece stop+hedef, yapay zekaya birak (gercek emir ve ucretli Claude yok)."""
import os
import sys
import time

sys.path.insert(0, sys.argv[1])
import config as C
for f in os.listdir(C.APP_DIR):
    if f.startswith(("state", "durdur", "ders_defteri", "hibrit")):
        os.remove(os.path.join(C.APP_DIR, f))
import agent as A
import bot as B

ok_all = True


def check(name, cond, info=""):
    global ok_all
    ok_all &= bool(cond)
    print(("OK  " if cond else "HATA"), name, info)


A.brain = lambda: "local"
b = B.Bot()
b.symbols = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"]
b.refresh_prices()
pa = b.accounts["PAPER"]
pa.mark()
ETH, SOL, BNB, XRP = "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT"

# gecersiz secimler
px = b.ask[ETH]
check("bilinmeyen secim reddedilir", "Geçersiz" in (b.manual_buy("PAPER", ETH, 100, px * .9, None, "taker", "t", "uydurma") or ""))
check("sadece stop+hedef: hedefsiz reddedilir", "ikisini" in (b.manual_buy("PAPER", ETH, 100, px * .9, None, "taker", "t", "stop") or ""))
check("sadece stop+hedef: stopsuz reddedilir", "ikisini" in (b.manual_buy("PAPER", ETH, 100, 0, px * 1.1, "taker", "t", "stop") or ""))
check("sure: saatsiz reddedilir", "Süre" in (b.manual_buy("PAPER", ETH, 100, px * .9, None, "taker", "t", "sure") or ""))
check("sure: sinir disi reddedilir", "Süre" in (b.manual_buy("PAPER", ETH, 100, px * .9, None, "taker", "t", "sure", 9999) or ""))
check("reddedilenler pozisyon acmadi", ETH not in pa.st["positions"])

# varsayilan cagri eski davranisla ayni
check("eski cagri: stop+hedef ile Manuel Kilit", b.manual_buy("PAPER", XRP, 100, b.ask[XRP] * .9, b.ask[XRP] * 1.1) is None and pa.st["positions"][XRP]["manual"]
      and not pa.st["positions"][XRP]["ai"] and pa.st["positions"][XRP]["sure_t"] is None)

# suresiz: stop bos -> felaket stopu, sure yok
err = b.manual_buy("PAPER", ETH, 100, 0, None, "taker", "t", "suresiz")
pos = pa.st["positions"].get(ETH)
check("suresiz: stop bos, felaket stopu", not err and pos and abs(pos["sl"] / pos["entry"] - 0.65) < .01 and pos["manual"] and pos["sure_t"] is None, err or pos["sl"] / pos["entry"])
pos["t"] -= 400 * 3600
pa.manage_positions()
check("suresiz: sure asimiyla kapanmaz", ETH in pa.st["positions"])
b.bid[ETH] = b.ask[ETH] = pos["entry"] * 1.5
pa.manage_positions()
check("suresiz: hedef yok, yukselince kapanmaz, kalkan stopu tasimaz", ETH in pa.st["positions"] and abs(pos["sl"] / pos["entry"] - .65) < .01)
pa.close(ETH, "ELLE_KAPATILDI")

# sure
b.refresh_prices()
err = b.manual_buy("PAPER", ETH, 100, b.ask[ETH] * .9, None, "taker", "t", "sure", 2)
pos = pa.st["positions"].get(ETH)
check("sure: Manuel Kilit + bitis zamani", not err and pos["manual"] and abs(pos["sure_t"] - time.time() - 7200) < 5, err)
pa.manage_positions()
check("sure: dolmadan kapanmaz", ETH in pa.st["positions"])
check("sure: uzatma", pa.set_timer(ETH, 5) is None and abs(pos["sure_t"] - time.time() - 18000) < 5)
check("sure: sinir disi uzatma reddedilir", "Süre" in (pa.set_timer(ETH, 0.01) or "") and abs(pos["sure_t"] - time.time() - 18000) < 5)
check("sure: iptal", pa.set_timer(ETH, None) is None and pos["sure_t"] is None)
pos["sure_t"] = time.time() - 1
pa.manage_positions()
t = pa.st["trades"][-1]
check("sure doldu: satildi", ETH not in pa.st["positions"] and t["reason"] == "SURE_DOLDU", t["reason"])

# sure doldu ama stop once: stop nedeni yazilir
b.refresh_prices()
b.manual_buy("PAPER", ETH, 100, b.ask[ETH] * .9, None, "taker", "t", "sure", 2)
pos = pa.st["positions"][ETH]
pos["sure_t"] = time.time() - 1
b.bid[ETH] = pos["sl"] * .99
pa.manage_positions()
check("ayni anda stop ve sure: stop oncelikli", pa.st["trades"][-1]["reason"] != "SURE_DOLDU", pa.st["trades"][-1]["reason"])

# pasif emir: sure dolumdan sayilir
b.refresh_prices()
err = b.manual_buy("PAPER", SOL, 100, b.bid[SOL] * .9, None, "maker", "t", "sure", 3)
check("pasif alis: emir bekliyor", not err and SOL in pa.st["pending"], err)
b.ask[SOL] = pa.st["pending"][SOL]["price"]
time.sleep(1)
pa.manage_pending()
pos = pa.st["positions"].get(SOL)
check("pasif alis: dolumda sure basladi", pos and pos["manual"] and abs(pos["sure_t"] - time.time() - 10800) < 5)
pa.close(SOL, "ELLE_KAPATILDI")

# uygulama kapaliyken sure doldu (sanal hesap)
b.refresh_prices()
b.manual_buy("PAPER", SOL, 100, b.bid[SOL] * .5, None, "taker", "t", "sure", 1)
pos = pa.st["positions"][SOL]
now = time.time()
pos["sure_t"], pos["seen"] = now - 600, now - 1200
e = pos["entry"]
real = pa.ex.fetch_ohlcv
pa.ex.fetch_ohlcv = lambda s, tf, since=None, limit=None: [[int((now - 900) * 1000), e, e, e, e, 1], [int((now - 300) * 1000), e * 1.02, e * 1.03, e * 1.01, e * 1.02, 1]]
closed = pa.replay_gap(SOL, pos)
pa.ex.fetch_ohlcv = real
t = pa.st["trades"][-1]
check("kapaliyken sure doldu: o andaki fiyattan satildi", closed and t["reason"] == "SURE_DOLDU" and abs(t["exit"] / (e * 1.02) - 1) < .01, f"{t['reason']} {t.get('exit')}")

# yapay zekaya birak: stop bos -> %8 alti, kalkan calisir
b.refresh_prices()
err = b.manual_buy("PAPER", ETH, 100, 0, None, "taker", "t", "ai")
pos = pa.st["positions"].get(ETH)
check("ai: kalkan acik, stop %8 alti", not err and pos["ai"] and not pos["manual"] and abs(pos["sl"] / pos["entry"] - .92) < .01, err)
err = b.manual_buy("PAPER", BNB, 100, 0, None, "taker", "t", "ai")
pos2 = pa.st["positions"].get(BNB)
calls = []
reply = {}


def fake_ask(system, prompt, schema, model=None):
    calls.append(prompt)
    if isinstance(reply["v"], Exception):
        raise reply["v"]
    return reply["v"], 0.01, "sahte"


A.ask_json = fake_ask
b.ai_positions()
check("ai: Claude bagli degil, cagri yok, uyari", not calls and any("Claude" in n["text"] and "stop ve kalkan" in n["text"] for n in b.notes), len(calls))
A.brain = lambda: "api"
real_budget = b.budget_full
b.budget_full = lambda: True
b.ai_positions()
check("ai: butce dolu, cagri yok", not calls)
b.budget_full = real_budget

reply["v"] = {"kararlar": [{"sym": ETH, "karar": "tut", "stop": 0, "hedef": 0, "neden": "trend saglam"}]}
spent = b.ag["spent"]
b.ai_positions()
check("ai: tek hesap tek cagri, iki coin birlikte", len(calls) == 1 and ETH in calls[0] and BNB in calls[0] and XRP not in calls[0], calls[0][:200] if calls else "")
check("ai: tut -> kaldi, not yazildi, maliyet islendi", ETH in pa.st["positions"] and pos["ai_not"] == "trend saglam" and b.ag["spent"] > spent)

sl0 = pos["sl"]
newstop = b.bid[ETH] * .97
reply["v"] = {"kararlar": [{"sym": ETH, "karar": "guncelle", "stop": newstop, "hedef": b.bid[ETH] * 1.2, "neden": "x"}]}
b.ai_positions()
check("ai: stop yukari + hedef", pos["sl"] == newstop and pos["tp"] > b.bid[ETH] * 1.1)
reply["v"] = {"kararlar": [{"sym": ETH, "karar": "guncelle", "stop": sl0, "hedef": 0, "neden": "gevset"}]}
b.ai_positions()
check("ai: stop asagi inemez", pos["sl"] == newstop and "uygulanmadı" in pos["ai_not"], pos["ai_not"])

for bad in ("yazi", {"kararlar": "x"}, {"kararlar": [1, {"sym": 5}, {"sym": "DOGE/USDT", "karar": "kapat"}, {"sym": XRP, "karar": "kapat", "neden": "x"}, {"sym": ETH, "karar": "uydur"}]},
            {"kararlar": [{"sym": ETH, "karar": "guncelle", "stop": "yazi", "hedef": None, "neden": "x"}]}):
    reply["v"] = bad
    b.ai_positions()
check("ai: bozuk cevap hicbir sey kapatmaz (elle pozisyon XRP dahil)", ETH in pa.st["positions"] and BNB in pa.st["positions"] and XRP in pa.st["positions"] and pos["sl"] == newstop)

reply["v"] = A.AgentError("baglanti")
b.ai_positions()
check("ai: Claude hatasi pozisyona dokunmaz", ETH in pa.st["positions"])

# kullanici geri aldi: Claude artik bakmaz
pa.set_levels(BNB, sl=b.bid[BNB] * .9)
check("elle stop: ai biter", pos2["manual"] and not pos2["ai"])
n = len(calls)
reply["v"] = {"kararlar": [{"sym": BNB, "karar": "kapat", "stop": 0, "hedef": 0, "neden": "x"}, {"sym": ETH, "karar": "tut", "stop": 0, "hedef": 0, "neden": "y"}]}
b.ai_positions()
check("Manuel Kilite alinan coine Claude dokunamaz", BNB in pa.st["positions"] and BNB not in calls[n], calls[n][:120])

# devir sirasi: Claude cevabi beklenirken kullanici kilitledi
def lock_then(system, prompt, schema, model=None):
    pa.set_levels(ETH, sl=b.bid[ETH] * .9)
    return {"kararlar": [{"sym": ETH, "karar": "kapat", "stop": 0, "hedef": 0, "neden": "x"}]}, 0, "sahte"


A.ask_json = lock_then
b.ai_positions()
check("cevap beklerken kilitlenen pozisyon kapanmaz", ETH in pa.st["positions"])
A.ask_json = fake_ask

check("AI'ya devret: ai geri gelir, sure korunur", pa.set_timer(ETH, 4) is None and pa.set_auto(ETH) is None and pos["ai"] and pos["sure_t"])
reply["v"] = {"kararlar": [{"sym": ETH, "karar": "kapat", "stop": 0, "hedef": 0, "neden": "yorgun"}]}
b.ai_positions()
t = pa.st["trades"][-1]
check("ai: kapat", ETH not in pa.st["positions"] and t["reason"] == "CLAUDE_KAPATTI", t["reason"])

# portfoy pozisyonu
b.refresh_prices()
pa.pf_buy(ETH, 100, "test")
pf = pa.st["positions"][ETH]
check("portfoy: sure konamaz", "Portföy" in (pa.set_timer(ETH, 2) or "") and not pf.get("sure_t"))
pa.set_auto(ETH)
check("portfoy: AI'ya devret ai yapmaz", not pf.get("ai"))
n = len(calls)
b.ai_positions()
check("portfoy: Claude'a sorulmaz", all(ETH not in c for c in calls[n:]))

# saatlik is ai_positions'i cagirir, hata saatligi durdurmaz, OTO_SAATLIK kapaliyken de
b.ensure_brain, b.scan = (lambda: None), (lambda *a, **k: None)
hit = []
b.ai_positions = lambda: (hit.append(1), 1 / 0)
C.OTO_SAATLIK = False
b.hourly()
check("saatlik: cagrildi, hata yutuldu", hit == [1])


# gercek hesap: sure doldu -> piyasa satisi tek emir kapisindan; sonuc bilinmezse tekrar denenir
class FakeEx:
    def market(self, s):
        return {"limits": {"cost": {"min": 5}}}


lv = B.Account(b, "LIVE", {})
sent = []
lv.ex, lv.synced, lv.usdt_total, lv.own = FakeEx(), True, 1000, True
lv.order = lambda s, typ, side, qty, price=None, params=None, act=None: sent.append((typ, side, qty, act))
lv._cancel_stop = lambda *a, **k: None
lv._sellable = lambda s, q: q
b.refresh_prices()
lv.st["positions"][SOL] = {"qty": 1.0, "qty0": 1.0, "entry": b.bid[SOL], "atr": 1, "sl": b.bid[SOL] * .5, "sl0": b.bid[SOL] * .5, "tp": b.bid[SOL] * 2,
                           "part": False, "hi": 0, "fee": 0, "realized": 0, "t": time.time(), "stop_id": None, "stop_try": time.time(), "partial_at": None,
                           "manual": True, "sure_t": time.time() - 1}
lv.manage_positions()
check("gercek: sure doldu, emir kapisindan piyasa satisi", sent and sent[0][:3] == ("market", "sell", 1.0) and sent[0][3]["reason"] == "SURE_DOLDU", sent)
check("gercek: satis tamamlanana kadar tekrar denenecek", lv.st["positions"][SOL].get("exit_want") == "SURE_DOLDU")

print("\nSONUC:", "TUM TESTLER GECTI" if ok_all else "HATA VAR")
