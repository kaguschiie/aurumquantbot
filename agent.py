"""Claude ajani: al-sat kararlari (yapilandirilmis JSON) ve sohbet.
Saniyelik isler (fiyat, stop, hedef, iz suren stop) yerelde kalir; Claude sadece karar anlarinda cagrilir:
saatlik mum kapanisi + olay tetikleri (TradingView guclu sinyal, pozisyonda sert hareket, BTC soku).
Her cagrinin maliyeti olculur, gunluk butce asilirsa yeni karar istenmez (acik pozisyonlar korunmaya devam eder)."""
import glob
import json
import os
import shutil
import subprocess
import time

import anthropic

import config as C

BETAS = ["server-side-fallback-2026-07-01"]  # reddedilen istek sunucuda onerilen modele devredilir
# model -> ($/1M girdi, $/1M cikti, $/1M onbellek okuma); onbellege yazma girdinin 1.25 kati
PRICE = {"claude-opus-5-5": (4.0, 20.0, 0.20), "claude-sonnet-5-5": (2.0, 10.0, 0.20),
         "claude-haiku-5-5": (0.10, 0.50, 0.01)}
MODELS = {"claude-opus-5-5": "Claude Opus 5.5", "claude-sonnet-5-5": "Claude Sonnet 5.5",
          "claude-haiku-5-5": "Claude Haiku 5.5"}
_C = {"key": None, "client": None, "fb": True, "check": (0, None)}


class AgentError(Exception):
    def __init__(self, msg, usd=0.0):
        super().__init__(msg)
        self.usd = usd


def client():
    if not C.CLAUDE_API_KEY:
        return None
    if _C["key"] != C.CLAUDE_API_KEY:
        _C.update(key=C.CLAUDE_API_KEY, fb=True, check=(0, None),
                  client=anthropic.Anthropic(api_key=C.CLAUDE_API_KEY, timeout=240, max_retries=2))
    return _C["client"]


def cost(model, u):
    """Cagrinin dolar maliyeti. Yedek modele devredildiyse reddeden denemenin tokenlari da sayilir."""
    its = getattr(u, "iterations", None)
    if its:
        return sum(_cost1(getattr(it, "model", None) or model, it) for it in its)
    return _cost1(model, u)


def _cost1(model, u):
    i, o, cr = PRICE.get(model, (5.0, 25.0, 0.5))  # bilinmeyen (yedek) model: temkinli fiyat
    g = lambda k: getattr(u, k, 0) or 0
    return (g("input_tokens") * i + g("cache_creation_input_tokens") * i * 1.25
            + g("cache_read_input_tokens") * cr + g("output_tokens") * o) / 1e6


def _call(stream, model=None, **kw):
    """Opus/Sonnet: sunucu tarafi yedek model acik. Desteklenmezse bir kez kapatip tekrar dener."""
    c, model = client(), model or C.CLAUDE_MODEL
    use_fb = _C["fb"] and not model.startswith("claude-haiku")
    api = c.beta.messages if use_fb else c.messages
    extra = {"betas": BETAS, "fallbacks": "default"} if use_fb else {}
    try:
        return (api.stream if stream else api.create)(model=model, **extra, **kw)
    except anthropic.BadRequestError as e:
        if use_fb and "fallback" in str(e).lower():
            _C["fb"] = False
            return _call(stream, model, **kw)
        raise


def friendly(e):
    if isinstance(e, anthropic.AuthenticationError):
        return "Claude API anahtarı geçersiz (Ayarlar > Claude)."
    if isinstance(e, anthropic.PermissionDeniedError):
        return "Bu API anahtarının modele erişim izni yok."
    if isinstance(e, anthropic.NotFoundError):
        return f"Model bulunamadı: {C.CLAUDE_MODEL}"
    if isinstance(e, anthropic.RateLimitError):
        return "Claude hız sınırı doldu, biraz sonra tekrar denenecek."
    if isinstance(e, anthropic.BadRequestError) and "credit" in str(e).lower():
        return "Claude kredi bakiyesi yetersiz (console.anthropic.com > Billing)."
    if isinstance(e, anthropic.APIStatusError):
        return f"Claude API hatası {e.status_code}: {str(e)[:160]}"
    if isinstance(e, anthropic.APIConnectionError):
        return "Claude'a ulaşılamadı (internet?)."
    return str(e)[:200]


