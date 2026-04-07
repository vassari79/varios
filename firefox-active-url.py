#!/usr/bin/env python3
"""Print the URL of the currently active Firefox Nightly tab."""
import sys, json, glob, lz4.block

patterns = [
    "/home/vassari/.mozilla/firefox/*nightly*/sessionstore-backups/recovery.jsonlz4",
    "/home/vassari/.mozilla/firefox/*/sessionstore-backups/recovery.jsonlz4",
]

session_file = None
for pat in patterns:
    matches = glob.glob(pat)
    if matches:
        session_file = matches[0]
        break

if not session_file:
    print("ERROR: session file not found", file=sys.stderr)
    sys.exit(1)

try:
    with open(session_file, 'rb') as f:
        magic = f.read(8)
        if magic != b'mozLz40\x00':
            print(f"ERROR: unexpected magic {magic!r}", file=sys.stderr)
            sys.exit(1)
        data = lz4.block.decompress(f.read(), uncompressed_size=-1)
except Exception as e:
    print(f"ERROR: {e}", file=sys.stderr)
    sys.exit(1)

session = json.loads(data)
for win in session.get('windows', []):
    sel = win.get('selected', 1) - 1
    tabs = win.get('tabs', [])
    if 0 <= sel < len(tabs):
        tab = tabs[sel]
        idx = tab.get('index', len(tab.get('entries', []))) - 1
        entries = tab.get('entries', [])
        if 0 <= idx < len(entries):
            url = entries[idx].get('url', '')
            print(url)
            sys.exit(0)

print("ERROR: could not find active tab", file=sys.stderr)
sys.exit(1)
