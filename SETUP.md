# Complete Setup Guide — from clone to permanently running

This walks you through everything needed to run **midea_finder** forever on an
always-on Linux machine, so it e-mails `albert.schuetz1@gmx.de` the moment a
Midea PortaSplit is back in stock.

Estimated time: ~10 minutes.

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
standard library.

---

## 1. Clone the repository

```bash
cd ~
git clone https://github.com/Albschu/midea_finder.git
cd midea_finder
```

You should now see `midea_finder.py`, `config.example.json`, `install-linux.sh`,
etc. (`ls`).

---

## 2. Prepare your GMX mailbox (one-time, important)

GMX blocks programs from sending mail until you switch it on:

1. Log in at **gmx.net**.
2. Go to **Einstellungen → POP3 / IMAP Abruf**.
3. Enable **"POP3 und IMAP Zugriff erlauben"** and save.

That's the account the program will *log in to* in order to send the alert.
You can send the alert from your own address to your own address — that's fine.

> If you have two-factor authentication on GMX, create an **app-specific
> password** and use that instead of your normal password.

---

## 3. Create and review your config

```bash
cp config.example.json config.json
```

Open `config.json` and check these fields:

```json
{
  "check_interval_seconds": 300,          // how often to check (300 = every 5 min)
  "email": {
    "to_addr":   "albert.schuetz1@gmx.de",  // who gets the alert (you)
    "from_addr": "albert.schuetz1@gmx.de",  // sender (your GMX address)
    "smtp_host": "mail.gmx.net",            // correct for GMX
    "smtp_port": 465,
    "smtp_ssl":  true
  },
  "products": [ ... ]                       // the shop pages to watch
}
```

The `products` list already contains the known PortaSplit pages at OBI, toom and
BAUHAUS. Add or remove entries freely — each is just:

```json
{ "name": "...", "retailer": "...", "url": "https://..." }
```

> **Do not** put your password in this file. Credentials are handled in the next
> step. `config.json` is git-ignored, so your settings stay local.

---

## 4. Install and start it (one command)

```bash
./install-linux.sh
```

The script does the whole setup for you:

1. asks for your **GMX login and password** and stores them in
   `~/.config/midea-finder.env` with `600` permissions (only you can read it);
2. sends a **test e-mail** so you immediately know SMTP works;
3. enables **linger** so the service keeps running when you log out and starts
   again after a reboot;
4. installs and starts the systemd service `midea-finder-loop.service`.

**Check your inbox for the test e-mail.** If it arrived, everything is wired up
correctly and the watcher is already running.

If the script can't enable linger automatically, it will print one command to
run with `sudo` — run it, that's all.

---

## 5. Confirm it's running

```bash
systemctl --user status midea-finder-loop.service
```

You want to see **`active (running)`**. To watch it work in real time:

```bash
journalctl --user -u midea-finder-loop.service -f
```

Every cycle prints one line per product, e.g.:

```
🔴 OBI       Midea mobile Split-Klimaanlage PortaSplit     -> out_of_stock  [JSON-LD availability=...outofstock]
🟢 toom      Midea Mobiles Klimagerät PortaSplit 12000 BTU -> in_stock      [JSON-LD availability=...instock]
```

When something flips from 🔴 to 🟢 you'll get the e-mail. Press `Ctrl+C` to stop
following the log (this does **not** stop the service).

---

## 6. Day-to-day management

```bash
# after editing config.json (e.g. new products / different interval):
systemctl --user restart midea-finder-loop.service

# pause it temporarily:
systemctl --user stop midea-finder-loop.service

# start again:
systemctl --user start midea-finder-loop.service

# stop permanently and remove from autostart:
systemctl --user disable --now midea-finder-loop.service
```

To **update** to a newer version later:

```bash
cd ~/midea_finder
git pull
systemctl --user restart midea-finder-loop.service
```

---

## 7. Troubleshooting

| Symptom | Fix |
| --- | --- |
| No test e-mail arrived | Re-check step 2 (POP3/IMAP enabled) and your login/password. Re-run `./install-linux.sh` or test directly: `set -a; source ~/.config/midea-finder.env; set +a; python3 midea_finder.py --test-email` |
| `Authentication failed` in logs | Wrong password, or 2FA is on → create a GMX **app password** and update `~/.config/midea-finder.env` |
| A product always shows `unknown` | That shop blocked the plain request, or changed its page. The run continues; other products still work. |
| Service not running after reboot | Make sure linger is on: `loginctl enable-linger $USER`, then `systemctl --user enable --now midea-finder-loop.service` |
| Laptop only checks while awake | A sleeping/suspended laptop pauses the timer. Use a machine that stays awake, or disable sleep. |

---

## How it decides "back in stock"

For each product page it reads the shop's **schema.org availability data**
(`InStock` / `OutOfStock`) and, as a fallback, scans the visible German text
("In den Warenkorb" / "nicht verfügbar" …). It remembers the previous status in
`state.json` and e-mails you **only on an out-of-stock → in-stock transition**,
so you won't get repeat mails while the item stays available.