def brain():
    """Aktif beyin: 'api' (ucretli anahtar) > 'cli' (Claude aboneligi, yerel claude komutu) > 'local' (yerel quant)."""
    if C.CLAUDE_API_KEY and api_status()[0]:
        return "api"
    return "cli" if cli_ready() else "local"


BRAIN_TR = {"api": "Claude API aktif", "cli": "Claude CLI köprüsü aktif (abonelik)", "local": "Yerel Quant Motoru aktif"}


def status():
    """Arayuz icin (her zaman calisir, aciklama). Hangi beyin aktif, neden."""
    b = brain()
    if b == "api":
        return True, f"{BRAIN_TR[b]} · {MODELS.get(C.CLAUDE_MODEL, C.CLAUDE_MODEL)}"
    if b == "cli":
        return True, BRAIN_TR[b]
    why = CLI["why"] or "Claude bağlı değil"
    return True, f"{BRAIN_TR[b]} ({why})"


# ---------------- Claude CLI koprusu (abonelik, ekstra API ucreti yok) ----------------
CLI = {"path": None, "t": 0, "ok": None, "down_until": 0, "why": ""}
CLI_MODEL = {"claude-opus-5-5": "opus", "claude-sonnet-5-5": "sonnet", "claude-haiku-5-5": "haiku"}
NO_WINDOW = 0x08000000  # Windows: siyah konsol penceresi acilmasin


SKIP = {"cache", "code cache", "gpucache", "indexeddb", "local storage", "session storage", "blob_storage", "logs",
        "crashpad", "dawncache", "node_modules", "projects", "shell-snapshots", "todos"}


def _scan(root, name, depth=6):
    """Derinligi sinirli, onbellek klasorlerini atlayan dosya arama (buyuk uygulama klasorlerinde de hizli)."""
    out = []
    if not os.path.isdir(root):
        return out
    base = root.rstrip("\\/").count(os.sep)
    for d, dirs, files in os.walk(root):
        if d.count(os.sep) - base >= depth:
            dirs[:] = []
        dirs[:] = [x for x in dirs if x.lower() not in SKIP]
        if name in files:
            out.append(os.path.join(d, name))
    return out


def cli_candidates():
    """Bu bilgisayardaki tum olasi claude komutlari (kullanici adindan bagimsiz, ortam degiskenleriyle):
    elle secilen yol, PATH, resmi kurulum (~/.local/bin), npm, eski yerel kurulum, WinGet, Program Files ve Claude
    masaustu uygulamasinin icindeki kopyalar (en yenisi once)."""
    env = lambda k: os.environ.get(k, "")
    home, local, roam = os.path.expanduser("~"), env("LOCALAPPDATA"), env("APPDATA")
    out = [C.CLAUDE_CLI_PATH] if C.CLAUDE_CLI_PATH else []
    out += [shutil.which(n) for n in ("claude", "claude.exe", "claude.cmd")]
    out += [os.path.join(home, ".local", "bin", "claude.exe"), os.path.join(home, ".local", "bin", "claude"),
            os.path.join(roam, "npm", "claude.cmd"), os.path.join(home, ".claude", "local", "claude.cmd"),
            os.path.join(home, ".claude", "local", "claude.exe"), os.path.join(local, "Microsoft", "WinGet", "Links", "claude.exe"),
            os.path.join(local, "Programs", "claude", "claude.exe"), os.path.join(env("ProgramFiles"), "nodejs", "claude.cmd")]
    found = []
    for root in [os.path.join(local, d) for d in ("Claude-Data", "AnthropicClaude", "Claude", "Programs")] + \
                [os.path.join(roam, "Claude"), env("ProgramFiles") and os.path.join(env("ProgramFiles"), "Claude"),
                 env("ProgramFiles") and os.path.join(env("ProgramFiles"), "Anthropic")]:
        found += _scan(root, "claude.exe") if root else []
    out += sorted(found, key=os.path.getmtime, reverse=True)
    seen, res = set(), []
    for c in out:
        if c and os.path.isfile(c) and os.path.normcase(c) not in seen:
            seen.add(os.path.normcase(c))
            res.append(c)
    return res


