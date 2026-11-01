"""Merkezi sunucu modu + uzaktan erisim: yerel ag adresleri, Tailscale (ozel ag) durumu ve tek tikla Cloudflare
gecici tuneli (trycloudflare.com). Tunel/ag erisiminde de giris (kullanici + sifre) zorunludur."""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time

import requests

import config as C

NO_WINDOW = 0x08000000
TOOLS = os.path.join(C.APP_DIR, "araclar")
CF_URL = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
TUN = {"proc": None, "url": None, "error": "", "t": 0.0}


def lan_ips():
    ips = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:  # varsayilan ag kartinin adresi (paket gonderilmez)
            s.connect(("8.8.8.8", 80))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    try:
        ips |= {i for i in socket.gethostbyname_ex(socket.gethostname())[2] if not i.startswith("127.")}
    except OSError:
        pass
    return sorted(ips)


def _tailscale():
    return shutil.which("tailscale") or next((p for p in (os.path.join(os.environ.get("ProgramFiles", ""), "Tailscale",
                                                                       "tailscale.exe"),) if os.path.isfile(p)), None)


def tailscale():
    exe = _tailscale()
    if not exe:
        return {"installed": False}
    try:
        st = json.loads(subprocess.run([exe, "status", "--json"], capture_output=True, text=True, timeout=10,
                                       creationflags=NO_WINDOW).stdout or "{}")
        me = st.get("Self") or {}
        return {"installed": True, "state": st.get("BackendState"), "ips": me.get("TailscaleIPs") or [],
                "dns": (me.get("DNSName") or "").rstrip(".")}
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"installed": True, "state": f"okunamadı: {str(e)[:60]}", "ips": [], "dns": ""}


def cloudflared():
    p = os.path.join(TOOLS, "cloudflared.exe")
    return p if os.path.isfile(p) else shutil.which("cloudflared")


def download_cloudflared():
    """Cloudflare'in resmi cloudflared.exe'si (GitHub yayinindan) araclar klasorune."""
    os.makedirs(TOOLS, exist_ok=True)
    tmp = os.path.join(TOOLS, "cloudflared.exe.part")
    with requests.get(CF_URL, stream=True, timeout=60) as r:
        r.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
    os.replace(tmp, os.path.join(TOOLS, "cloudflared.exe"))


def tunnel_start():
    """Gecici Cloudflare tuneli: https://xxxx.trycloudflare.com -> bu bilgisayardaki uygulama. Hata metni/None."""
    if TUN["proc"] and TUN["proc"].poll() is None:
        return None
    exe = cloudflared()
    if not exe:
        try:
            download_cloudflared()
            exe = cloudflared()
        except (requests.RequestException, OSError) as e:
            return f"cloudflared indirilemedi: {str(e)[:120]}"
    TUN.update(url=None, error="", t=time.time())
    p = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{C.PORT}"], stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", creationflags=NO_WINDOW)
    TUN["proc"] = p

    def read():
        for line in p.stdout:
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
            if m and not TUN["url"]:
                TUN["url"] = m.group(0)
        TUN.update(url=None, error=TUN["error"] or "Tünel kapandı.")
    threading.Thread(target=read, daemon=True).start()
    for _ in range(40):  # adres en fazla ~20 sn'de gelir
        if TUN["url"] or p.poll() is not None:
            break
        time.sleep(0.5)
    return None if TUN["url"] else "Tünel adresi alınamadı (internet / güvenlik duvarı?)."


def tunnel_stop():
    if TUN["proc"] and TUN["proc"].poll() is None:
        TUN["proc"].terminate()
    TUN.update(proc=None, url=None, error="")


def tunnel_host():
    return TUN["url"].split("//", 1)[1] if TUN["url"] else None


def status():
    up = TUN["proc"] is not None and TUN["proc"].poll() is None
    active = getattr(C, "SERVER_ACTIVE", False)  # su an gercekten tum aga acik mi (ayar yeniden baslatinca gecer)
    ts = tailscale()
    return {"server_mode": C.SERVER_MODE, "active": active, "port": C.PORT, "local": f"http://127.0.0.1:{C.PORT}",
            "lan": [f"http://{ip}:{C.PORT}" for ip in lan_ips()] if active else [],
            "ts_urls": [f"http://{ip}:{C.PORT}" for ip in ts.get("ips", []) if "." in ip]
            + ([f"http://{ts['dns']}:{C.PORT}"] if ts.get("dns") else []) if active else [],
            "lan_ips": lan_ips(), "tailscale": ts, "tunnel": {"running": up, "url": TUN["url"] if up else None,
                                                                       "installed": bool(cloudflared()), "error": TUN["error"]}}
