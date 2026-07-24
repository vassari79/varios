#!/usr/bin/env python3
"""Collector for the ESP32 phone-presence sensors (runs in Termux on your phone).

Two ESP32 boards sniff 2.4 GHz WiFi/BLE and POST what they hear here every ~2 s.
This keeps a baseline (ambient MACs learned in the empty room), correlates the
two boards to zone each device to a corner, detects short traffic "episodes"
(a phone used for a moment), and shows it all in a live console table and a web
page. You can mark a MAC W (watch) or D (disregard) from the web page.

Setup in Termux:
    pkg install python termux-api
    pip install flask
    termux-wake-lock
    python collector.py
(termux-notification also needs the Termux:API app installed from F-Droid.)

Keys (type the letter + Enter in the console):
    b baseline (empty room) | l live | i idle | s save baseline | c clear | +/- threshold | q quit
"""

import json
import os
import subprocess
import sys
import threading
import time
from collections import deque

# ---------------- Config ----------------
HOST, PORT          = "0.0.0.0", 8000
_DIR                = os.path.dirname(os.path.abspath(__file__))
BASELINE_FILE       = os.path.join(_DIR, "baseline.json")
MARKS_FILE          = os.path.join(_DIR, "marks.json")
BASELINE_SECS       = 600          # 10 min empty-room capture
EXPIRE_SECS         = 120          # forget a MAC unseen this long
RSSI_THRESHOLD      = -80          # ignore devices weaker than this (dBm)
ALERT_STRONGEST_MIN = -70          # only buzz on episodes at least this close (or W-marked)
ALERT_COOLDOWN_SECS = 60           # min gap between buzzes, per MAC
BOARD_LABELS        = {1: "front-left", 2: "back-right"}   # where each board sits
ZONE_MARGIN_DB      = 6            # RSSI gap below which a device is "center"
HISTORY_SECS        = 6000         # keep 1 h 40 min of per-MAC traffic history
HISTORY_INTERVAL    = 2            # one history sample per this many seconds (match POST_INTERVAL_MS)

# Episode detection: a short burst of real data traffic (a phone being used over WiFi).
EPISODE_WINDOW_SECS   = 30         # sliding window that must "raise": matches a brief phone use
EPISODE_MIN_BYTES     = 8000       # window sum must clear this floor (bytes/30s). CALIBRATE in the room.
EPISODE_MIN_FRAME     = 250        # ...and average frame size must clear this (bytes), so idle
                                   # keepalives/ACKs (tiny frames) don't count. CALIBRATE.
EPISODE_RETAIN_SECS   = 300        # keep flagging "used Xs ago" this long after an episode

# ---------------- State ----------------
lock       = threading.Lock()
boards     = {}                    # board_id -> {mac: {"rssi","last","src","flags","pkts","bytes"}}
history    = {}                    # mac -> deque[(t, pkts, bytes)] over the last HISTORY_SECS
episodes   = {}                    # mac -> list[{"start","end","peak","peak_rssi"}] (closed)
open_ep    = {}                    # mac -> {"start","peak","peak_rssi"} (currently open)
baseline   = set()
marks      = {}                    # mac -> "W" | "D"
ep_alert   = {}                    # mac -> last buzz time
mode       = "idle"                # idle | baseline | live
baseline_until = 0.0
threshold  = RSSI_THRESHOLD
last_hist  = 0.0
running    = True
web_error  = None                  # set if the web server thread fails to start


# ---------------- Ingest ----------------
def report():
    global mode
    from flask import request
    data = request.get_json(force=True, silent=True) or {}
    bid  = int(data.get("board", 0))
    devs = data.get("devs", [])
    now  = time.time()
    with lock:
        table = boards.setdefault(bid, {})
        for d in devs:
            mac = d.get("m")
            if not mac:
                continue
            rssi = int(d.get("r", -127))
            pkts = int(d.get("p", 0))
            byts = int(d.get("b", 0))
            e = table.get(mac)
            if e is None:
                table[mac] = {"rssi": rssi, "last": now, "src": int(d.get("s", 0)),
                              "flags": int(d.get("f", 0)), "pkts": pkts, "bytes": byts}
            else:
                if rssi > e["rssi"]:
                    e["rssi"] = rssi
                e["last"]   = now
                e["flags"] |= int(d.get("f", 0))
                e["pkts"]   = pkts
                e["bytes"]  = byts
            if mode == "baseline":
                baseline.add(mac)
    return "ok"


