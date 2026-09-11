#!/usr/bin/env bash
# Instala ZenMonitor para el usuario actual, dentro de ~/.local. No toca el sistema
# ni pide root. Las rutas se generan aqui, por eso el repo no lleva ninguna fija.
set -euo pipefail

DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
CONF="${XDG_CONFIG_HOME:-$HOME/.config}"
SHARE="$DATA/zenmonitor"
BIN="$HOME/.local/bin"
ORIGEN="$(cd "$(dirname "$0")" && pwd)"

autostart=0
f12=0
for arg in "$@"; do
    case "$arg" in
        --autostart) autostart=1 ;;
        --f12)       f12=1 ;;
        -h|--help)
            echo "uso: $0 [--autostart] [--f12]"
            echo "  --autostart  arranca con la sesion, en segundo plano (solo bandeja)"
            echo "  --f12        ata la tecla F12 (logo Zen) para abrir la ventana"
            exit 0 ;;
        *) echo "opcion desconocida: $arg" >&2; exit 1 ;;
    esac
done

command -v python3 >/dev/null || { echo "hace falta python3" >&2; exit 1; }
python3 -c "import PyQt6.QtWidgets, PyQt6.QtNetwork" 2>/dev/null || {
    echo "hace falta PyQt6 (Arch: pacman -S python-pyqt6)" >&2; exit 1; }

install -d "$SHARE" "$BIN" "$DATA/applications"
install -m 644 "$ORIGEN/zenmonitor.py" "$SHARE/zenmonitor.py"

# El icono se dibuja desde el vector que ya vive en el codigo, asi no hace falta
# guardar ningun binario en el repositorio.
QT_QPA_PLATFORM=offscreen python3 - "$SHARE" <<'PY'
import sys
from PyQt6.QtWidgets import QApplication
sys.path.insert(0, sys.argv[1])
app = QApplication([])
import zenmonitor as z
z.asus_icon(z.BLUE, 256).pixmap(256, 256).save(sys.argv[1] + "/zenmonitor.png")
PY
rm -rf "$SHARE/__pycache__"

cat > "$BIN/zenmonitor" <<LANZADOR
#!/bin/sh
exec python3 "$SHARE/zenmonitor.py" "\$@"
LANZADOR
chmod +x "$BIN/zenmonitor"

cat > "$DATA/applications/zenmonitor.desktop" <<ESCRITORIO
[Desktop Entry]
Type=Application
Name=ZenMonitor
GenericName=Monitor del portatil
Comment=Ventilador, carga y energia del Zenbook
Exec=$BIN/zenmonitor
Icon=$SHARE/zenmonitor.png
Terminal=false
Categories=System;Monitor;HardwareSettings;
Keywords=ventilador;fan;rpm;temperatura;perfil;zenmonitor;
StartupNotify=true
ESCRITORIO

# KWin no deja que una ventana ya abierta se traiga al frente sola: al pedir
# activacion sin un token de Wayland la marca como que "reclama atencion" (el
# parpadeo naranja en la barra de tareas) en vez de levantarla. Una regla de
# ventana que desactive esa proteccion solo para ZenMonitor lo arregla.
REGLA_ID="4d0a1b6e-5c2f-4a71-9f3b-7e1c8a2d6f40"
anadir_regla_kwin() {
    command -v kwriteconfig6 >/dev/null || return 0
    local previas
    previas="$(kreadconfig6 --file kwinrulesrc --group General --key rules 2>/dev/null || true)"
    case ",$previas," in
        *",$REGLA_ID,"*) return 0 ;;        # ya estaba, no duplicar
    esac
    kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key Description \
        "ZenMonitor: dejar que se traiga al frente sola"
    kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key wmclass zenmonitor
    kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key wmclassmatch 1
    kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key fsplevel 0
    kwriteconfig6 --file kwinrulesrc --group "$REGLA_ID" --key fsplevelrule 2
    kwriteconfig6 --file kwinrulesrc --group General --key rules \
        "${previas:+$previas,}$REGLA_ID"
    kwriteconfig6 --file kwinrulesrc --group General --key count \
        "$(( $(printf '%s' "${previas:+$previas,}$REGLA_ID" | tr ',' '\n' | grep -c .) ))"
    gdbus call --session --dest org.kde.KWin --object-path /KWin \
        --method org.kde.KWin.reconfigure >/dev/null 2>&1 || true
    echo "regla de KWin anadida (la ventana ya se trae al frente sola)"
}
anadir_regla_kwin

if [ "$autostart" = 1 ]; then
    install -d "$CONF/autostart"
    cat > "$CONF/autostart/zenmonitor.desktop" <<AUTO
[Desktop Entry]
Type=Application
Name=ZenMonitor
Comment=Arranca en segundo plano, solo con el icono en la bandeja
Exec=$BIN/zenmonitor --tray
Icon=$SHARE/zenmonitor.png
Terminal=false
StartupNotify=false
X-GNOME-Autostart-enabled=true
AUTO
    echo "autoarranque activado (modo bandeja)"
fi

if [ "$f12" = 1 ]; then
    id -nG | tr ' ' '\n' | grep -qx input || {
        echo "AVISO: tu usuario no esta en el grupo 'input', el listener no podra"
        echo "       leer /dev/input. Arreglalo con: sudo usermod -aG input \$USER"; }
    install -m 755 "$ORIGEN/extras/asus-f12-listener" "$BIN/asus-f12-listener"
    install -d "$CONF/systemd/user"
    install -m 644 "$ORIGEN/extras/asus-f12-zenmonitor.service" \
        "$CONF/systemd/user/asus-f12-zenmonitor.service"
    systemctl --user daemon-reload
    systemctl --user enable --now asus-f12-zenmonitor.service
    echo "tecla F12 atada (servicio asus-f12-zenmonitor)"
fi

command -v update-desktop-database >/dev/null && \
    update-desktop-database "$DATA/applications" 2>/dev/null || true

echo "instalado. Arrancalo con: $BIN/zenmonitor"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) echo "ojo: $BIN no esta en tu PATH" ;;
esac
