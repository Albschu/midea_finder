#!/usr/bin/env bash
#
# One-shot installer: runs midea_finder permanently on your own Linux machine
# as a systemd *user* service that
#   - starts automatically on boot,
#   - restarts automatically if it crashes,
#   - keeps running even when you are logged out (linger).
#
# Usage:
#   ./install-linux.sh
#
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$HOME/.config/midea-finder.env"
UNIT_SRC="$REPO_DIR/systemd/midea-finder-loop.service"
UNIT_DST_DIR="$HOME/.config/systemd/user"
UNIT_DST="$UNIT_DST_DIR/midea-finder-loop.service"

echo "==> midea_finder is in: $REPO_DIR"

# 1. config.json
if [[ ! -f "$REPO_DIR/config.json" ]]; then
    cp "$REPO_DIR/config.example.json" "$REPO_DIR/config.json"
    echo "==> created config.json from the template (edit it to add/remove products)"
fi

# 2. SMTP credentials -> 0600 env file
if [[ ! -f "$ENV_FILE" ]]; then
    echo "==> SMTP credentials for sending the alert e-mail:"
    read -rp "    GMX login (e-mail): " SMTP_USER
    read -rsp "    GMX password / app-password: " SMTP_PASS; echo
    mkdir -p "$(dirname "$ENV_FILE")"
    umask 077
    cat > "$ENV_FILE" <<EOF
MIDEA_SMTP_USER=$SMTP_USER
MIDEA_SMTP_PASS=$SMTP_PASS
EOF
    chmod 600 "$ENV_FILE"
    echo "==> wrote $ENV_FILE (permissions 600)"
else
    echo "==> $ENV_FILE already exists, leaving it untouched"
fi

# 3. adjust WorkingDirectory/ExecStart if the repo is not in ~/midea_finder
mkdir -p "$UNIT_DST_DIR"
sed "s|%h/midea_finder|$REPO_DIR|g" "$UNIT_SRC" > "$UNIT_DST"
echo "==> installed unit: $UNIT_DST"

# 4. send a test e-mail before enabling, so config problems surface now
echo "==> sending a test e-mail to verify SMTP ..."
set -a; # shellcheck disable=SC1090
source "$ENV_FILE"; set +a
if python3 "$REPO_DIR/midea_finder.py" --test-email; then
    echo "==> test e-mail sent OK"
else
    echo "!! test e-mail failed - fix config/credentials, then re-run this script" >&2
    exit 1
fi

# 5. enable linger so the service survives logout / runs at boot
loginctl enable-linger "$USER" 2>/dev/null || \
    echo "   (could not enable linger automatically - run: sudo loginctl enable-linger $USER)"

# 6. start it
systemctl --user daemon-reload
systemctl --user enable --now midea-finder-loop.service

echo
echo "==> Done. The watcher is running and will start on every boot."
echo "    Status :  systemctl --user status midea-finder-loop.service"
echo "    Logs   :  journalctl --user -u midea-finder-loop.service -f"
echo "    Stop   :  systemctl --user disable --now midea-finder-loop.service"
