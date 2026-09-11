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

# quita la regla de KWin, dejando intactas las demas
REGLA_ID="4d0a1b6e-5c2f-4a71-9f3b-7e1c8a2d6f40"
if command -v kwriteconfig6 >/dev/null; then
    previas="$(kreadconfig6 --file kwinrulesrc --group General --key rules 2>/dev/null || true)"
    case ",$previas," in
        *",$REGLA_ID,"*)
            quedan="$(printf '%s' "$previas" | tr ',' '\n' | grep -vx "$REGLA_ID" \
                      | paste -sd, -)"
            kwriteconfig6 --file kwinrulesrc --group General --key rules "$quedan"
            kwriteconfig6 --file kwinrulesrc --group General --key count \
                "$(printf '%s' "$quedan" | tr ',' '\n' | grep -c . || true)"
            # kwriteconfig6 no borra grupos: se vacia clave a clave y KConfig
            # se lleva por delante el grupo vacio al guardar
            for clave in Description wmclass wmclassmatch fsplevel fsplevelrule; do
                kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key "$clave" --delete
            done
            gdbus call --session --dest org.kde.KWin --object-path /KWin \
                --method org.kde.KWin.reconfigure >/dev/null 2>&1 || true
            ;;
    esac
fi
systemctl --user daemon-reload 2>/dev/null || true
echo "desinstalado"
