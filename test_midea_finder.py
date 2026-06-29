#!/usr/bin/env python3
"""Offline unit tests for midea_finder's availability detection.

These tests use synthetic HTML so they run without network access.
Run with:  python3 test_midea_finder.py
"""

import midea_finder as mf

PASSED = 0
FAILED = 0


def check(label, got, want):
    global PASSED, FAILED
    if got == want:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label}: got {got!r}, want {want!r}")


# 1. JSON-LD in stock
html_in = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Midea PortaSplit",
 "offers":{"@type":"Offer","price":"799.00","priceCurrency":"EUR",
           "availability":"https://schema.org/InStock"}}
</script></head><body>In den Warenkorb</body></html>
"""
status, price, _ = mf.detect_availability(html_in)
check("json-ld InStock -> status", status, mf.STATUS_IN)
check("json-ld InStock -> price", price, "799.00 EUR")

# 2. JSON-LD out of stock
html_out = """
<html><head>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"Product","name":"Midea PortaSplit",
 "offers":{"@type":"Offer","price":"799.00","priceCurrency":"EUR",
           "availability":"https://schema.org/OutOfStock"}}
</script></head><body>Derzeit nicht verfügbar</body></html>
"""
status, _, _ = mf.detect_availability(html_out)
check("json-ld OutOfStock -> status", status, mf.STATUS_OUT)

# 3. @graph container with nested product
html_graph = """
<script type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"BreadcrumbList"},
  {"@type":"Product","offers":{"availability":"InStock","price":"850"}}
]}
</script>
"""
status, _, _ = mf.detect_availability(html_graph)
check("json-ld @graph nested -> status", status, mf.STATUS_IN)

# 4. Keyword fallback: out of stock wins over generic 'verfügbar'
html_kw_out = "<html><body>Dieser Artikel ist leider nicht verfügbar.</body></html>"
status, _, _ = mf.detect_availability(html_kw_out)
check("keyword 'nicht verfügbar' -> out", status, mf.STATUS_OUT)

# 5. Keyword fallback: in stock
html_kw_in = "<html><body><button>In den Warenkorb</button> Sofort lieferbar</body></html>"
status, _, _ = mf.detect_availability(html_kw_in)
check("keyword 'in den warenkorb' -> in", status, mf.STATUS_IN)

# 6. Unknown when no signal
status, _, _ = mf.detect_availability("<html><body>Hallo Welt</body></html>")
check("no signal -> unknown", status, mf.STATUS_UNKNOWN)

# 7. Transition logic: build_email renders all items
cfg = {
    "email": {
        "to_addr": "albert.schuetz1@gmx.de",
        "from_addr": "albert.schuetz1@gmx.de",
        "smtp_host": "mail.gmx.net",
    }
}
msg = mf.build_email(cfg, [
    {"name": "PortaSplit", "retailer": "OBI", "url": "https://x", "price": "799 EUR"}
])
body = msg.get_content()
check("email contains product name", "PortaSplit" in body, True)
check("email contains url", "https://x" in body, True)
check("email To header", msg["To"], "albert.schuetz1@gmx.de")

# 8. run_once returns (results, newly_available) and detects out->in transition.
import json
import os
import tempfile

_html_by_url = {}
mf.fetch = lambda url, timeout=25: _html_by_url[url]  # stub out the network

cfg2 = {
    "request_delay_seconds": 0,
    "products": [
        {"name": "P-A", "retailer": "OBI", "url": "https://shop/a"},
        {"name": "P-B", "retailer": "toom", "url": "https://shop/b"},
    ],
}
tmp_state = os.path.join(tempfile.gettempdir(), "mf_test_state.json")
if os.path.exists(tmp_state):
    os.remove(tmp_state)

# first run: A out, B out  -> no transition
_html_by_url = {"https://shop/a": html_out, "https://shop/b": html_out}
results, newly = mf.run_once(cfg2, tmp_state, verbose=False, send_mail=False)
check("run_once returns full results", len(results), 2)
check("first run: nothing newly available", len(newly), 0)

# second run: A now in stock -> one transition
_html_by_url = {"https://shop/a": html_in, "https://shop/b": html_out}
results, newly = mf.run_once(cfg2, tmp_state, verbose=False, send_mail=False)
check("second run: one newly available", len(newly), 1)
check("the right product transitioned", newly[0]["name"], "P-A")

# third run: A still in stock -> no repeat alert
results, newly = mf.run_once(cfg2, tmp_state, verbose=False, send_mail=False)
check("third run: no repeat alert", len(newly), 0)
os.remove(tmp_state)

# 8b. Geo filtering: only stores within radius are kept; online always kept.
osna = {"name": "Osnabrück", "lat": 52.2799, "lon": 8.0472, "radius_km": 100}
# sanity: Münster ~52 km, Bremen ~104 km
d_ms = mf.haversine_km(52.2799, 8.0472, 51.9607, 7.6261)
d_hb = mf.haversine_km(52.2799, 8.0472, 53.0793, 8.8017)
check("Münster within 100km", d_ms < 100, True)
check("Bremen beyond 100km", d_hb > 100, True)

cfg_geo = {
    "location": osna,
    "include_online": True,
    "include_local_stores": True,
    "products": [
        {"kind": "online", "name": "On", "retailer": "OBI", "url": "https://x"},
        {"kind": "store", "name": "Near", "retailer": "OBI", "city": "Münster",
         "lat": 51.9607, "lon": 7.6261, "url": "TODO"},
        {"kind": "store", "name": "Far", "retailer": "toom", "city": "Bremen",
         "lat": 53.0793, "lon": 8.8017, "url": "TODO"},
    ],
}
sel = mf.select_products(cfg_geo)
names = sorted(p["name"] for p, _ in sel)
check("Bremen filtered out, Münster + online kept", names, ["Near", "On"])

# online offers excluded when include_online is false
cfg_no_online = dict(cfg_geo, include_online=False)
sel2 = mf.select_products(cfg_no_online)
check("include_online=false drops online", all(p["kind"] == "store" for p, _ in sel2), True)

# local stores excluded when include_local_stores is false
cfg_no_local = dict(cfg_geo, include_local_stores=False)
sel3 = mf.select_products(cfg_no_local)
check("include_local_stores=false drops stores", all(p["kind"] == "online" for p, _ in sel3), True)

# store without a real URL reports 'not configured' rather than crashing
res_store = mf.check_product(cfg_geo["products"][1])
check("store w/o URL -> unknown", res_store["status"], mf.STATUS_UNKNOWN)
check("store w/o URL -> note", "noch nicht" in res_store["detail"], True)

# 9. UI page + dashboard JSON are well-formed.
check("page has title", "Midea PortaSplit Watcher" in mf.PAGE_HTML, True)
check("page polls status endpoint", "/api/status" in mf.PAGE_HTML, True)
check("dashboard is JSON-serializable", isinstance(json.dumps(mf._dashboard), str), True)

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
