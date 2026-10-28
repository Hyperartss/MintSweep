#!/usr/bin/env bash
# Install MintSweep for the current user only (no sudo, no system files touched).
#   ./install.sh             install
#   ./install.sh --uninstall remove the shortcut and launcher
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN_DIR="$HOME/.local/bin"
APP_MENU_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
DESKTOP_FILE="$APP_MENU_DIR/mintsweep.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$DESKTOP_FILE" "$BIN_DIR/mintsweep" "$ICON_DIR/mintsweep.svg"
    echo "MintSweep shortcuts removed. The project folder itself is untouched."
    exit 0
fi

if ! python3 -c "import gi; gi.require_version('Gtk','3.0')" >/dev/null 2>&1; then
    echo "Python GTK 3 bindings are missing. Install them with:" >&2
    echo "    sudo apt install python3-gi gir1.2-gtk-3.0" >&2
    exit 1
fi

chmod +x "$APP_DIR/bin/mintsweep"
mkdir -p "$BIN_DIR" "$APP_MENU_DIR" "$ICON_DIR"
ln -sf "$APP_DIR/bin/mintsweep" "$BIN_DIR/mintsweep"
cp "$APP_DIR/assets/mintsweep.svg" "$ICON_DIR/mintsweep.svg"

cat > "$DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Version=1.0
Name=MintSweep
GenericName=Storage and Memory Cleaner
Comment=Choose exactly what to clean up on Linux Mint
Exec=$BIN_DIR/mintsweep
Icon=mintsweep
Terminal=false
Categories=System;Settings;Utility;
Keywords=clean;cleaner;disk;storage;memory;ram;junk;cache;space;
StartupNotify=true
EOF

chmod +x "$DESKTOP_FILE"
if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APP_MENU_DIR" >/dev/null 2>&1 || true
fi
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
    gtk-update-icon-cache -q "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true
fi

echo "Installed. Open MintSweep from the application menu, or run: mintsweep"
echo "(If 'mintsweep' is not found, add $BIN_DIR to your PATH.)"