# ---------------- Persistence ----------------
def save_baseline():
    with open(BASELINE_FILE, "w") as f:
        json.dump(sorted(baseline), f)
    print(f"Saved {len(baseline)} baseline MACs to {BASELINE_FILE}")


def load_baseline():
    try:
        with open(BASELINE_FILE) as f:
            baseline.update(json.load(f))
        print(f"Loaded {len(baseline)} baseline MACs.")
    except FileNotFoundError:
        pass


def save_marks():
    with open(MARKS_FILE, "w") as f:
        json.dump(marks, f)


def load_marks():
    try:
        with open(MARKS_FILE) as f:
            marks.update(json.load(f))
    except FileNotFoundError:
        pass


# ---------------- Alerts ----------------
def notify(text):
    try:
        subprocess.run(
            ["termux-notification", "--title", "Exam alert", "--content", text],
            timeout=8, check=False,
        )
    except FileNotFoundError:
        pass   # termux-api not installed; the live console still shows everything


# ---------------- Formatting ----------------
def human_bytes(n):
    for unit in ("", "K", "M"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}G"


def human_age(s):
    s = int(s)
    if s < 60:   return f"{s}s"
    if s < 3600: return f"{s // 60}m"
    return f"{s // 3600}h"


# ---------------- History + episode detection ----------------
def sample_history(now):
    """Snapshot each active non-baseline MAC's current traffic into its history.
    Call under `lock`. Prunes samples/MACs older than HISTORY_SECS."""
    cur = {}
    for table in boards.values():
        for mac, e in table.items():
            if mac in baseline:
                continue
            pk, by = cur.get(mac, (0, 0))
            cur[mac] = (max(pk, e.get("pkts", 0)), max(by, e.get("bytes", 0)))
    for mac, (pk, by) in cur.items():
        dq = history.setdefault(mac, deque())
        dq.append((now, pk, by))
        while dq and now - dq[0][0] > HISTORY_SECS:
            dq.popleft()
    for mac in [m for m, dq in history.items() if not dq or now - dq[-1][0] > HISTORY_SECS]:
        del history[mac]


def window_stats(dq, now, window):
    """(packets, bytes) summed over the last `window` seconds."""
    pk = by = 0
    for (t, p, b) in dq:
        if now - t <= window:
            pk += p
            by += b
    return pk, by


def zone_of(per_board):
    if len(per_board) >= 2:
        (b1, r1), (b2, r2) = sorted(per_board.items(), key=lambda kv: -kv[1])[:2]
        return "center" if (r1 - r2) < ZONE_MARGIN_DB else f"near {BOARD_LABELS.get(b1, b1)}"
    b = next(iter(per_board))
    return f"near {BOARD_LABELS.get(b, b)}"


def update_episodes(now):
    """Open/close a WiFi traffic episode per MAC (floor + average-frame-size gate),
    and buzz on non-baseline BLE presence. Call under `lock`, after sample_history()."""
    perb, srcmap = {}, {}
    for bid, table in boards.items():
        for mac, e in table.items():
            if mac in baseline or now - e["last"] > EXPIRE_SECS:
                continue
            perb.setdefault(mac, {})[bid] = e["rssi"]
            srcmap[mac] = e.get("src", 0)
    for mac, dq in history.items():
        pk, by = window_stats(dq, now, EPISODE_WINDOW_SECS)
        avg = by / pk if pk else 0
        pb  = perb.get(mac, {})
        rssi = max(pb.values()) if pb else -127
        if by >= EPISODE_MIN_BYTES and avg >= EPISODE_MIN_FRAME:   # real data, not keepalives
            op = open_ep.get(mac)
            if op is None:
                open_ep[mac] = {"start": now, "peak": by, "peak_rssi": rssi}
                _maybe_notify(mac, rssi, by, zone_of(pb) if pb else "?", now)
            else:
                op["peak"] = max(op["peak"], by)
                op["peak_rssi"] = max(op["peak_rssi"], rssi)
        else:
            op = open_ep.pop(mac, None)
            if op is not None:
                op["end"] = now
                lst = episodes.setdefault(mac, [])
                lst.append(op)
                del lst[:-50]                                 # cap per-MAC episode log
    for mac, src in srcmap.items():                          # BLE presence = violation by itself
        if src == 1 and (max(perb[mac].values()) >= threshold or marks.get(mac) == "W"):
            _maybe_notify_ble(mac, max(perb[mac].values()), zone_of(perb[mac]), now)
    for mac in list(episodes):                               # prune old episodes
        episodes[mac] = [e for e in episodes[mac] if now - e["end"] <= HISTORY_SECS]
        if not episodes[mac] and mac not in open_ep:
            del episodes[mac]


