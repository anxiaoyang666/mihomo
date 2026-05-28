# Mihomo Toolbox

Mihomo Web panel and helper scripts for a gateway-style Mihomo service.

## Online Panel Upgrade

New panel versions can upgrade themselves from this repository. The panel reads these optional values from `/etc/mihomo/.env`:

```bash
MIHOMO_PANEL_REPO_URL="https://github.com/anxiaoyang666/mihomo.git"
MIHOMO_PANEL_BRANCH="main"
```

Panel upgrades preserve local runtime state:

- `/etc/mihomo/.env`
- `/etc/mihomo/config.yaml`
- `/etc/mihomo/ui`

## One-Time Upgrade For Old Panels

Old installations that do not yet have the online upgrade button can be upgraded once over SSH:

```bash
bash -c 'set -e; TMP=$(mktemp -d); mkdir -p /etc/mihomo/backup; curl -fL -o "$TMP/mihomo.zip" "https://gh-proxy.com/https://github.com/anxiaoyang666/mihomo/archive/refs/heads/main.zip"; python3 - <<PY "$TMP/mihomo.zip" "$TMP"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
SRC=$(find "$TMP" -maxdepth 3 -type d -name remote-root | head -n1); test -n "$SRC"; BACKUP="/etc/mihomo/backup/manager.before-upgrade.$(date +%Y%m%d%H%M%S)"; [ -d /etc/mihomo/manager ] && cp -a /etc/mihomo/manager "$BACKUP" || true; install -m 0755 "$SRC/usr/bin/mihomo" /usr/bin/mihomo; rm -rf /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; mkdir -p /etc/mihomo/manager /etc/mihomo/scripts /etc/mihomo/templates; cp -a "$SRC/etc/mihomo/manager/." /etc/mihomo/manager/; cp -a "$SRC/etc/mihomo/scripts/." /etc/mihomo/scripts/; cp -a "$SRC/etc/mihomo/templates/." /etc/mihomo/templates/; install -m 0644 "$SRC/etc/systemd/system/mihomo.service" /etc/systemd/system/mihomo.service; install -m 0644 "$SRC/etc/systemd/system/mihomo-manager.service" /etc/systemd/system/mihomo-manager.service; install -m 0644 "$SRC/etc/systemd/system/force-ip-forward.service" /etc/systemd/system/force-ip-forward.service; systemctl daemon-reload; systemctl restart mihomo-manager; rm -rf "$TMP"; echo "Mihomo panel upgraded. Backup: $BACKUP"'
```

Run the command inside the host/container where `/etc/mihomo` and `mihomo-manager` are installed.

## Layout

- `remote-root/`: files copied into their absolute locations on install/upgrade.
- `tests/`: contract tests for panel upgrade behavior.

## Local Files Not Published

The repository intentionally ignores live secrets, subscriptions, provider cache, runtime config, extraction inventory, and archives.
