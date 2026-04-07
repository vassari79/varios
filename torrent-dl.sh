#!/usr/bin/env bash
# torrent-dl.sh
# Keyboard-shortcut script:
#   1. Gets the active Firefox Nightly tab URL (from session file — no browser interaction)
#   2. Fetches the page and extracts the <a id="download"> link
#   3. Downloads the file to TORRENT_TEMP
#   4. Moves other recent .torrent files from ~/Downloads (last 12 h)
#   5. Opens all collected torrents in transmission-qt → DOWNLOAD_DIR
#   6. After transmission-qt closes, runs image-to-pdf.sh
#
# Requires: ydotool (ydotoold running), wlrctl, wl-clipboard, python3 python-lz4, curl, transmission-qt, notify-send

set -euo pipefail

TORRENT_TEMP="${HOME}/temp/torrents"
DOWNLOAD_DIR="${HOME}/temp/h.temp"
IMG2PDF_SCRIPT="${HOME}/bin/image-to-pdf.sh"

mkdir -p "$TORRENT_TEMP" "$DOWNLOAD_DIR"

notify() { command -v notify-send &>/dev/null && notify-send "torrent-dl" "$1" || echo "torrent-dl: $1" >&2; }

# ── 1. Get current Firefox Nightly tab URL via ydotool ─────────────────────
# Socket path for ydotoold (systemd user service)
export YDOTOOL_SOCKET="/run/user/1000/.ydotool_socket"

sleep 0.3   # wait for labwc to finish keybind dispatch

# Explicitly focus Firefox Nightly via wlrctl (wlroots compositor API)
wlrctl window focus app_id:firefox-nightly
sleep 0.3   # wait for focus to land

# Key codes: ESC=1, LEFTCTRL=29, A=30, L=38, C=46
ydotool key -d 30 29:1 38:1 38:0 29:0   # Ctrl+L — focus address bar
sleep 0.3
ydotool key -d 30 29:1 30:1 30:0 29:0   # Ctrl+A — select all
sleep 0.15
ydotool key -d 30 29:1 46:1 46:0 29:0   # Ctrl+C — copy to clipboard
sleep 0.4
ydotool key -d 30 1:1 1:0               # Escape
sleep 0.1

PAGE_URL=$(wl-paste --no-newline 2>/dev/null || true)

if [[ -z "$PAGE_URL" || "$PAGE_URL" != http* ]]; then
    notify "Could not read tab URL (got: ${PAGE_URL:0:40})"
    exit 1
fi

# ── 2. Open download URL in Firefox (already logged in) ──────────────────────
TORRENT_URL="${PAGE_URL%/}/download"
firefox-nightly "$TORRENT_URL" &

# ── 3. Move newest .torrent from ~/Downloads ──────────────────────────────────
# Poll until a new .torrent appears (max 30s)
DEADLINE=$(( $(date +%s) + 30 ))
FOUND=""
while [[ $(date +%s) -lt $DEADLINE ]]; do
    FOUND=$(find "${HOME}/Downloads" -maxdepth 2 -name "*.torrent" -mmin -1 2>/dev/null | head -1)
    [[ -n "$FOUND" ]] && break
    sleep 1
done

if [[ -z "$FOUND" ]]; then
    notify "No .torrent file appeared in ~/Downloads after 30s"
    exit 1
fi

find "${HOME}/Downloads" -maxdepth 2 -name "*.torrent" -mmin -1 \
    -exec mv -n {} "${TORRENT_TEMP}/" \; 2>/dev/null || true

# ── 5. Open all torrents in transmission-qt ─────────────────────────────────
mapfile -d '' -t TORRENTS < <(find "$TORRENT_TEMP" -maxdepth 1 -name "*.torrent" -print0)

if [[ ${#TORRENTS[@]} -eq 0 ]]; then
    notify "No .torrent files in ${TORRENT_TEMP}"
    exit 0
fi

notify "Adding ${#TORRENTS[@]} torrent(s) to transmission-daemon"
# Start daemon if not running
if ! pgrep -x transmission-da > /dev/null; then
    transmission-daemon --download-dir "$DOWNLOAD_DIR"
    sleep 2  # wait for daemon to be ready
fi
for t in "${TORRENTS[@]}"; do
    transmission-remote --add "$t" --download-dir "$DOWNLOAD_DIR"
done
notify "Torrents added — status at http://localhost:9091"

# ── 5. After downloads complete, run image-to-pdf manually ─────────────────────
# Run ~/bin/image-to-pdf.sh once all torrents are done in the web UI.