def _maybe_notify(mac, rssi, ws, zone, now):
    if marks.get(mac) == "D":
        return
    if not (rssi >= ALERT_STRONGEST_MIN or marks.get(mac) == "W"):
        return
    if now - ep_alert.get(mac, 0) < ALERT_COOLDOWN_SECS:
        return
    ep_alert[mac] = now
    tag = " [WATCH]" if marks.get(mac) == "W" else ""
    notify(f"phone used{tag}: {zone}, {human_bytes(ws)} @ {rssi} dBm")


def _maybe_notify_ble(mac, rssi, zone, now):
    if marks.get(mac) == "D":
        return
    if now - ep_alert.get(mac, 0) < ALERT_COOLDOWN_SECS:
        return
    ep_alert[mac] = now
    notify(f"BLE device present (watch/earbud?): {zone}, {rssi} dBm")


def episode_info(mac, now):
    if mac in open_ep:
        op = open_ep[mac]
        return {"state": "active", "age": 0.0, "peak": op["peak"], "peak_rssi": op["peak_rssi"]}
    lst = episodes.get(mac)
    if lst:
        last = lst[-1]
        age = now - last["end"]
        if age <= EPISODE_RETAIN_SECS:
            return {"state": "recent", "age": age, "peak": last["peak"], "peak_rssi": last["peak_rssi"]}
    return None


# ---------------- Merge / zone / sort ----------------
def _sortkey(d):
    # W (watch) on top, unmarked in the middle, D (disregard) at the bottom; within each
    # group: episodes and BLE-present devices float up (episodes by recency), then by RSSI.
    g = 0 if d["mark"] == "W" else (2 if d["mark"] == "D" else 1)
    ep = d["episode"]
    flagged = 0 if (ep or d["src"] == 1) else 1
    return (g, flagged, ep["age"] if ep else 0.0, -d["rssi"])


def merged_view(now):
    perb, meta, traf = {}, {}, {}
    for bid, table in boards.items():
        for mac, e in table.items():
            if now - e["last"] > EXPIRE_SECS or mac in baseline:
                continue
            perb.setdefault(mac, {})[bid] = e["rssi"]
            meta[mac] = (e["src"], e["flags"])
            pk, by = traf.get(mac, (0, 0))
            traf[mac] = (max(pk, e.get("pkts", 0)), max(by, e.get("bytes", 0)))
    out = []
    for mac, pb in perb.items():
        best = max(pb.values())
        mark = marks.get(mac)
        if best < threshold and mark is None:                # keep marked devices visible even if weak
            continue
        src, flags = meta[mac]
        pk, by = traf[mac]
        out.append({"mac": mac, "rssi": best, "zone": zone_of(pb), "src": src, "flags": flags,
                    "pkts": pk, "bytes": by, "mark": mark, "episode": episode_info(mac, now)})
    out.sort(key=_sortkey)
    return out


# ---------------- Render loop ----------------
RED, GREEN, YEL, DIM, RST = "\033[31m", "\033[32m", "\033[33m", "\033[2m", "\033[0m"