def find_cli():
    """Calisan ilk claude komutu (--version cevap veriyor mu?)."""
    for c in cli_candidates():
        try:
            r = subprocess.run([c, "--version"], capture_output=True, text=True, timeout=20, env=_cli_env(),
                               creationflags=NO_WINDOW if os.name == "nt" else 0)
            if r.returncode == 0:
                return c
        except (OSError, subprocess.SubprocessError):
            continue
    return None


def cli_diagnose():
    """Baglanti sihirbazi icin: adim adim durum ve ne yapilacagi."""
    cands = cli_candidates()
    CLI.update(ok=None, t=0, down_until=0)
    ready = cli_ready()
    path = CLI["path"]
    ver = ""
    if path:
        try:
            ver = _cli(["--version"], timeout=20).stdout.strip()[:60]
        except (OSError, subprocess.SubprocessError):
            pass
    if not cands:
        step, why = "kur", "Claude CLI kurulu değil (bu bilgisayarda claude komutu bulunamadı)."
    elif not path:
        step, why = "onar", f"{len(cands)} aday yol bulundu ama hiçbiri çalışmadı (kurulum bozuk olabilir)."
    elif not ready:
        step, why = "giris", "Claude CLI kurulu ama oturum açılmamış (masaüstü uygulamasının oturumu CLI'ye geçmez)."
    else:
        step, why = "hazir", "Claude CLI köprüsü hazır."
    return {"step": step, "why": why, "path": path, "version": ver, "candidates": cands[:8], "manual": C.CLAUDE_CLI_PATH,
            "ready": ready}


def cli_install():
    """Resmi Claude Code kurulumu (Anthropic'in Windows kurulum betigi) gorunur pencerede; bitince 'Kontrol et'."""
    subprocess.Popen(["cmd", "/c", "start", "Claude Code kurulumu", "powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                      "-NoExit", "-Command", "irm https://claude.ai/install.ps1 | iex; "
                      "Write-Host ''; Write-Host 'Kurulum bitti. Bu pencereyi kapatip uygulamada Kontrol et e basin.'"],
                     env=_cli_env())
    CLI.update(ok=None, t=0, down_until=0, path=None)


def _cli_env():
    # baska bir Claude oturumundan miras kalan gecici ortam degiskenleri kendi girisini bozmasin
    keep = ("CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CONFIG_DIR")
    return {k: v for k, v in os.environ.items() if not k.startswith(("CLAUDE", "ANTHROPIC")) or k in keep}


