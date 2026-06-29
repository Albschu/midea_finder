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

print(f"\n{PASSED} passed, {FAILED} failed")
raise SystemExit(1 if FAILED else 0)