def render_loop():
    global last_hist
    while running:
        now = time.time()
        with lock:
            if mode == "baseline" and now >= baseline_until:
                _set_mode("live", quiet=True)
                save_baseline()
            if mode == "live" and now - last_hist >= HISTORY_INTERVAL:
                sample_history(now)
                update_episodes(now)
                last_hist = now
            view = merged_view(now) if mode == "live" else []
            m, thr, base_n = mode, threshold, len(baseline)

        os.system("clear")
        print(f"=== signal_detector collector ===  mode={m}  threshold={thr} dBm  baseline={base_n} MACs")
        web = f"web DOWN -> {web_error}" if web_error else f"web http://<phone-ip>:{PORT}/"
        print(f"keys: b baseline | l live | i idle | s save | c clear | +/- threshold | q quit    {web}\n")

        if m == "baseline":
            with lock:
                left = int(baseline_until - now)
            print(f"BASELINE capturing... {left:>4}s left. Leave the room empty.")
        elif m == "live":
            print(f"active devices: {len(view)}   "
                  f"({RED}W{RST}=watch top  {GREEN}D{RST}=disregard bottom  "
                  f"{YEL}BLE{RST}=watch/earbud present  USED=phone used)\n")
            print(f"  {'M':1} {'MAC':<17}  {'RSSI':>5}  {'ZONE':<16}  {'TRAFFIC/2s':>13}  {'USED':>10}  TYPE")
            for d in view:
                ble = d["src"] == 1
                tags = ("BLE" if ble else "WiFi")
                if d["flags"] & 0x01: tags += " rand"
                if d["flags"] & 0x02: tags += " apple"
                traffic = "-" if ble else f"{d['pkts']:>4}p {human_bytes(d['bytes']):>6}"
                ep = d["episode"]
                if ble:
                    used = "present"
                else:
                    used = "NOW" if ep and ep["state"] == "active" else (human_age(ep["age"]) if ep else "")
                mk = d["mark"]
                mcol = f"{RED}W{RST}" if mk == "W" else (f"{GREEN}D{RST}" if mk == "D" else " ")
                line = (f"  {mcol} {d['mac']:<17}  {d['rssi']:>4}d  {d['zone']:<16}  "
                        f"{traffic:>13}  {used:>10}  {tags}")
                if   mk == "W": print(f"{RED}{line}{RST}")
                elif mk == "D": print(f"{DIM}{line}{RST}")
                elif ble:       print(f"{YEL}{line}{RST}")
                else:           print(line)
        else:
            print("idle. press 'l' to start live detection, 'b' to (re)baseline.")

        time.sleep(2)


