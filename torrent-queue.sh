#!/usr/bin/env bash
# torrent-queue.sh — daemon started by torrent-dl.sh
# Watches ~/temp/torrents/ for new .torrent files and adds them to
# transmission-daemon. Exits automatically after 2h idle.
#
# Requires: transmission-cli (transmission-remote, transmission-daemon)

TORRENT_TEMP="${HOME}/temp/h.temp/torrents"
DOWNLOAD_DIR="${HOME}/temp/h.temp"
PID_FILE="/tmp/torrent-queue.pid"
ADDED_LOG="/tmp/torrent-queue-added.txt"  # tracks files already submitted
IDLE_TIMEOUT=7200   # seconds (2h) with no new torrents before exit

mkdir -p "$TORRENT_TEMP" "$DOWNLOAD_DIR"
echo $$ > "$PID_FILE"
touch "$ADDED_LOG"

notify() { command -v notify-send &>/dev/null && notify-send "torrent-queue" "$1" || echo "torrent-queue: $1" >&2; }

cleanup() { rm -f "$PID_FILE"; exit 0; }
trap cleanup EXIT INT TERM

# ── Ensure transmission-daemon is running ─────────────────────────────────
ensure_daemon() {
    if ! pgrep -x transmission-da > /dev/null; then
        transmission-daemon --download-dir "$DOWNLOAD_DIR"
        local retries=10
        while (( retries-- > 0 )); do
            sleep 1
            transmission-remote --list &>/dev/null && break
        done
    fi
}

# ── Add a single torrent file (idempotent via ADDED_LOG) ──────────────────
add_torrent() {
    local torrent_file="$1"
    local basename
    basename=$(basename "$torrent_file")

    # Skip if already submitted this session
    if grep -qxF "$basename" "$ADDED_LOG" 2>/dev/null; then
        return
    fi

    ensure_daemon
    if transmission-remote --add "$torrent_file" --download-dir "$DOWNLOAD_DIR" &>/dev/null; then
        echo "$basename" >> "$ADDED_LOG"
        notify "Added: $basename"
    else
        notify "Failed to add: $basename"
    fi
}

# ── Main watch loop ───────────────────────────────────────────────────────
LAST_ACTIVITY=$(date +%s)

while true; do
    ADDED_ANY=false

    while IFS= read -r -d '' f; do
        add_torrent "$f"
        ADDED_ANY=true
        LAST_ACTIVITY=$(date +%s)
    done < <(find "$TORRENT_TEMP" -maxdepth 1 -name "*.torrent" -print0 2>/dev/null)

    # Auto-exit after idle timeout
    if (( $(date +%s) - LAST_ACTIVITY > IDLE_TIMEOUT )); then
        exit 0
    fi

    sleep 3
done
