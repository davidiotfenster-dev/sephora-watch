#!/usr/bin/env python3
"""
Vigila un producto de Sephora y avisa (Telegram y/o ntfy) cuando pasa a estar disponible.
Sin dependencias: solo librería estándar (Python 3.9+).

  python watch.py          -> una comprobación (cron / GitHub Actions / Programador de tareas)
  python watch.py --test   -> muestra el estado detectado y envía una notificación de prueba
"""
import gzip
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from html import unescape
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE_FILE = HERE / "state.json"
DEFAULT_URL = "https://www.sephora.es/p/advent-calendar-2026---calendario-de-adviento-820428.html"

REMINDERS = 2             # avisos extra en las siguientes comprobaciones si sigue disponible
ERRORS_BEFORE_ALERT = 3   # fallos seguidos antes de avisarte de que el vigilante no funciona

IN_STOCK = {"instock", "limitedavailability", "onlineonly", "preorder", "presale", "backorder"}
OUT_STOCK = {"outofstock", "soldout", "discontinued", "instoreonly"}
OUT_CUES = ("avísame", "agotado", "sin stock", "fuera de stock", "no disponible")
IN_CUES = ("añadir a la cesta", "añadir al carrito", "añadir a mi cesta")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9",
    "Accept-Encoding": "gzip",
}
LD_RE = re.compile(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)

if sys.stdout:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def load_env():
    env = HERE / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def fetch(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        data = r.read()
        if r.headers.get("Content-Encoding", "").lower() == "gzip":
            data = gzip.decompress(data)
    return data.decode("utf-8", "replace")


def walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def detect(html):
    """Devuelve (estado, detalle). estado: disponible | agotado | desconocido"""
    # 1) Datos estructurados schema.org (lo más fiable)
    found = set()
    for block in LD_RE.findall(html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for node in walk(data):
            av = node.get("availability")
            if isinstance(av, str):
                found.add(av.rstrip("/").rsplit("/", 1)[-1].lower())
    if found & IN_STOCK:
        return "disponible", f"schema.org: {', '.join(sorted(found))}"
    if found & OUT_STOCK:
        return "agotado", f"schema.org: {', '.join(sorted(found))}"

    # 2) Respaldo: texto visible de la página
    text = re.sub(r"<(script|style|noscript)\b.*?</\1>", " ", html, flags=re.S | re.I)
    text = re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", text))).lower()
    out_hits = [c for c in OUT_CUES if c in text]
    in_hits = [c for c in IN_CUES if c in text]
    detail = f"texto: agotado={out_hits} disponible={in_hits}"
    if out_hits:
        return "agotado", detail
    if in_hits:
        return "disponible", detail
    return "desconocido", "no se encontraron señales de stock en la página"


def notify(title, body, urgent=False):
    url = os.getenv("PRODUCT_URL", DEFAULT_URL)
    sent = []

    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    if token and chat:
        try:
            data = urllib.parse.urlencode({"chat_id": chat, "text": f"{title}\n\n{body}"}).encode()
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage",
                                   data=data, timeout=20).read()
            sent.append("telegram")
        except Exception as e:
            print(f"[telegram] error: {e}")

    topic = os.getenv("NTFY_TOPIC")
    if topic:
        try:
            payload = {"topic": topic, "title": title, "message": body, "click": url,
                       "priority": 5 if urgent else 3,
                       "tags": ["rotating_light" if urgent else "bell"]}
            req = urllib.request.Request(os.getenv("NTFY_SERVER", "https://ntfy.sh"),
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=20).read()
            sent.append("ntfy")
        except Exception as e:
            print(f"[ntfy] error: {e}")

    print(f"[aviso] {title} -> {', '.join(sent) or 'NINGÚN CANAL (revisa las variables)'}")
    return bool(sent)


def main():
    load_env()
    url = os.getenv("PRODUCT_URL", DEFAULT_URL)
    state = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {}
    prev = state.get("status")  # último estado válido conocido

    try:
        status, detail = detect(fetch(url))
    except urllib.error.HTTPError as e:
        status, detail = "error", f"HTTP {e.code} (posible bloqueo anti-bot)"
    except Exception as e:
        status, detail = "error", f"{type(e).__name__}: {e}"
    print(f"Estado: {status} | {detail} | anterior: {prev}")

    if "--test" in sys.argv:
        ok = notify("🧪 Prueba vigilante Sephora", f"Estado detectado: {status}\n{detail}\n{url}")
        sys.exit(0 if ok else 1)

    if status in ("disponible", "agotado"):
        if state.get("error_alerted"):
            notify("✅ Vigilante Sephora funcionando de nuevo", f"Estado actual: {status}")
        if status == "disponible":
            if prev != "disponible":
                notify("🚨 ¡YA DISPONIBLE! Calendario de Adviento Sephora",
                       f"Corre a comprarlo:\n{url}", urgent=True)
                state["reminders"] = REMINDERS
            elif state.get("reminders", 0) > 0:
                notify("⏰ Sigue disponible", f"Aún puedes comprarlo:\n{url}", urgent=True)
                state["reminders"] -= 1
        elif prev == "disponible":
            notify("Se ha vuelto a agotar", f"Sigo vigilando.\n{url}")
            state["reminders"] = 0
        state.update(status=status, errors=0, error_alerted=False)
    else:
        state["errors"] = state.get("errors", 0) + 1
        if state["errors"] >= ERRORS_BEFORE_ALERT and not state.get("error_alerted"):
            notify("⚠️ El vigilante de Sephora no puede leer la página", f"{detail}\n{url}")
            state["error_alerted"] = True

    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
