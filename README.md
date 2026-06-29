# midea_finder

A small Python program that does what [braucheklima.de](https://www.braucheklima.de/)
does for the **Midea PortaSplit**: it watches German DIY-store product pages
(OBI, toom, BAUHAUS, HORNBACH, …) and shows you, **live in a web dashboard**,
which ones are in stock right now. (It can optionally also e-mail you — see below.)

No external dependencies — it uses only the Python standard library
(Python 3.8+).

> **New here?** Follow [`SETUP.md`](SETUP.md) for a complete step-by-step guide.

## Quick start (web dashboard)

```bash
cp config.example.json config.json     # one-time
python3 midea_finder.py                 # starts the dashboard (default mode)
```

Then open **http://127.0.0.1:8765** in your browser. You'll see one card per
product with a live status — 🟢 **VERFÜGBAR**, 🔴 **Ausverkauft** or ⚪ unknown —
the price, a link to the shop, a countdown to the next check, and a
**"Jetzt prüfen"** button to check immediately. The page refreshes itself; just
leave the tab open.

## How it works

1. For every product in `config.json` it downloads the retailer's product page.
2. It detects availability in two ways:
   - **schema.org JSON-LD** `offers.availability` (`InStock` / `OutOfStock`) —
     the reliable, machine-readable signal that most shops embed.
   - a **keyword fallback** scanning the visible German text
     ("In den Warenkorb", "sofort lieferbar" vs. "nicht verfügbar",
     "ausverkauft", …).
3. It remembers the last status of each product in `state.json`.
4. The dashboard shows every product's current status. A product that flips
   **out-of-stock → in-stock** is highlighted (and, if `ui_send_mail` is on,
   also triggers an e-mail — without spamming you while it stays available).

## Optional: e-mail alerts

The dashboard is the default. If you *also* want an e-mail when something comes
back in stock, set `"ui_send_mail": true` in `config.json` and provide SMTP
credentials (see the e-mail setup below). There is also a headless
`--loop` mode that only e-mails and runs no UI.

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
# live web dashboard (default mode) – open http://127.0.0.1:8765
python3 midea_finder.py
python3 midea_finder.py --serve --host 0.0.0.0 --port 9000   # custom bind/port

# single check, print to terminal, and exit
python3 midea_finder.py --once

# headless: run forever and only e-mail (no UI)
python3 midea_finder.py --loop

# send a test e-mail and exit
python3 midea_finder.py --test-email
```

> By default the dashboard binds to `127.0.0.1` (only reachable from your own
> PC). Use `--host 0.0.0.0` only if you want to reach it from other devices on
> your network.

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

## Run it permanently on your own Linux machine

**Just leaving the dashboard open in a terminal** is the simplest option —
`python3 midea_finder.py` and open the browser tab.

To have the dashboard **start automatically on boot and restart on crashes**,
install it as a systemd *user* service:

```bash
cp systemd/midea-finder-ui.service ~/.config/systemd/user/
sed -i "s|%h/midea_finder|$PWD|g" ~/.config/systemd/user/midea-finder-ui.service
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now midea-finder-ui.service
# then open http://127.0.0.1:8765
```

### E-mail variant

If you instead want the headless **e-mail** service (no UI), the included
installer sets it up — registers a systemd user service that starts on boot,
restarts on crashes, and keeps running even when you are logged out:

```bash
./install-linux.sh
```

The script will:
1. create `config.json` (if missing),
2. ask for your GMX login + password and store them in `~/.config/midea-finder.env` (permissions `600`),
3. send a test e-mail to confirm SMTP works,
4. enable *linger* so the service survives logout / reboots,
5. enable and start `midea-finder-loop.service`.

Handy commands afterwards:

```bash
systemctl --user status midea-finder-loop.service     # is it running?
journalctl --user -u midea-finder-loop.service -f      # live logs
systemctl --user restart midea-finder-loop.service     # after editing config.json
systemctl --user disable --now midea-finder-loop.service  # stop for good
```

The watcher checks every `check_interval_seconds` (default 300s = 5 min).

### Alternatives

**cron** (every 5 minutes, one-shot mode):

```cron
*/5 * * * * MIDEA_SMTP_USER='you@gmx.de' MIDEA_SMTP_PASS='secret' \
  /usr/bin/python3 /path/to/midea_finder/midea_finder.py --once >> /path/to/midea_finder/run.log 2>&1
```

**systemd one-shot timer** — `systemd/midea-finder.service` + `midea-finder.timer`
run a single check on a schedule instead of a continuous loop.

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
