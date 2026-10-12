#!/usr/bin/env bash
# Instala DeckSpace para el usuario actual: acceso directo en el menú (y en el Escritorio con --desktop).
# No toca el sistema (SteamOS tiene la raíz de solo lectura): todo queda en tu carpeta personal.
#   ./install.sh              instalar o actualizar el acceso directo
#   ./install.sh --desktop    además, copia del acceso directo en ~/Desktop
#   ./install.sh --uninstall  quitar los accesos directos (no borra esta carpeta, la caché ni ~/Backups/DeckSpace)
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ENTRY="$APPS/deckspace.desktop"
DESKTOP="$HOME/Desktop/deckspace.desktop"

if [[ "${1:-}" == "--uninstall" ]]; then
    rm -f "$ENTRY" "$DESKTOP"
    echo "Accesos directos quitados. Puedes borrar $DIR y ~/.cache/DeckSpace si ya no los quieres."
    echo "Las partidas copiadas antes de limpiar siguen en ~/Backups/DeckSpace."
    exit 0
fi

# Requisitos: Python 3 con Tkinter y du (vienen con SteamOS). Steam hace falta para analizar los juegos.
if ! python3 -c "import tkinter" 2>/dev/null; then
    echo "Error: hace falta Python 3 con Tkinter." >&2
    exit 1
fi
command -v du >/dev/null || { echo "Error: hace falta du (coreutils)." >&2; exit 1; }
[[ -d "$HOME/.local/share/Steam" || -d "$HOME/.steam/steam" ]] \
    || echo "Aviso: no encuentro Steam; DeckSpace solo podrá mostrar las carpetas."
command -v xdg-open >/dev/null || echo "Aviso: no está xdg-open; «Abrir carpeta» no funcionará."

chmod +x "$DIR/deckspace.py"
mkdir -p "$APPS"
cat > "$ENTRY" <<EOF
[Desktop Entry]
Type=Application
Name=DeckSpace
Comment=Espacio en disco de la Deck y limpieza de restos
Exec=/usr/bin/env python3 "$DIR/deckspace.py"
Icon=drive-harddisk
Terminal=false
Categories=Utility;System;
EOF
chmod +x "$ENTRY"
echo "Acceso directo creado: $ENTRY"

if [[ "${1:-}" == "--desktop" ]]; then
    mkdir -p "$HOME/Desktop"
    cp "$ENTRY" "$DESKTOP"
    chmod +x "$DESKTOP"
    echo "Copia en el Escritorio: $DESKTOP"
fi
command -v update-desktop-database >/dev/null && update-desktop-database "$APPS" 2>/dev/null || true
echo "Listo. Abre DeckSpace desde el menú de aplicaciones (Utilidades o Sistema)."
