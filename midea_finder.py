#!/usr/bin/env python3
"""
midea_finder - Stock alert for the Midea PortaSplit (a la braucheklima.de)

Checks a configurable list of German retailer product pages (OBI, toom, BAUHAUS,
HORNBACH, ...) for the Midea PortaSplit and sends an e-mail as soon as a product
that was previously sold out becomes available again ("back in stock").

Zero external dependencies - uses only the Python standard library, so it runs
anywhere Python 3.8+ is installed.

Usage:
    # one single check (ideal for cron / systemd timer)
    python3 midea_finder.py --once

    # run forever, checking every N seconds (default from config)
    python3 midea_finder.py --loop

    # send a test e-mail to verify the SMTP configuration
    python3 midea_finder.py --test-email

Configuration:
    Non-secret settings live in config.json (see config.example.json).
    SMTP credentials are read from environment variables so they never end up
    in the repository:
        MIDEA_SMTP_USER   - login user for the sending mailbox
        MIDEA_SMTP_PASS   - password / app-password for that mailbox
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import smtplib
import ssl
import sys
import threading
import time
import zlib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CONFIG_PATH = os.path.join(HERE, "config.json")
DEFAULT_STATE_PATH = os.path.join(HERE, "state.json")

# A real browser User-Agent. These shops reject obvious bots / empty UAs.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "de-DE,de;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

# Words that signal the item can be bought right now.
IN_STOCK_HINTS = [
    "in den warenkorb",
    "in den einkaufswagen",
    "jetzt kaufen",
    "sofort lieferbar",
    "auf lager",
    "verfügbar",
    "lieferbar",
    "online bestellbar",
    "instock",
    "in stock",
]

# Words that signal the item is sold out. Checked first - they win over the
# generic "verfügbar" hint (e.g. "nicht verfügbar" / "leider ausverkauft").
OUT_OF_STOCK_HINTS = [
    "nicht verfügbar",
    "derzeit nicht verfügbar",
    "ausverkauft",
    "vergriffen",
    "nicht auf lager",
    "nicht lieferbar",
    "benachrichtigen sie mich",
    "benachrichtigung bei verfügbarkeit",
    "outofstock",
    "out of stock",
    "soldout",
    "sold out",
]

STATUS_IN = "in_stock"
STATUS_OUT = "out_of_stock"
STATUS_UNKNOWN = "unknown"


# --------------------------------------------------------------------------- #
# Configuration / state helpers
# --------------------------------------------------------------------------- #
def load_config(path: str) -> dict:
    if not os.path.exists(path):
        sys.exit(
            f"Config file not found: {path}\n"
            f"Copy config.example.json to config.json and adjust it."
        )
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def save_state(path: str, state: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Geo helpers (limit local-store search to a radius around a location)
# --------------------------------------------------------------------------- #
def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance between two lat/lon points in kilometres."""
    import math

    r = 6371.0  # Earth radius in km
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def store_distance_km(product: dict, location: dict):
    """Distance of a store product from the configured location, or None."""
    if not location:
        return None
    try:
        return haversine_km(
            float(location["lat"]), float(location["lon"]),
            float(product["lat"]), float(product["lon"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def select_products(cfg: dict):
    """
    Apply the configured scope: keep online offers and only those stores within
    the location radius. Returns a list of (product, distance_km) pairs;
    distance_km is None for online offers.
    """
    location = cfg.get("location")
    radius = float(location["radius_km"]) if location and location.get("radius_km") else None
    include_online = cfg.get("include_online", True)
    include_local = cfg.get("include_local_stores", True)

    selected = []
    for product in cfg.get("products", []):
        kind = product.get("kind", "online")
        if kind == "store":
            if not include_local:
                continue
            dist = store_distance_km(product, location)
            # if we have both a radius and a known distance, enforce the radius
            if radius is not None and dist is not None and dist > radius:
                continue
            selected.append((product, dist))
        else:  # online
            if not include_online:
                continue
            selected.append((product, None))
    return selected


# --------------------------------------------------------------------------- #
# Fetching + availability detection
# --------------------------------------------------------------------------- #
def fetch(url: str, timeout: int = 25) -> str:
    """Download a URL and return decoded text, handling gzip/deflate."""
    req = Request(url, headers=BROWSER_HEADERS)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
        encoding = (resp.headers.get("Content-Encoding") or "").lower()
        if "gzip" in encoding:
            raw = gzip.decompress(raw)
        elif "deflate" in encoding:
            try:
                raw = zlib.decompress(raw)
            except zlib.error:
                raw = zlib.decompress(raw, -zlib.MAX_WBITS)
        charset = resp.headers.get_content_charset() or "utf-8"
        return raw.decode(charset, errors="replace")


def _iter_json_ld(html: str):
    """Yield every parsed JSON-LD object found in the page."""
    pattern = re.compile(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        re.IGNORECASE | re.DOTALL,
    )
    for block in pattern.findall(html):
        block = block.strip()
        if not block:
            continue
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        # A block may be a single object, a list, or an @graph container.
        stack = [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                yield item
                if "@graph" in item and isinstance(item["@graph"], list):
                    stack.extend(item["@graph"])


def _availability_from_json_ld(html: str):
    """
    Return (status, price, detail) from schema.org Product/Offer JSON-LD,
    or None if no usable availability info is present.
    """
    for obj in _iter_json_ld(html):
        offers = obj.get("offers")
        if offers is None:
            continue
        for offer in offers if isinstance(offers, list) else [offers]:
            if not isinstance(offer, dict):
                continue
            avail = str(offer.get("availability", "")).lower()
            price = offer.get("price") or offer.get("lowPrice")
            currency = offer.get("priceCurrency", "")
            price_str = f"{price} {currency}".strip() if price else None
            if not avail:
                continue
            if "instock" in avail or "limitedavailability" in avail or "preorder" in avail:
                return STATUS_IN, price_str, f"JSON-LD availability={avail}"
            if "outofstock" in avail or "soldout" in avail or "discontinued" in avail:
                return STATUS_OUT, price_str, f"JSON-LD availability={avail}"
    return None


def _availability_from_keywords(html: str):
    """Heuristic fallback based on visible German shop wording."""
    text = unescape(re.sub(r"<[^>]+>", " ", html)).lower()
    text = re.sub(r"\s+", " ", text)

    for hint in OUT_OF_STOCK_HINTS:
        if hint in text:
            return STATUS_OUT, None, f"keyword: '{hint}'"
    for hint in IN_STOCK_HINTS:
        if hint in text:
            return STATUS_IN, None, f"keyword: '{hint}'"
    return STATUS_UNKNOWN, None, "no availability signal found"


def detect_availability(html: str):
    """
    Determine availability for a product page.

    Returns (status, price, detail) where status is one of
    in_stock / out_of_stock / unknown.
    """
    result = _availability_from_json_ld(html)
    if result is not None:
        return result
    return _availability_from_keywords(html)


def check_product(product: dict) -> dict:
    """Check a single product and return a result dict."""
    url = product.get("url")
    name = product.get("name", url or "")
    retailer = product.get("retailer", "")
    kind = product.get("kind", "online")
    result = {
        "name": name,
        "retailer": retailer,
        "url": url,
        "kind": kind,
        "store": product.get("store"),
        "city": product.get("city"),
        "status": STATUS_UNKNOWN,
        "price": None,
        "detail": "",
        "checked_at": now_iso(),
    }
    # store entries without a verified market URL are in-scope but not yet checkable
    if not url or str(url).startswith("TODO"):
        result["detail"] = "Markt-URL noch nicht hinterlegt"
        return result
    try:
        html = fetch(url)
    except HTTPError as exc:
        result["detail"] = f"HTTP error {exc.code}"
        return result
    except (URLError, TimeoutError) as exc:
        result["detail"] = f"network error: {exc}"
        return result
    except Exception as exc:  # noqa: BLE001 - never let one product kill the run
        result["detail"] = f"error: {exc}"
        return result

    status, price, detail = detect_availability(html)
    result.update(status=status, price=price, detail=detail)
    return result


# --------------------------------------------------------------------------- #
# E-mail
# --------------------------------------------------------------------------- #
def build_email(cfg: dict, available: list[dict]) -> EmailMessage:
    email_cfg = cfg["email"]
    msg = EmailMessage()
    msg["Subject"] = email_cfg.get(
        "subject", "✅ Midea PortaSplit wieder verfügbar!"
    )
    msg["From"] = email_cfg["from_addr"]
    msg["To"] = email_cfg["to_addr"]

    lines = ["Die folgenden Midea PortaSplit Angebote sind wieder verfügbar:\n"]
    for item in available:
        price = f" - {item['price']}" if item.get("price") else ""
        retailer = f"[{item['retailer']}] " if item.get("retailer") else ""
        lines.append(f"• {retailer}{item['name']}{price}")
        lines.append(f"  {item['url']}")
        lines.append("")
    lines.append(f"Geprüft am {now_iso()} von midea_finder.")
    msg.set_content("\n".join(lines))
    return msg


def send_email(cfg: dict, msg: EmailMessage) -> None:
    email_cfg = cfg["email"]
    host = email_cfg["smtp_host"]
    port = int(email_cfg.get("smtp_port", 465))
    user = os.environ.get("MIDEA_SMTP_USER") or email_cfg.get("smtp_user")
    password = os.environ.get("MIDEA_SMTP_PASS") or email_cfg.get("smtp_pass")

    if not user or not password:
        raise RuntimeError(
            "SMTP credentials missing. Set MIDEA_SMTP_USER and MIDEA_SMTP_PASS "
            "environment variables (or smtp_user/smtp_pass in config.json)."
        )

    context = ssl.create_default_context()
    use_ssl = email_cfg.get("smtp_ssl", port == 465)
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, context=context, timeout=30) as server:
            server.login(user, password)
            server.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as server:
            server.ehlo()
            server.starttls(context=context)
            server.login(user, password)
            server.send_message(msg)


# --------------------------------------------------------------------------- #
# Core run
# --------------------------------------------------------------------------- #
def run_once(cfg: dict, state_path: str, verbose: bool = True, send_mail: bool = True):
    """
    Check all products once and update state.

    On an out->in transition the product is collected as "newly available";
    if send_mail is True an e-mail is sent for those products.

    Returns (results, newly_available) where results is the full list of every
    product's current result and newly_available is the subset that just came
    back in stock.
    """
    state = load_state(state_path)
    selected = select_products(cfg)
    results = []
    newly_available = []

    for product, distance in selected:
        result = check_product(product)
        result["distance_km"] = round(distance, 1) if distance is not None else None
        key = result["url"] or f"{result['retailer']}|{result['store']}|{result['name']}"
        prev = state.get(key, {}).get("status", STATUS_UNKNOWN)
        curr = result["status"]
        result["previous"] = prev

        if verbose:
            icon = {"in_stock": "🟢", "out_of_stock": "🔴"}.get(curr, "⚪")
            price = f" ({result['price']})" if result.get("price") else ""
            where = "🌐 online" if result["kind"] == "online" else \
                f"📍 {result.get('city') or result.get('store') or '?'}" \
                + (f" {result['distance_km']}km" if result.get("distance_km") is not None else "")
            print(
                f"{icon} {result['retailer'] or '-':9} {where:18} "
                f"{result['name'][:40]:40} -> {curr}{price}  [{result['detail']}]"
            )

        # A real transition into stock (was out/unknown -> in).
        if curr == STATUS_IN and prev in (STATUS_OUT, STATUS_UNKNOWN):
            newly_available.append(result)

        state[key] = {
            "status": curr,
            "price": result.get("price"),
            "name": result["name"],
            "retailer": result.get("retailer"),
            "last_checked": result["checked_at"],
        }
        results.append(result)
        # be polite between requests
        if result.get("url") and not str(result.get("url")).startswith("TODO"):
            time.sleep(cfg.get("request_delay_seconds", 2))

    # display order: in-stock first, then online before stores, then by distance
    def sort_key(r):
        status_rank = {STATUS_IN: 0, STATUS_OUT: 1, STATUS_UNKNOWN: 2}.get(r["status"], 3)
        kind_rank = 0 if r["kind"] == "online" else 1
        dist = r["distance_km"] if r.get("distance_km") is not None else 0
        return (status_rank, kind_rank, dist)

    results.sort(key=sort_key)

    if newly_available and send_mail:
        if verbose:
            print(f"\n📧 {len(newly_available)} product(s) back in stock - sending e-mail...")
        try:
            msg = build_email(cfg, newly_available)
            send_email(cfg, msg)
            if verbose:
                print(f"   E-mail sent to {cfg['email']['to_addr']}.")
        except Exception as exc:  # noqa: BLE001
            print(f"   ERROR sending e-mail: {exc}", file=sys.stderr)
            # Revert state so the alert is retried on the next run.
            for item in newly_available:
                state[item["url"]]["status"] = STATUS_OUT

    save_state(state_path, state)
    return results, newly_available


def run_loop(cfg: dict, state_path: str) -> None:
    interval = int(cfg.get("check_interval_seconds", 300))
    print(f"Starting watch loop (every {interval}s). Ctrl+C to stop.\n")
    while True:
        print(f"--- check at {now_iso()} ---")
        try:
            run_once(cfg, state_path, send_mail=True)
        except Exception as exc:  # noqa: BLE001 - keep the loop alive
            print(f"Run failed: {exc}", file=sys.stderr)
        print(f"--- sleeping {interval}s ---\n")
        time.sleep(interval)


def test_email(cfg: dict) -> None:
    msg = EmailMessage()
    msg["Subject"] = "midea_finder - Test-E-Mail"
    msg["From"] = cfg["email"]["from_addr"]
    msg["To"] = cfg["email"]["to_addr"]
    msg.set_content(
        "Dies ist eine Test-E-Mail von midea_finder.\n"
        "Wenn du sie erhältst, ist die SMTP-Konfiguration korrekt.\n"
        f"Zeit: {now_iso()}"
    )
    send_email(cfg, msg)
    print(f"Test e-mail sent to {cfg['email']['to_addr']}.")


# --------------------------------------------------------------------------- #
# Web UI (zero-dependency dashboard)
# --------------------------------------------------------------------------- #
# Shared between the background checker thread and the HTTP handlers.
_dash_lock = threading.Lock()
_dashboard = {
    "results": [],
    "last_run": None,       # ISO string of the last completed check
    "next_run_epoch": None,  # unix ts when the next check is due
    "interval": 300,
    "checking": False,
    "send_mail": False,
    "scope": "",
}
_check_now = threading.Event()   # set by the UI "Check now" button
_stop = threading.Event()

PAGE_HTML = """<!doctype html>
<html lang="de"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Midea PortaSplit Watcher</title>
<style>
  :root { color-scheme: light dark; }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
         margin: 0; background: #0f1115; color: #e6e8eb; }
  header { padding: 20px 24px; background: #171a21; border-bottom: 1px solid #262b35;
           display: flex; align-items: center; gap: 16px; flex-wrap: wrap; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  .scope { font-size: 12px; color: #9aa3b2; margin-top: 3px; }
  .meta { font-size: 13px; color: #9aa3b2; margin-left: auto; text-align: right; }
  button { background: #2d6cdf; color: #fff; border: 0; padding: 8px 14px;
           border-radius: 8px; font-size: 13px; cursor: pointer; }
  button:disabled { opacity: .5; cursor: default; }
  main { padding: 16px 24px 40px; max-width: 1000px; margin: 0 auto; }
  .grid { display: grid; gap: 12px; }
  .card { background: #171a21; border: 1px solid #262b35; border-radius: 12px;
          padding: 14px 16px; display: flex; align-items: center; gap: 14px; }
  .card.in { border-color: #2e7d44; box-shadow: 0 0 0 1px #2e7d4455; }
  .dot { width: 12px; height: 12px; border-radius: 50%; flex: none; }
  .in .dot { background: #3fb950; } .out .dot { background: #f85149; }
  .unknown .dot { background: #8b949e; }
  .info { flex: 1; min-width: 0; }
  .name { font-weight: 600; font-size: 15px; }
  .sub { font-size: 12px; color: #9aa3b2; margin-top: 2px;
         white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .badge { font-size: 12px; font-weight: 600; padding: 4px 10px; border-radius: 999px; }
  .in .badge { background: #2e7d4433; color: #3fb950; }
  .out .badge { background: #f8514922; color: #f85149; }
  .unknown .badge { background: #8b949e22; color: #b6bdc7; }
  .price { font-weight: 600; font-size: 14px; min-width: 90px; text-align: right; }
  a { color: #6cb0ff; text-decoration: none; } a:hover { text-decoration: underline; }
  .empty { color: #9aa3b2; padding: 40px; text-align: center; }
</style></head>
<body>
<header>
  <div>
    <h1>🌡️ Midea PortaSplit Watcher</h1>
    <div id="scope" class="scope"></div>
  </div>
  <button id="checkBtn" onclick="checkNow()">Jetzt prüfen</button>
  <div class="meta">
    <div id="status">lädt…</div>
    <div id="timer"></div>
  </div>
</header>
<main><div id="grid" class="grid"></div></main>
<script>
let nextEpoch = null;
function fmt(ts){ if(!ts) return "–"; return new Date(ts).toLocaleString("de-DE"); }
async function load(){
  try {
    const r = await fetch("/api/status"); const d = await r.json();
    nextEpoch = d.next_run_epoch ? d.next_run_epoch*1000 : null;
    document.getElementById("status").textContent =
      (d.checking ? "⏳ prüfe gerade…" : "Letzte Prüfung: " + fmt(d.last_run)) +
      (d.send_mail ? " · 📧 E-Mail an" : "") ;
    document.getElementById("scope").textContent = d.scope || "";
    document.getElementById("checkBtn").disabled = d.checking;
    const g = document.getElementById("grid");
    if(!d.results.length){ g.innerHTML = '<div class="empty">Noch keine Daten – erste Prüfung läuft…</div>'; return; }
    g.innerHTML = d.results.map(p => {
      const cls = p.status === "in_stock" ? "in" : p.status === "out_of_stock" ? "out" : "unknown";
      const label = p.status === "in_stock" ? "VERFÜGBAR" : p.status === "out_of_stock" ? "Ausverkauft" : "Unbekannt";
      const where = p.kind === "store"
        ? `📍 ${p.store || p.city || "Filiale"}${p.distance_km != null ? " · " + p.distance_km + " km" : ""}`
        : "🌐 Online";
      const title = `${p.retailer ? "["+p.retailer+"] " : ""}${p.name}`;
      const name = p.url ? `<a href="${p.url}" target="_blank" rel="noopener">${title}</a>` : title;
      return `<div class="card ${cls}">
        <span class="dot"></span>
        <div class="info">
          <div class="name">${name}</div>
          <div class="sub">${where} · ${p.detail || ""}</div>
        </div>
        <div class="price">${p.price || ""}</div>
        <span class="badge">${label}</span>
      </div>`;
    }).join("");
  } catch(e){ document.getElementById("status").textContent = "Verbindung verloren…"; }
}
function tick(){
  if(nextEpoch){
    const s = Math.max(0, Math.round((nextEpoch - Date.now())/1000));
    document.getElementById("timer").textContent = "Nächste Prüfung in " + s + "s";
  }
}
async function checkNow(){
  document.getElementById("checkBtn").disabled = true;
  await fetch("/api/check", {method:"POST"}); setTimeout(load, 500);
}
load(); setInterval(load, 5000); setInterval(tick, 1000);
</script>
</body></html>
"""


def _make_handler():
    class Handler(BaseHTTPRequestHandler):
        def _send(self, code, body, content_type="application/json"):
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/index.html"):
                self._send(200, PAGE_HTML, "text/html")
            elif self.path == "/api/status":
                with _dash_lock:
                    self._send(200, json.dumps(_dashboard))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def do_POST(self):
            if self.path == "/api/check":
                _check_now.set()
                self._send(200, json.dumps({"ok": True}))
            else:
                self._send(404, json.dumps({"error": "not found"}))

        def log_message(self, *args):  # silence default request logging
            pass

    return Handler


def serve(cfg: dict, state_path: str, host: str, port: int) -> None:
    interval = int(cfg.get("check_interval_seconds", 300))
    send_mail = bool(cfg.get("ui_send_mail", False))
    selected = select_products(cfg)
    n_online = sum(1 for _, d in selected if d is None)
    n_store = len(selected) - n_online
    loc = cfg.get("location") or {}
    scope_parts = []
    if cfg.get("include_online", True):
        scope_parts.append(f"{n_online} Online")
    if cfg.get("include_local_stores", True) and loc:
        scope_parts.append(
            f"{n_store} Filialen ≤ {loc.get('radius_km')} km um {loc.get('name', '?')}"
        )
    scope = " · ".join(scope_parts)
    with _dash_lock:
        _dashboard["interval"] = interval
        _dashboard["send_mail"] = send_mail
        _dashboard["scope"] = scope

    def worker():
        while not _stop.is_set():
            with _dash_lock:
                _dashboard["checking"] = True
            try:
                results, _ = run_once(cfg, state_path, verbose=False, send_mail=send_mail)
            except Exception as exc:  # noqa: BLE001 - keep the UI alive
                results = [{
                    "name": "Prüfung fehlgeschlagen", "retailer": "", "url": "#",
                    "status": STATUS_UNKNOWN, "price": None, "detail": str(exc),
                }]
            with _dash_lock:
                _dashboard["results"] = results
                _dashboard["last_run"] = now_iso()
                _dashboard["checking"] = False
                _dashboard["next_run_epoch"] = int(time.time()) + interval
            # wait for the interval, but wake early on a manual "Check now"
            _check_now.wait(interval)
            _check_now.clear()

    threading.Thread(target=worker, daemon=True).start()

    httpd = ThreadingHTTPServer((host, port), _make_handler())
    url = f"http://{host}:{port}"
    print(f"Midea PortaSplit dashboard running at {url}")
    print(f"Checking every {interval}s. Open the URL in your browser. Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping…")
    finally:
        _stop.set()
        httpd.shutdown()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Midea PortaSplit stock alert (braucheklima.de style)."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="path to config.json")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="path to state.json")
    parser.add_argument("--host", default="127.0.0.1", help="web UI bind host (--serve)")
    parser.add_argument("--port", type=int, default=8765, help="web UI port (--serve)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--serve", action="store_true", help="run the live web dashboard (default)")
    mode.add_argument("--once", action="store_true", help="run a single check and exit")
    mode.add_argument("--loop", action="store_true", help="run forever on an interval (e-mail mode)")
    mode.add_argument("--test-email", action="store_true", help="send a test e-mail and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.test_email:
        test_email(cfg)
        return 0
    if args.loop:
        run_loop(cfg, args.state)
        return 0
    if args.once:
        run_once(cfg, args.state, send_mail=True)
        return 0

    # default: the web dashboard
    serve(cfg, args.state, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
