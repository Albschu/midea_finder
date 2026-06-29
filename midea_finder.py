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
import time
import zlib
from datetime import datetime, timezone
from email.message import EmailMessage
from html import unescape
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
    url = product["url"]
    name = product.get("name", url)
    retailer = product.get("retailer", "")
    result = {
        "name": name,
        "retailer": retailer,
        "url": url,
        "status": STATUS_UNKNOWN,
        "price": None,
        "detail": "",
        "checked_at": now_iso(),
    }
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
def run_once(cfg: dict, state_path: str, verbose: bool = True) -> list[dict]:
    """Check all products once, e-mail on out->in transitions, update state."""
    state = load_state(state_path)
    products = cfg.get("products", [])
    newly_available = []

    for product in products:
        result = check_product(product)
        key = result["url"]
        prev = state.get(key, {}).get("status", STATUS_UNKNOWN)
        curr = result["status"]

        if verbose:
            icon = {"in_stock": "🟢", "out_of_stock": "🔴"}.get(curr, "⚪")
            price = f" ({result['price']})" if result.get("price") else ""
            print(
                f"{icon} {result['retailer'] or '-':9} {result['name'][:45]:45} "
                f"-> {curr}{price}  [{result['detail']}]"
            )

        # Alert only on a real transition into stock (was out/unknown -> in).
        if curr == STATUS_IN and prev in (STATUS_OUT, STATUS_UNKNOWN):
            newly_available.append(result)

        state[key] = {
            "status": curr,
            "price": result.get("price"),
            "name": result["name"],
            "retailer": result.get("retailer"),
            "last_checked": result["checked_at"],
        }
        # be polite between requests
        time.sleep(cfg.get("request_delay_seconds", 2))

    if newly_available:
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
    return newly_available


def run_loop(cfg: dict, state_path: str) -> None:
    interval = int(cfg.get("check_interval_seconds", 300))
    print(f"Starting watch loop (every {interval}s). Ctrl+C to stop.\n")
    while True:
        print(f"--- check at {now_iso()} ---")
        try:
            run_once(cfg, state_path)
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
# CLI
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Midea PortaSplit stock alert (braucheklima.de style)."
    )
    parser.add_argument("--config", default=DEFAULT_CONFIG_PATH, help="path to config.json")
    parser.add_argument("--state", default=DEFAULT_STATE_PATH, help="path to state.json")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--once", action="store_true", help="run a single check (default)")
    mode.add_argument("--loop", action="store_true", help="run forever on an interval")
    mode.add_argument("--test-email", action="store_true", help="send a test e-mail and exit")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)

    if args.test_email:
        test_email(cfg)
        return 0
    if args.loop:
        run_loop(cfg, args.state)
        return 0

    # default: single run
    run_once(cfg, args.state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