def _cli(args, inp=None, timeout=60, stream=False):
    work = os.path.join(C.APP_DIR, "cli")  # bos calisma klasoru: proje dosyalari/ayarlari karisMAsin
    os.makedirs(work, exist_ok=True)
    kw = dict(cwd=work, env=_cli_env(), creationflags=NO_WINDOW if os.name == "nt" else 0)
    if stream:
        return subprocess.Popen([CLI["path"], *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace", **kw)
    return subprocess.run([CLI["path"], *args], input=inp, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout, **kw)


def cli_ready():
    """CLI bulundu mu ve abonelikle giris yapilmis mi? 10 dk'da bir ucretsiz 'auth status' ile kontrol."""
    if time.time() < CLI["down_until"]:
        return False
    if CLI["ok"] is None or time.time() - CLI["t"] > 600:
        CLI.update(t=time.time(), path=find_cli())
        if not CLI["path"]:
            CLI.update(ok=False, why="Claude CLI bulunamadı")
        else:
            try:
                r = _cli(["auth", "status", "--json"], timeout=30)
                ok = bool(json.loads(r.stdout or "{}").get("loggedIn"))
                CLI.update(ok=ok, why="" if ok else "Claude CLI'ye giriş yapılmamış")
            except (OSError, ValueError, subprocess.SubprocessError) as e:
                CLI.update(ok=False, why=f"Claude CLI çalışmadı: {str(e)[:80]}")
    return CLI["ok"]


def cli_login():
    """Kullanicinin kendisi bir kez tarayicidan abonelik girisi yapsin diye gorunur pencere acar."""
    path = CLI["path"] or find_cli()
    if not path:
        return "Claude CLI bulunamadı. Claude masaüstü uygulaması veya Claude Code kurulu olmalı."
    subprocess.Popen(["cmd", "/c", "start", "Claude girisi", "cmd", "/k", path, "auth", "login"], env=_cli_env())
    CLI.update(ok=None, down_until=0)  # giristen sonra yeniden kontrol edilsin
    return None


def _cli_fail(msg):
    """Kota/oturum sorunu: 15 dk yerel motora gec, sonra tekrar dene."""
    CLI.update(down_until=time.time() + 900, why=msg[:90], ok=None)
    raise AgentError(msg)


def cli_decide(brief, model=None):
    """Ayni karar semasi, Claude CLI uzerinden (abonelik). Donus: (karar, 0.0, model). model: kota icin farkli model."""
    cm = CLI_MODEL.get(model or C.CLAUDE_MODEL, "sonnet")
    args = ["-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA), "--tools", "",
            "--model", cm, "--setting-sources", "", "--no-session-persistence",
            "--strict-mcp-config", "--system-prompt", DECIDE_SYSTEM + "\nSadece semaya uygun JSON dondur."]
    try:
        r = _cli(args, inp=brief, timeout=300)
        out = json.loads(r.stdout or "{}")
    except subprocess.TimeoutExpired:
        _cli_fail("Claude CLI zaman aşımı")
    except (OSError, ValueError) as e:
        _cli_fail(f"Claude CLI cevabı okunamadı: {str(e)[:80]}")
    if out.get("is_error"):
        _cli_fail(f"Claude CLI: {str(out.get('result'))[:120]}")
    dec = out.get("structured_output")
    if not isinstance(dec, dict):
        txt = str(out.get("result") or "")
        try:
            dec = json.loads(txt[txt.index("{"):txt.rindex("}") + 1])
        except ValueError:
            raise AgentError("Claude CLI cevabı JSON değil.")
    return dec, 0.0, f"Claude CLI ({cm})"


def cli_chat_stream(question, context, history):
    """Sohbet, CLI uzerinden akisla. Hata olursa AgentError (cagiran yerel analiste gecer)."""
    past = "".join(f"\n[{'KULLANICI' if m['role'] == 'user' else 'SEN'}]: {m['content'][:1500]}" for m in history[-6:])
    prompt = (f"ONCEKI KONUSMA:{past}\n\n" if past else "") + f"CANLI BOT VERISI:\n{context}\n\nSORU: {question}"
    p = _cli(["-p", "--output-format", "stream-json", "--verbose", "--include-partial-messages", "--tools", "",
              "--model", CLI_MODEL.get(C.CLAUDE_MODEL, "sonnet"), "--setting-sources", "", "--no-session-persistence",
              "--strict-mcp-config", "--system-prompt", CHAT_SYSTEM], stream=True)
    p.stdin.write(prompt)
    p.stdin.close()
    sent, final = False, None
    try:
        for line in p.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            e = ev.get("event") or {}
            if ev.get("type") == "stream_event" and (e.get("delta") or {}).get("type") == "text_delta":
                sent = True
                yield e["delta"]["text"]
            elif ev.get("type") == "result":
                final = ev
    finally:
        if p.poll() is None:
            p.kill()
    if final and final.get("is_error"):
        if sent:
            yield "\n[Claude CLI cevabı yarıda kesildi]"
            return
        _cli_fail(f"Claude CLI: {str(final.get('result'))[:120]}")
    if not sent:
        if final and final.get("result"):
            yield final["result"]
        else:
            _cli_fail("Claude CLI cevap vermedi")


def api_status():
    """Ucretli API anahtari gecerli mi? 10 dk'da bir ucretsiz Models API ile dogrulanir."""
    if not C.CLAUDE_API_KEY:
        return False, "Claude API anahtarı girilmedi."
    t, res = _C["check"]
    if time.time() - t > 600 or res is None:
        try:
            client().models.retrieve(C.CLAUDE_MODEL)
            res = (True, MODELS.get(C.CLAUDE_MODEL, C.CLAUDE_MODEL))
        except anthropic.APIConnectionError:
            res = (True, MODELS.get(C.CLAUDE_MODEL, C.CLAUDE_MODEL) + " (doğrulanamadı: internet?)")
        except anthropic.APIError as e:
            res = (False, friendly(e))
        _C["check"] = (time.time(), res)
    return res


# ---------------- al-sat karari ----------------
DECIDE_SYSTEM = """Sen KriptoQuantBot'un portfoy yoneticisi ve trader'isin. Binance SPOT piyasasinda (sadece USDT
pariteleri, sadece alis yonu; aciga satis ve kaldirac yok) otomatik al-sat kararlarini sen veriyorsun.

Sana her cagrida kompakt bir piyasa ozeti gelir: hesap ve kartopu kasa durumu, acik pozisyonlar, piyasa rejimi,
kuresel makro (endeksler, altin, dolar, VIX), aday coin tablosu (canli fiyat, degisimler, ATR, RSI, 4 zaman diliminde
teknik ozet [hareketli ortalama + osilator oylari, -1 guclu sat .. +1 guclu al], alici akisi, fonlama, acik pozisyon
(OI), emir defteri alici payi ve spread, yerel ML modelinin olasiligi), tum piyasa radari, Ders Defteri, son
islemlerin sonuclari, guven kalibrasyonun ve senin onceki strateji notun.

Nasil calisirsin:
- AJAN KOMITESI: karari su sirayla kur: (1) ANALISTLER: teknik (4 zaman dilimi, RSI, ATR, hacim), zincir (balina akisi,
  likidasyon kumeleri), makro/haber; (2) BOGA ve AYI TEZLERI: her tezi ozetteki KANITLA tart (GECMIS OKULU'nun 2 yillik
  R degerleri, Ders Defteri, HAFIZA satirlari); kanitsiz tez agirlik almaz; (3) TRADER: oneri; (4) RISK: yerel risk
  yoneticisi kalkan/odul-risk, maliyet, likidite, rejim izni, tuzak, hafiza ve ML kontrollerinden gecmeyeni VETO eder.
  bull_case ve bear_case alanlarina en guclu kanitli tezleri yaz.
- BEKLENTI: kazanma orani degil islem basi beklenti (ortalama R) onemlidir. Ortalama kayip ortalama kazanctan buyukse
  yuksek kazanma oranina ragmen para kaybedilir. Komisyon+kayma gidis-donus ~%0,3: stop mesafesi bunun yeterince
  ustunde olmali (dar stop = maliyet riskin buyuk kismini yer; risk yoneticisi reddeder).
- HAFIZA: 'HAFIZA' satirlari trading_memory.md'den gelir (benzer gecmis durumlar ve sonuclari). Ayni hatayi tekrarlama.
- Aktif bir trader'sin: pozitif beklenen degerli kurulum varsa islem acarsin. Kurulum yoksa beklemek de bir karardir.
- Her alis icin boga (bull_case) ve ayi (bear_case) senaryosunu kisaca yaz ve tart; trende karsi, dikey pompanin
  tepesinde, dusuk likiditede veya genis spreadde alma.
- conviction (0-1) gercekten isabet olasiligini yansitsin: guven kalibrasyon satirina bakip kendini duzelt.
- Kararin fiyat ozetteki fiyattan cok uzaklasirsa (karar suresince kacarsa) risk yoneticisi emri bayat sayip iptal eder.
- Stop'u yapiya gore koy (son dip / destek alti, genelde 1-2 ATR). Kar hedefi: tp1 (pozisyonun yarisi satilir,
  stop basabasa cekilir, kalan kisim iz suren stopla takip edilir) ve tp2 (son hedef). tp2 icin odul/risk en az 1.5.
- risk_pct: stop olursa kasanin kaybedilecek yuzdesi. Guvenine gore kucuk tut; ust siniri yerel risk yoneticisi uygular.
- max_hours: islem bu surede hedefe gitmezse kapatilir (1-72).
- entry: "maker" (alis fiyatina pasif limit, ucuz, dolmayabilir) veya "taker" (hemen, momentumda).
- Acik pozisyonlari her cagrida degerlendir: tez bozulduysa "close", korumayi siklastirmak icin "adjust" (stop sadece
  yukari tasinabilir), aksi halde dokunma. "hold" kullanmana gerek yok.
- ML olasiligi zayif bir sinyaldir (gecmis testte tek basina karli degildi); tek basina karar gerekcesi yapma.
- Yerel risk yoneticisi kurallari asan, eksik veya mantiksiz emirleri reddeder; reddedilenler bir sonraki ozette gorunur.
- Anapara korunur ama surekli nakitte beklemek de bir hatadir: sanal kasa bir ogrenme laboratuvaridir ve Ders Defteri
  ancak islemlerle ogrenir. Sifir zarar kalkani (+0,4R'de basabas + kar kilitleyen iz suren stop) yerelde otomatik
  calisir; bu yuzden BTC dususteyken bile radarda hacmi 3x+ patlamis, BTC'den pozitif ayrisan, RSI 50-74 araliginda
  ve alici akisi pozitif momentum/kirilim kurulumlarinda kucuk riskle (0,3-0,6) aktif islem ac. Balina satis baskisi
  ve likidasyon kumeleri ozette verilir; stopu kumenin hemen ustune koyma.
- RADAR: tum Binance USDT pariteleri taranir. BTC dusse bile BTC'den pozitif ayrisan (24s BTC'ye gore +5 puan ustu),
  hacmi patlayan (3x+) ve 1s/4s trendi guclu coinler degerlendirilebilir; piyasaya karsi islemde riski kucuk tut,
  dikey pompanin tepesini (RSI 75+, 24s %30+) kovalama.
- ALTIN: PAXG/USDT (1 PAXG = 1 ons altin) islem gorur. Kripto zayif ve makro risk-off iken altin teknik olarak
  gucluyse parayi bekletmek yerine altina yonlendirebilirsin. Hisse ve endeksler (S&P, Nasdaq, BIST) Binance'te islem
  goremez; sadece risk istahi baglamidir (Nasdaq sert dusus / VIX yuksek = kripto icin temkin).
- DERS DEFTERI: kendi gecmis islemlerinin ve adaylarin otopsisinden ogrenilen sinyal agirliklari, tuzak kombinasyonlar
  ve altin kurallar ozette gelir. Tuzak kombinasyonlarina girme (risk yoneticisi zaten reddeder), agirligi pozitif
  sinyalleri ve altin kurallari tercih et, ayni hatayi tekrarlama. 'ders R' sutunu o coinin su anki sinyallerinin
  ogrenilmis avantajidir (R = risk birimi).

Cikti: sadece istenen JSON. Metinleri dogru Turkce karakterlerle (ç ğ ı ö ş ü) yaz. Kullanilmayan sayisal alanlara 0 yaz.
- market_view: piyasa yorumun (en fazla 2 cumle).
- actions: buy / close / adjust listesi (bos olabilir). symbol "SOL/USDT" bicimindedir.
- reason: kullanicinin okuyacagi net gerekce (en fazla 3 cumle; somut rakamlarla).
- bull_case / bear_case: alista birer kisa cumle; close/adjust icin bos birakabilirsin ("").
- memo: kendine strateji notu (en fazla 600 karakter). Son islemlerden ne ogrendigini ve bundan sonra neye dikkat
  edecegini yaz; bir sonraki cagrida sana geri verilir. Boylece kendi hatalarindan ders cikarirsin."""

_NUM = {"type": "number"}
SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["market_view", "actions", "memo"],
    "properties": {
        "market_view": {"type": "string"},
        "actions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["type", "symbol", "conviction", "risk_pct", "stop", "tp1", "tp2", "max_hours", "entry",
                         "bull_case", "bear_case", "reason"],
            "properties": {"type": {"type": "string", "enum": ["buy", "close", "adjust"]},
                           "symbol": {"type": "string"}, "conviction": _NUM, "risk_pct": _NUM, "stop": _NUM,
                           "tp1": _NUM, "tp2": _NUM, "max_hours": _NUM,
                           "entry": {"type": "string", "enum": ["maker", "taker"]},
                           "bull_case": {"type": "string"}, "bear_case": {"type": "string"},
                           "reason": {"type": "string"}}}},
        "memo": {"type": "string"}}}


