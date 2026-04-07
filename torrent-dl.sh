#!/usr/bin/env bash
# torrent-dl.sh — triggered by Super+D keybind
# Gets current Firefox tab URL, triggers /download, moves .torrent to queue folder.
# torrent-queue.sh handles adding to transmission-daemon.
#
# Requires: ydotool (ydotoold), wlrctl, wl-clipboard, notify-send

set -euo pipefail

TORRENT_TEMP="${HOME}/temp/h.temp/torrents"
QUEUE_DAEMON="${HOME}/bin/torrent-queue.sh"

mkdir -p "$TORRENT_TEMP"

notify() { command -v notify-send &>/dev/null && notify-send "torrent-dl" "$1" || echo "torrent-dl: $1" >&2; }

export YDOTOOL_SOCKET="/run/user/1000/.ydotool_socket"

# ── 1. Get current tab URL ─────────────────────────────────────────────────
sleep 0.3   # let labwc finish keybind dispatch
wlrctl window focus app_id:firefox-nightly
sleep 0.3
# Key codes: ESC=1, LEFTCTRL=29, A=30, L=38, C=46
ydotool key -d 30 29:1 38:1 38:0 29:0   # Ctrl+L
sleep 0.3
ydotool key -d 30 29:1 30:1 30:0 29:0   # Ctrl+A
sleep 0.15
ydotool key -d 30 29:1 46:1 46:0 29:0   # Ctrl+C
sleep 0.4
ydotool key -d 30 1:1 1:0               # Escape
sleep 0.1

PAGE_URL=$(wl-paste --no-newline 2>/dev/null || true)
if [[ -z "$PAGE_URL" || "$PAGE_URL" != http* ]]; then
    notify "Could not read tab URL (got: ${PAGE_URL:0:40})"
    exit 1
fi

# ── 2. Open download URL in Firefox ───────────────────────────────────────
TORRENT_URL="${PAGE_URL%/}/download"
# Timestamp marker: only pick up torrents that appear AFTER this point
MARKER=$(mktemp /tmp/torrent-dl-marker.XXXXXX)
firefox-nightly "$TORRENT_URL" &

# ── 3. Poll ~/Downloads for new .torrent (race-safe via timestamp) ────────
DEADLINE=$(( $(date +%s) + 30 ))
FOUND=""
while [[ $(date +%s) -lt $DEADLINE ]]; do
    FOUND=$(find "${HOME}/Downloads" -maxdepth 2 -name "*.torrent" -newer "$MARKER" 2>/dev/null | head -1)
    [[ -n "$FOUND" ]] && break
    sleep 1
done

if [[ -z "$FOUND" ]]; then
    rm -f "$MARKER"
    notify "No .torrent appeared in ~/Downloads after 30s"
    exit 1
fi

# Move all new torrents to queue folder (skip if already there)
find "${HOME}/Downloads" -maxdepth 2 -name "*.torrent" -newer "$MARKER" \
    -exec mv -n {} "${TORRENT_TEMP}/" \; 2>/dev/null || true
rm -f "$MARKER"

notify "Queued: $(basename "$FOUND")"

# ── 4. Ensure queue daemon is running ─────────────────────────────────────
if ! pgrep -f torrent-queue.sh > /dev/null; then
    nohup bash "$QUEUE_DAEMON" > /tmp/torrent-queue.log 2>&1 &
fi