# ---------------- Web UI ----------------
INDEX_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>signal_detector</title>
<style>
  :root { color-scheme: dark; }
  body { font-family: system-ui, sans-serif; margin:0; background:#111; color:#eee; }
  header { padding:10px 14px; background:#1b1b1b; border-bottom:1px solid #333; }
  header b { color:#7fd1ff; }
  #wrap { display:flex; flex-wrap:wrap; gap:12px; padding:12px; }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th,td { text-align:left; padding:5px 8px; border-bottom:1px solid #2a2a2a; white-space:nowrap; }
  th { color:#999; font-weight:600; }
  tr.dev { cursor:pointer; }
  tr.dev:hover { filter:brightness(1.25); }
  tr.sel  { outline:1px solid #7fd1ff; }
  tr.w { background:#3a1f22; }            /* watch  -> red   */
  tr.d { background:#1f3a24; color:#9c9; } /* disregard -> green */
  tr.ble { background:#3a331f; }          /* BLE watch/earbud present -> yellow */
  .used { color:#ffcf5c; font-weight:bold; }
  .present { color:#ffd166; font-weight:bold; }
  #calib { margin-top:6px; }
  .rand { color:#c58cff; } .apple { color:#7fd1ff; }
  .mk { display:inline-block; width:18px; text-align:center; border:1px solid #444; border-radius:4px;
        margin-right:3px; cursor:pointer; color:#bbb; user-select:none; }
  .mk:hover { border-color:#888; }
  .mk.onW { background:#c0392b; color:#fff; border-color:#c0392b; }
  .mk.onD { background:#27ae60; color:#fff; border-color:#27ae60; }
  #panel { flex:1; min-width:320px; }
  canvas { width:100%; max-width:640px; height:220px; background:#181818; border:1px solid #333; border-radius:6px; }
  #ranges { margin:4px 0 6px; }
  .rng { display:inline-block; padding:2px 8px; margin-right:4px; border:1px solid #444; border-radius:4px;
         cursor:pointer; color:#bbb; font-size:13px; user-select:none; }
  .rng.on { background:#243447; color:#fff; border-color:#7fd1ff; }
  .muted { color:#888; font-size:13px; }
  .col-list { flex:1; min-width:360px; }
</style></head><body>
<header><b>signal_detector</b> &nbsp; <span id="status" class="muted"></span></header>
<div id="wrap">
  <div class="col-list">
    <table><thead><tr><th>mark</th><th>MAC</th><th>RSSI</th><th>ZONE</th><th>TRAFFIC/2s</th><th>USED</th><th>TYPE</th></tr></thead>
    <tbody id="rows"></tbody></table>
  </div>
  <div id="panel">
    <div id="chartTitle" class="muted">Select a device to see its traffic. Shaded humps are episodes.</div>
    <div id="ranges">
      <span class="rng" data-s="300" onclick="setSpan(300)">5m</span>
      <span class="rng on" data-s="900" onclick="setSpan(900)">15m</span>
      <span class="rng" data-s="1800" onclick="setSpan(1800)">30m</span>
      <span class="rng" data-s="3600" onclick="setSpan(3600)">60m</span>
      <span class="rng" data-s="6000" onclick="setSpan(6000)">1h40m</span>
    </div>
    <canvas id="chart" width="640" height="220"></canvas>
    <div id="chartInfo" class="muted"></div>
    <div id="calib" class="muted"></div>
  </div>
</div>
<script>
let sel = null, span = 900, lastHist = null;
function human(n){ let u=['','K','M','G'],i=0; while(n>=1024&&i<3){n/=1024;i++;} return (i?n.toFixed(1):n.toFixed(0))+u[i]; }
function ago(s){ s=Math.round(s); if(s<60)return s+'s'; if(s<3600)return Math.round(s/60)+'m'; return Math.round(s/3600)+'h'; }
function spanLabel(s){ if(s<3600) return (s/60)+'m'; let h=Math.floor(s/3600), m=Math.round((s%3600)/60); return h+'h'+(m?m+'m':''); }
function setSpan(s){ span=s;
  document.querySelectorAll('.rng').forEach(b=>b.classList.toggle('on', +b.dataset.s===s));
  if(lastHist) draw(lastHist.points, lastHist.episodes, lastHist.now);
}
async function setMark(mac, mark, ev){ ev.stopPropagation(); await fetch('/api/mark?mac='+encodeURIComponent(mac)+'&mark='+mark); loadDevices(); }
function mkbtn(x, letter, cls){
  let on = (x.mark===letter) ? (' on'+letter) : '';
  return '<span class="mk'+on+'" onclick="setMark(\\''+x.mac+'\\',\\''+(x.mark===letter?'-':letter)+'\\',event)">'+cls+'</span>';
}
async function loadDevices(){
  let d = await (await fetch('/api/devices')).json();
  document.getElementById('status').textContent =
    'mode='+d.mode+'  threshold='+d.threshold+' dBm  devices='+d.devices.length;
  let tb = document.getElementById('rows'); tb.innerHTML='';
  for(const x of d.devices){
    let ble = x.type==='BLE';
    let tr = document.createElement('tr');
    tr.className = 'dev' + (x.mark==='W'?' w':(x.mark==='D'?' d':(ble?' ble':''))) + (x.mac===sel?' sel':'');
    let type = x.type + (x.rand?' <span class="rand">rand</span>':'') + (x.apple?' <span class="apple">apple</span>':'');
    let traf = ble ? '-' : (x.pkts+'p '+human(x.bytes));
    let used = ble ? '<span class="present">present</span>'
             : (x.episode ? (x.episode.state==='active' ? '<span class="used">NOW</span>' : 'used '+ago(x.episode.age)+' ago') : '');
    tr.innerHTML =
      '<td>'+mkbtn(x,'W','W')+mkbtn(x,'D','D')+'</td>'+
      '<td>'+x.mac+'</td><td>'+x.rssi+'d</td><td>'+x.zone+'</td>'+
      '<td>'+traf+'</td><td>'+used+'</td><td>'+type+'</td>';
    tr.onclick = ()=>{ sel = x.mac; loadDevices(); loadHistory(); };
    tb.appendChild(tr);
  }
}
async function loadHistory(){
  if(!sel) return;
  let d = await (await fetch('/api/history?mac='+encodeURIComponent(sel))).json();
  lastHist = d;
  document.getElementById('chartTitle').innerHTML =
    'Traffic of <b>'+sel+'</b> &mdash; '+d.episodes.length+' episode(s)';
  let w = d.win, hit = (w.bytes>=d.floor && w.avg>=d.min_frame);
  document.getElementById('calib').innerHTML =
    'calib '+w.secs+'s: sum <b>'+human(w.bytes)+'B</b> avg-frame <b>'+w.avg+'B</b>'
    +' &nbsp;|&nbsp; floor '+human(d.floor)+'B &amp; '+d.min_frame+'B &rarr; '
    +(hit ? '<span class="present">EPISODE</span>' : 'below');
  draw(d.points, d.episodes, d.now);
}
function draw(pts, eps, now){
  let c=document.getElementById('chart'), g=c.getContext('2d');
  let W=c.width, H=c.height, pad=30; g.clearRect(0,0,W,H);
  let t0 = now - span;
  let vis = pts.filter(p=>p[0]>=t0);
  let X = t => pad + (W-pad-4)*(1-(now-t)/span);
  let maxB = Math.max(1, ...vis.map(p=>p[1]));
  let Y = b => (H-pad) - (H-pad-4)*(b/maxB);
  for(const e of eps){                               // shaded episode humps, clipped to window
    if(e[1] < t0) continue;
    let x1=X(Math.max(e[0],t0)), x2=X(e[1]); g.fillStyle='rgba(255,140,60,0.22)';
    g.fillRect(Math.min(x1,x2), 4, Math.max(2,Math.abs(x2-x1)), H-pad-4);
  }
  g.strokeStyle='#333'; g.beginPath(); g.moveTo(pad,H-pad); g.lineTo(W-4,H-pad); g.moveTo(pad,4); g.lineTo(pad,H-pad); g.stroke();
  g.fillStyle='#888'; g.font='11px sans-serif';
  g.fillText(human(maxB)+'B',2,12); g.fillText('0',pad-12,H-pad+4); g.fillText('-'+spanLabel(span),pad,H-8); g.fillText('now',W-28,H-8);
  if(!vis.length){ document.getElementById('chartInfo').textContent='no traffic in the last '+spanLabel(span); return; }
  g.strokeStyle='#7fd1ff'; g.fillStyle='rgba(127,209,255,0.15)'; g.beginPath(); g.moveTo(X(vis[0][0]),H-pad);
  for(const p of vis) g.lineTo(X(p[0]),Y(p[1]));
  g.lineTo(X(vis[vis.length-1][0]),H-pad); g.closePath(); g.fill();
  g.beginPath(); vis.forEach((p,i)=>{ let x=X(p[0]),y=Y(p[1]); i?g.lineTo(x,y):g.moveTo(x,y); }); g.stroke();
  let last=vis[vis.length-1];
  document.getElementById('chartInfo').textContent =
    'window '+spanLabel(span)+'   latest '+human(last[1])+'B/2s   peak '+human(maxB)+'B   samples '+vis.length;
}
loadDevices(); setInterval(loadDevices,3000); setInterval(loadHistory,3000);
</script></body></html>"""


def index():
    return INDEX_HTML


def api_devices():
    now = time.time()
    with lock:
        view = merged_view(now) if mode == "live" else []
        out = [{"mac": d["mac"], "rssi": d["rssi"], "zone": d["zone"],
                "type": "BLE" if d["src"] else "WiFi",
                "rand": bool(d["flags"] & 0x01), "apple": bool(d["flags"] & 0x02),
                "pkts": d["pkts"], "bytes": d["bytes"], "mark": d["mark"],
                "episode": d["episode"]} for d in view]
        payload = {"mode": mode, "threshold": threshold, "devices": out}
    return json.dumps(payload), 200, {"Content-Type": "application/json"}


def api_history():
    from flask import request
    mac = request.args.get("mac", "")
    now = time.time()
    with lock:
        dq = history.get(mac)
        pts = [[round(t, 1), b, p] for (t, p, b) in dq] if dq else []
        eps = [[round(e["start"], 1), round(e["end"], 1)] for e in episodes.get(mac, [])]
        if mac in open_ep:
            eps.append([round(open_ep[mac]["start"], 1), round(now, 1)])
        mark = marks.get(mac)
        pk, by = window_stats(dq, now, EPISODE_WINDOW_SECS) if dq else (0, 0)
        win = {"secs": EPISODE_WINDOW_SECS, "bytes": by, "pkts": pk,
               "avg": round(by / pk) if pk else 0}
    return json.dumps({"mac": mac, "now": round(now, 1), "mark": mark,
                       "points": pts, "episodes": eps, "win": win,
                       "floor": EPISODE_MIN_BYTES, "min_frame": EPISODE_MIN_FRAME}
                      ), 200, {"Content-Type": "application/json"}


def api_mark():
    from flask import request
    mac = request.args.get("mac", "")
    mk  = request.args.get("mark", "")
    with lock:
        if mk in ("W", "D"):
            marks[mac] = mk
        else:
            marks.pop(mac, None)
        save_marks()
    return "ok"


# ---------------- Commands ----------------
def _set_mode(new, quiet=False):
    global mode, baseline_until
    mode = new
    if new == "baseline":
        baseline.clear()
        baseline_until = time.time() + BASELINE_SECS
    if not quiet:
        print(f">> mode = {new}")


def input_loop():
    global mode, threshold, running
    for line in sys.stdin:
        c = line.strip()[:1]
        with lock:
            if   c == "b": _set_mode("baseline")
            elif c == "l": _set_mode("live")
            elif c == "i": _set_mode("idle")
            elif c == "s": save_baseline()
            elif c == "c": baseline.clear(); print("baseline cleared.")
            elif c == "+": threshold += 5; print(f"threshold = {threshold}")
            elif c == "-": threshold -= 5; print(f"threshold = {threshold}")
            elif c == "q": running = False; break
    os._exit(0)


HELP = f"""\
collector.py - room phone-presence collector for the ESP32 sensors.

WHAT IT DOES
    Receives device batches from the two ESP32 boards over HTTP (port {PORT}),
    ignores the baseline (ambient MACs learned in the empty room), zones each
    device to a corner, and detects short traffic "episodes" - a phone used for
    a moment. Buzzes you when an episode opens on a nearby (or watched) device.

WEB UI  ->  http://<phone-ip>:{PORT}/   (or http://localhost:{PORT}/ on the phone)
    - Live device list. Click a MAC to graph its traffic over the last 2 h;
      episodes show as shaded humps.
    - Mark a MAC W (watch) or D (disregard) with the buttons on its row.
      Watched (W, red) sort to the TOP; disregarded (D, green) to the BOTTOM.
      Marks persist in marks.json.
    - USED column: "NOW" during an episode, else "used Xs ago" for a while after.

EPISODE DETECTION (phones, over WiFi)
    A device is "in an episode" when, over the last {EPISODE_WINDOW_SECS} s, its traffic clears
    EPISODE_MIN_BYTES AND its average frame size clears EPISODE_MIN_FRAME, so idle
    keepalives/ACKs (tiny frames) don't count. Calibrate both in the room: select
    a test device and watch the web page's "calib" readout while it idles, then
    while it does one real query; set the floor in the gap between the two.

BLE PRESENCE (watches / earbuds)
    Non-baseline BLE devices are flagged just by being present - a watch or
    earbud in the room is itself a violation. They show yellow and sort up.

USAGE
    python collector.py            start the collector
    python collector.py --help     show this help and exit

FIRST TIME (in Termux)
    pkg install python termux-api
    pip install flask
    termux-wake-lock
    (also install the Termux:API app from F-Droid for notifications)

KEYS (type the letter, then Enter)
    b baseline ({BASELINE_SECS // 60} min, EMPTY room) | l live | i idle | s save | c clear | +/- threshold | q quit

LIMITS
    Off / airplane-mode phones emit nothing and are invisible. Randomized MACs
    inflate the count. Zoning is corner-level, not desk-level.
"""


def main():
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(HELP)
        return

    from flask import Flask
    import logging
    logging.getLogger("werkzeug").setLevel(logging.ERROR)   # don't fight the live table
    app = Flask(__name__)
    app.add_url_rule("/report", "report", report, methods=["POST"])
    app.add_url_rule("/", "index", index)
    app.add_url_rule("/api/devices", "api_devices", api_devices)
    app.add_url_rule("/api/history", "api_history", api_history)
    app.add_url_rule("/api/mark", "api_mark", api_mark)

    def run_server():
        global web_error
        try:
            app.run(host=HOST, port=PORT, threaded=True)
        except Exception as e:
            web_error = f"{type(e).__name__}: {e}"

    load_baseline()
    load_marks()
    threading.Thread(target=run_server, daemon=True).start()
    threading.Thread(target=input_loop, daemon=True).start()
    render_loop()


if __name__ == "__main__":
    main()
