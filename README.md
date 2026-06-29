# midea_finder

A small Python program that does what [braucheklima.de](https://www.braucheklima.de/)
does for the **Midea PortaSplit**: it watches German DIY-store product pages
(OBI, toom, BAUHAUS, HORNBACH, …) and **sends you an e-mail the moment a unit
that was sold out becomes available again** — straight to
`albert.schuetz1@gmx.de`.

No external dependencies — it uses only the Python standard library
(Python 3.8+).

## How it works

1. For every product in `config.json` it downloads the retailer's product page.
2. It detects availability in two ways:
   - **schema.org JSON-LD** `offers.availability` (`InStock` / `OutOfStock`) —
     the reliable, machine-readable signal that most shops embed.
   - a **keyword fallback** scanning the visible German text
     ("In den Warenkorb", "sofort lieferbar" vs. "nicht verfügbar",
     "ausverkauft", …).
3. It remembers the last status of each product in `state.json`.
4. When a product flips **out-of-stock → in-stock**, it sends one e-mail.
   It will not spam you again while the item stays available.

## Setup

```bash
# 1. create your local config from the template
cp config.example.json config.json

# 2. provide the sending mailbox credentials via environment variables
#    (so they never land in the repo)
export MIDEA_SMTP_USER="dein-gmx-login@gmx.de"
export MIDEA_SMTP_PASS="dein-gmx-passwort-oder-app-passwort"

# 3. verify e-mail works
python3 midea_finder.py --test-email
```

> **GMX note:** to send mail through `mail.gmx.net` you must first enable
> **POP3/IMAP access** in your GMX account settings
> (*Einstellungen → POP3/IMAP Abruf*). The defaults in `config.json`
> (`mail.gmx.net:465`, SSL) are correct for GMX.

## Usage

```bash
# single check – ideal for cron / a systemd timer
python3 midea_finder.py --once

# run forever, checking every check_interval_seconds (default 300s = 5 min)
python3 midea_finder.py --loop

# send a test e-mail and exit
python3 midea_finder.py --test-email
```

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `check_interval_seconds` | delay between checks in `--loop` mode |
| `request_delay_seconds` | polite pause between individual product requests |
| `email.to_addr` | recipient (your GMX address) |
| `email.from_addr` | sender address |
| `email.smtp_host` / `smtp_port` / `smtp_ssl` | SMTP server settings |
| `products[]` | list of `{name, retailer, url}` pages to watch |

Add or remove retailers freely — just add another entry to `products`.
The known Midea PortaSplit product pages are pre-filled.

## Run it automatically

**cron** (every 5 minutes):

```cron
*/5 * * * * MIDEA_SMTP_USER='you@gmx.de' MIDEA_SMTP_PASS='secret' \
  /usr/bin/python3 /path/to/midea_finder/midea_finder.py --once >> /path/to/midea_finder/run.log 2>&1
```

**systemd timer** — see `systemd/` for ready-to-edit unit files:

```bash
cp systemd/midea-finder.* ~/.config/systemd/user/
# edit the unit to set your paths and SMTP env vars, then:
systemctl --user daemon-reload
systemctl --user enable --now midea-finder.timer
```

## Tests

```bash
python3 test_midea_finder.py
```

The tests run fully offline (synthetic HTML), so they need no network.

## Notes & limitations

- braucheklima.de additionally maps **per-branch** stock for ~1,100 stores.
  This program tracks **online/product-page** availability, which is what you
  need to actually order. To watch a specific branch, add that branch's
  product URL (where the shop exposes one) to `products`.
- Some shops use aggressive bot protection. The program sends a realistic
  browser `User-Agent`; if a shop still blocks plain HTTP requests, that
  product will show `unknown` (logged, never crashes the run).
