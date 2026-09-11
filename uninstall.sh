#!/usr/bin/env bash
# Deja el sistema como estaba: borra todo lo que pone install.sh.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
BIN="$HOME/.local/bin"

pkill -f "zenmonitor/[z]enmonitor\.py" 2>/dev/null || true

if systemctl --user list-unit-files asus-f12-zenmonitor.service >/dev/null 2>&1; then
    systemctl --user disable --now asus-f12-zenmonitor.service 2>/dev/null || true
fi

rm -rf "$DATA/zenmonitor"
rm -f  "$BIN/zenmonitor" "$BIN/asus-f12-listener"
rm -f  "$DATA/applications/zenmonitor.desktop"
rm -f  "$CONF/autostart/zenmonitor.desktop"
rm -f  "$CONF/systemd/user/asus-f12-zenmonitor.service"
rm -f  "/tmp/zenmonitor-$(id -u)"
systemctl --user daemon-reload 2>/dev/null || true
echo "desinstalado"
