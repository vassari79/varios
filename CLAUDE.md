# varios — Personal Scripts (Arch Linux / Wayland)

## What it is
Collection of shell scripts for automating torrent downloads and image-to-PDF conversion on an Arch Linux + labwc (wlroots Wayland) system. Source of truth is `~/bin/`; this repo is the git-tracked mirror.

## GitHub
`vassari79/varios` (public) — SSH remote

---

## System Context

| Item | Value |
|---|---|
| OS | Arch Linux |
| Compositor | labwc (wlroots-based Wayland, **not** X11) |
| Firefox | `firefox-nightly`, running **native Wayland** (`app_id: firefox-nightly`) |
| ydotool | `/usr/bin/ydotool`, daemon socket at `/run/user/1000/.ydotool_socket` |
| wlrctl | Used to focus Wayland windows by `app_id` |
| Clipboard | `wl-paste --no-newline` (wl-clipboard) |
| Torrent client | `transmission-daemon` + `transmission-remote`, web UI at `http://localhost:9091` |
| Keybind | Super+D → `/home/vassari/bin/torrent-dl.sh` (set in `~/.config/labwc/rc.xml`) |

### Key codes (ydotool, from `/usr/include/linux/input-event-codes.h`)
`ESC=1`, `LEFTCTRL=29`, `A=30`, `L=38`, `C=46`  
Format: `code:1` = press, `code:0` = release

---

## Scripts

### `torrent-dl.sh` — keybind trigger (fast, non-blocking)
**Location:** `~/bin/torrent-dl.sh`  
**Trigger:** Super+D via labwc keybind

**Flow:**
1. `sleep 0.3` — let labwc finish dispatching the keybind
2. `wlrctl window focus app_id:firefox-nightly` — focus Firefox explicitly (required since it's native Wayland, not seen by xdotool)
3. `ydotool key` — send Ctrl+L, Ctrl+A, Ctrl+C to copy the address bar URL
4. `wl-paste` — read URL from clipboard
5. Open `{URL}/download` in firefox-nightly (already logged in to whatever tracker site)
6. Create a **timestamp marker** (`mktemp`) — used to find only torrents that appeared *after* this invocation (race-safe for multiple rapid keybind presses)
7. Poll `~/Downloads` for `*.torrent` newer than marker (max 30s)
8. Move new torrents to `~/temp/torrents/`
9. Start `torrent-queue.sh` daemon if not already running

**Race condition solution:** Uses `-newer $MARKER` (mktemp timestamp) instead of `-mmin -1`. Each invocation creates its own marker, so concurrent runs each capture only their own torrent file.

**Paths:**
- `TORRENT_TEMP=~/temp/torrents`
- `QUEUE_DAEMON=~/bin/torrent-queue.sh`
- Log: `/tmp/torrent-queue.log`

---

### `torrent-queue.sh` — background daemon
**Location:** `~/bin/torrent-queue.sh`  
**Started by:** `torrent-dl.sh` (via `nohup ... &`) if not already running

**Flow:**
1. Writes PID to `/tmp/torrent-queue.pid`
2. Loop every 3 seconds: scans `~/temp/torrents/*.torrent`
3. For each new file: ensures `transmission-daemon` is running, then calls `transmission-remote --add`
4. Tracks added files in `/tmp/torrent-queue-added.txt` (idempotent — won't re-add same file)
5. Auto-exits after **2 hours** of no new torrents

**Paths:**
- `TORRENT_TEMP=~/temp/torrents`
- `DOWNLOAD_DIR=~/temp/h.temp`
- `PID_FILE=/tmp/torrent-queue.pid`
- `ADDED_LOG=/tmp/torrent-queue-added.txt`

---

### `image-to-pdf.sh` — parallel image → PDF converter
**Location:** `~/bin/image-to-pdf.sh`  
**Run:** manually after all torrents finish downloading

**Flow:**
1. Scans subdirectories of `~/temp/h.temp/` (excluding `.processing/`)
2. For each subdirectory: moves it to `~/temp/h.temp/.processing/` (buffer, prevents partial runs)
3. Converts all `jpg/jpeg/png/webp` images to PDF with `convert -density 300` — up to **6 parallel jobs** (`xargs -P 6`)
4. Merges all PDFs with `pdftk` into a single `{name}.pdf` in `~/temp/h.temp/`
5. Cleans up the buffer directory

**Short name function:** directory names longer than 35 chars are truncated to `{first15}…{last20}` for the output PDF filename.

**Dependencies:** `imagemagick` (`convert`), `pdftk`

---

### `firefox-active-url.py` — session file URL reader
**Location:** `~/bin/firefox-active-url.py`  
**Status:** working but **not used in the main flow** (session file can be stale)

Reads the active tab URL from Firefox's `recovery.jsonlz4` session file.  
Requires: `python-lz4` (`pacman -S python-lz4`)  
Key fix: `lz4.block.decompress(data, uncompressed_size=-1)` — reads size from header instead of hardcoding.

---

## Debugging Notes

| Problem | Cause | Fix |
|---|---|---|
| `xdotool` can't find Firefox | Firefox runs native Wayland, not XWayland | Use `wlrctl window focus app_id:firefox-nightly` |
| ydotool has no socket | labwc doesn't inherit user env | Set `YDOTOOL_SOCKET=/run/user/1000/.ydotool_socket` explicitly in script |
| URL read gives page content, not address | ydotool fires before Firefox is focused | `wlrctl focus` + `sleep 0.3` before ydotool keys |
| Multiple rapid Super+D triggers race for same torrent | `-mmin -1` matches all recent files | `mktemp` marker + `-newer $MARKER` isolates each invocation |
| lz4 decompression fails with size error | Hardcoded `uncompressed_size` too small | Use `uncompressed_size=-1` to read size from data header |

## Workflow
```
Super+D (labwc keybind)
  └─ torrent-dl.sh         ← reads URL, triggers Firefox /download, moves .torrent
       └─ torrent-queue.sh ← daemon: adds .torrent files to transmission-daemon
                                     web UI: http://localhost:9091
                                     downloads to: ~/temp/h.temp/

[manually, after downloads finish]
  └─ image-to-pdf.sh       ← converts images in ~/temp/h.temp/ subdirs to merged PDFs
```

## To apply labwc keybind changes
```bash
labwc --reconfigure
```
