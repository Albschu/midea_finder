# Complete Setup Guide — from clone to running

This walks you through running **midea_finder** on your own PC and watching a
**live web dashboard** that shows which Midea PortaSplit offers are in stock
right now.

Estimated time: ~5 minutes.

---

## 0. Prerequisites

You need **Python 3.8+** and **git**. Check:

```bash
python3 --version     # expect 3.8 or newer
git --version
```

If something is missing (Debian/Ubuntu):

```bash
sudo apt update && sudo apt install -y python3 git
```

There is **nothing else to install** — midea_finder uses only the Python
standard library, and the dashboard runs in the browser you already have.

---

## 1. Clone the repository

```bash
cd ~
git clone https://github.com/Albschu/midea_finder.git
cd midea_finder
```

---

## 2. Create your config

```bash
cp config.example.json config.json
```

Open `config.json` if you want to adjust anything:

```json
{
  "check_interval_seconds": 300,   // how often to check (300 = every 5 min)
  "ui_send_mail": false,           // true = ALSO send e-mail (see step 5)
  "products": [ ... ]              // shop pages to watch
}
```

The `products` list already contains the known PortaSplit pages at OBI, toom and
BAUHAUS. Add or remove entries freely — each is just:

```json
{ "name": "...", "retailer": "...", "url": "https://..." }
```

`config.json` is git-ignored, so your settings stay local.

---

## 3. Start the dashboard

```bash
python3 midea_finder.py
```

You'll see:

```
Midea PortaSplit dashboard running at http://127.0.0.1:8765
Checking every 300s. Open the URL in your browser. Ctrl+C to stop.
```

---

## 4. Open it in your browser

Go to **http://127.0.0.1:8765**.

You get one card per product with:

- a **live status** — 🟢 **VERFÜGBAR**, 🔴 **Ausverkauft**, ⚪ Unbekannt,
- the **price** and a **link** straight to the shop,
- a **countdown** to the next automatic check,
- a **"Jetzt prüfen"** button to check immediately.

The page refreshes itself every few seconds — just leave the tab open. When a
product turns 🟢, its card is highlighted. That's it. ✅

> Keep the terminal window open (or set up the autostart service in step 6).
> Closing it stops the dashboard.

---

## 5. (Optional) Also get an e-mail alert

If you want an e-mail in addition to the dashboard:

1. In **gmx.net** → **Einstellungen → POP3 / IMAP Abruf**, enable
   **"POP3 und IMAP Zugriff erlauben"** (one-time). With 2FA, create an
   **app-specific password**.
2. Set `"ui_send_mail": true` in `config.json`.
3. Provide credentials and verify:

   ```bash
   export MIDEA_SMTP_USER="dein-login@gmx.de"
   export MIDEA_SMTP_PASS="dein-passwort-oder-app-passwort"
   python3 midea_finder.py --test-email     # check your inbox
   python3 midea_finder.py                   # dashboard now also e-mails
   ```

---

## 6. (Optional) Start automatically on boot

To make the dashboard launch on every boot and restart itself if it crashes,
install it as a systemd *user* service:

```bash
cp systemd/midea-finder-ui.service ~/.config/systemd/user/
sed -i "s|%h/midea_finder|$PWD|g" ~/.config/systemd/user/midea-finder-ui.service
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now midea-finder-ui.service
```

Manage it:

```bash
systemctl --user status  midea-finder-ui.service     # running?
journalctl  --user -u     midea-finder-ui.service -f  # logs
systemctl --user restart midea-finder-ui.service      # after editing config.json
systemctl --user disable --now midea-finder-ui.service  # stop for good
```

(If you enabled `ui_send_mail`, also uncomment the `EnvironmentFile` line in the
unit and put your credentials in `~/.config/midea-finder.env`.)

---

## 7. Update later

```bash
cd ~/midea_finder
git pull
# if running as a service:
systemctl --user restart midea-finder-ui.service
```

---

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| Browser can't reach the page | Make sure the program is still running; confirm the port (default `8765`). Use a different one with `--port 9000`. |
| Want to view it from another device | Start with `--host 0.0.0.0` and open `http://<this-pc-ip>:8765` (same network only). |
| A product always shows ⚪ Unbekannt | That shop blocked the request or changed its page layout. Other products are unaffected. |
| Laptop only updates while awake | A sleeping/suspended laptop pauses checks. Use a machine that stays awake, or disable sleep. |
| Test e-mail didn't arrive (step 5) | Recheck POP3/IMAP is enabled and the login/password; with 2FA use an app password. |

---

## How it decides "in stock"

For each product page it reads the shop's **schema.org availability data**
(`InStock` / `OutOfStock`) and, as a fallback, scans the visible German text
("In den Warenkorb" / "nicht verfügbar" …). It remembers the previous status in
`state.json`, so a product that turns green is recognised as *newly* available.
