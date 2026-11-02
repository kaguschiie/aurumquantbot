"""Binance spot USDT paritelerinin (delist edilenler dahil: hayatta kalan yanliligi yok) 2017+ gunluk mumlari.
Kaynak data.binance.vision aylik zip + bu ayin eksik gunleri icin REST. Cikti: d1/{SYM}.npz (t,o,h,l,c,qv,tbq,n)"""
import io, os, re, sys, zipfile, time
from concurrent.futures import ThreadPoolExecutor
import numpy as np, requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "d1")
os.makedirs(OUT, exist_ok=True)
U = "https://s3-ap-northeast-1.amazonaws.com/data.binance.vision"
F = "https://data.binance.vision/"
BAD = ("UPUSDT", "DOWNUSDT", "BULLUSDT", "BEARUSDT")
STABLE = {"USDCUSDT", "BUSDUSDT", "TUSDUSDT", "USDPUSDT", "PAXUSDT", "USDSUSDT", "USDSBUSDT", "DAIUSDT", "FDUSDUSDT",
          "EURUSDT", "GBPUSDT", "AUDUSDT", "EURIUSDT", "AEURUSDT", "USTUSDT", "SUSDUSDT", "USD1USDT", "XUSDUSDT",
          "BFUSDUSDT", "RLUSDUSDT", "USDEUSDT", "TRYUSDT", "BRLUSDT", "UAHUSDT", "PAXGUSDT_"}
S = requests.Session()


def lst(prefix, delim=True):
    out, marker = [], ""
    while True:
        p = {"prefix": prefix, "marker": marker}
        if delim:
            p["delimiter"] = "/"
        r = S.get(U, params=p, timeout=30).text
        ks = re.findall(r"<Prefix>([^<]+)</Prefix>" if delim else r"<Key>([^<]+)</Key>", r)
        ks = [k for k in ks if k != prefix]
        out += ks
        if "<IsTruncated>true" not in r or not ks:
            return out
        marker = ks[-1]


def one(sym):
    fn = os.path.join(OUT, sym + ".npz")
    if os.path.exists(fn):
        return sym, "var"
    keys = [k for k in lst(f"data/spot/monthly/klines/{sym}/1d/", False) if k.endswith(".zip")]
    rows = []
    for k in keys:
        for _ in range(3):
            try:
                z = zipfile.ZipFile(io.BytesIO(S.get(F + k, timeout=30).content))
                for line in z.read(z.namelist()[0]).decode().splitlines():
                    x = line.split(",")
                    if x[0].isdigit():
                        rows.append([int(x[0]), *map(float, (x[1], x[2], x[3], x[4], x[7], x[10], x[8]))])
                break
            except Exception:
                time.sleep(1)
    if not rows:
        return sym, "bos"
    a = np.array(sorted({r[0]: r for r in rows}.values()), dtype=float)
    a[:, 0] = np.where(a[:, 0] > 1e14, a[:, 0] // 1000, a[:, 0])  # 2025+ dosyalarda mikro saniye
    last = int(a[-1, 0])
    try:  # son ayin gunleri (henuz aylik zip yok)
        k = S.get("https://api.binance.com/api/v3/klines", params={"symbol": sym, "interval": "1d", "startTime": last + 1,
                                                                   "limit": 1000}, timeout=20).json()
        if isinstance(k, list) and k:
            b = np.array([[x[0], *map(float, (x[1], x[2], x[3], x[4], x[7], x[10], x[8]))] for x in k[:-1]], dtype=float)
            if len(b):
                a = np.vstack([a, b])
    except Exception:
        pass
    np.savez(fn, t=a[:, 0].astype(np.int64), o=a[:, 1], h=a[:, 2], l=a[:, 3], c=a[:, 4], qv=a[:, 5], tbq=a[:, 6], n=a[:, 7])
    return sym, len(a)


if __name__ == "__main__":
    syms = [p.split("/")[-2] for p in lst("data/spot/monthly/klines/")]
    syms = [s for s in syms if s.endswith("USDT") and not s.endswith(BAD) and s not in STABLE]
    print(len(syms), "parite", flush=True)
    t0 = time.time()
    def safe(s):
        err = ""
        for k in range(4):
            try:
                return one(s)
            except Exception as e:
                err = str(e)[:80]
                time.sleep(2 + 3 * k)
        return s, f"HATA {err}"

    with ThreadPoolExecutor(8) as ex:
        for i, (s, r) in enumerate(ex.map(safe, syms)):
            if str(r).startswith("HATA"):
                print(s, r, flush=True)
            if i % 50 == 0:
                print(i, s, r, f"{time.time() - t0:.0f}s", flush=True)
    print("bitti", len(os.listdir(OUT)), f"{time.time() - t0:.0f}s")