def decide(brief, model=None):
    """Karar al. Donus: (karar, maliyet $, cevaplayan model). Hata: AgentError."""
    if not client():
        raise AgentError("Claude API anahtarı girilmedi.")
    try:
        r = _call(False, model=model, max_tokens=16000,
                  system=[{"type": "text", "text": DECIDE_SYSTEM, "cache_control": {"type": "ephemeral"}}],
                  output_config={"effort": C.CLAUDE_EFFORT, "format": {"type": "json_schema", "schema": SCHEMA}},
                  messages=[{"role": "user", "content": brief}])
    except anthropic.APIError as e:
        raise AgentError(friendly(e)) from e
    usd = cost(r.model, r.usage)
    if r.stop_reason == "refusal":
        raise AgentError("Claude bu karar isteğini reddetti.", usd)
    if r.stop_reason == "max_tokens":
        raise AgentError("Claude'un cevabı yarıda kesildi.", usd)
    try:
        dec = json.loads(next(b.text for b in r.content if b.type == "text"))
    except (StopIteration, ValueError) as e:
        raise AgentError("Claude'un cevabı okunamadı.", usd) from e
    return dec, usd, r.model


# ---------------- sohbet ----------------
CHAT_SYSTEM = """Sen Claude'sun ve KriptoQuantBot'u yonetiyorsun: Binance spot al-sat kararlarini sen veriyorsun
(her saat basi ve onemli piyasa olaylarinda). Saniyelik fiyat takibi, stop, kademeli kar ve iz suren stop yerel
motorda calisir; yerel risk yoneticisi senin emirlerini kasa sinirlarina gore denetler.

Kullanici Turkce yazar; net, samimi Turkce cevap ver (dogru Turkce karakterlerle: ç ğ ı ö ş ü). Gecikmeye duyarli bir sohbettir: cevaba hemen basla.
Her mesajda botun canli verisi verilir (hesap ve kartopu kasa, pozisyonlar, son kararlarin ve gerekceleri, piyasa
tablosu, tum piyasa radari, kuresel makro/borsa, Ders Defteri'nin ogrendikleri, karar gunlugu). Sadece bu veriye dayan, veride olmayani uydurma; rakamlari birimleriyle ver.
Sohbetten emir veremezsin: kullanici bir islemi kapatmak isterse ekrandaki "Kapat" dugmesini, tum yeni islemleri
durdurmak isterse "Duraklat" dugmesini kullanmasini soyle.
Kar garantisi verme. Bu kisisel bir yazilimdir, yatirim tavsiyesi degildir; kullanici sorarsa kararin ona ait oldugunu belirt."""


def chat_stream(question, context, history, on_cost=lambda usd: None):
    """Cevabi parca parca uretir. Hata olursa okunur bir mesaj uretir."""
    if not client():
        yield "Claude bağlı değil. Admin, Ayarlar → Claude bölümüne API anahtarını girip Kaydet'e basmalı."
        return
    msgs = [{"role": m["role"], "content": m["content"]} for m in history[-8:]]
    msgs.append({"role": "user", "content": f"CANLI BOT VERISI:\n{context}\n\nSORU: {question}"})
    kw = dict(max_tokens=8000, output_config={"effort": "low"}, messages=msgs,
              system=[{"type": "text", "text": CHAT_SYSTEM, "cache_control": {"type": "ephemeral"}}])
    s = None
    try:
        try:  # akista istek __enter__ ile gider: yedek model destegi yoksa burada yakalanir
            s = _call(True, **kw).__enter__()
        except anthropic.BadRequestError as e:
            if not (_C["fb"] and "fallback" in str(e).lower()):
                raise
            _C["fb"] = False
            s = _call(True, **kw).__enter__()
        for part in s.text_stream:
            yield part
        if s.get_final_message().stop_reason == "refusal":
            yield "\n[Claude bu soruya cevap vermedi.]"
    except anthropic.APIError as e:
        yield f"\n[{friendly(e)}]"
    finally:  # yarida kesilse bile o ana kadarki maliyet butceye yazilir
        if s is not None:
            try:
                snap = s.current_message_snapshot
                on_cost(cost(snap.model, snap.usage))
            except Exception:
                pass
            s.__exit__(None, None, None)
