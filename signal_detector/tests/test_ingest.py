"""Ingest tests: the JSON the firmware actually builds must land in the right state.

Flask is only installed on the phone, so the request object is faked here; the
payloads below are byte-for-byte the shape signal_detector.ino emits.
"""

import json
import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c

REAL_NOTIFY = c.notify   # captured before the `clean` fixture swaps it for a spy


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (c.boards, c.rf_state, c.rf_floor, c.rf_cal, c.rf_alert,
              c.baseline, c.history, c.marks):
        d.clear()
    c.set_hunt(None)
    c.mode = "live"
    sent = []
    monkeypatch.setattr(c, "notify", sent.append)
    return sent


def post(payload, monkeypatch):
    fake = types.ModuleType("flask")
    fake.request = types.SimpleNamespace(get_json=lambda **kw: payload)
    monkeypatch.setitem(sys.modules, "flask", fake)
    return c.report()


def get(args, monkeypatch, fn):
    fake = types.ModuleType("flask")
    fake.request = types.SimpleNamespace(args=args)
    monkeypatch.setitem(sys.modules, "flask", fake)
    return fn()


# The firmware's batch: {"board":1,"devs":[{"m","r","s","f","p","b","c"}...],
#                        "rf":{"n","min","max","avg","hits"}}
MAC  = "AA:BB:CC:DD:EE:01"
DEVS = [{"m": MAC, "r": -55, "s": 0, "f": 1, "p": 40, "b": 12000, "c": 6}]


def test_devices_land_in_the_board_table(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)
    e = c.boards[1][MAC]
    assert (e["rssi"], e["pkts"], e["bytes"], e["flags"], e["ch"]) == (-55, 40, 12000, 1, 6)


def test_rf_field_is_optional(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)          # board without a detector
    assert c.rf_state == {}
    assert c.rf_view(c.time.time()) == []


def test_rf_field_is_ingested(monkeypatch):
    c.rf_floor[1] = 800.0
    post({"board": 1, "devs": DEVS,
          "rf": {"n": 5000, "min": 600, "max": 810, "avg": 805, "hits": 12}}, monkeypatch)
    st = c.rf_state[1]
    assert st["burst"] is True                             # 200 mV below ambient is ~9 dB
    assert st["over"] == pytest.approx(200 / c.RF_SLOPE_MV_DB)
    assert len(c.rf_view(st["last"])) == 1


def test_quiet_rf_batch_is_not_a_burst(monkeypatch, clean):
    c.rf_floor[1] = 800.0
    post({"board": 1, "devs": DEVS,
          "rf": {"n": 5000, "min": 795, "max": 812, "avg": 806, "hits": 0}}, monkeypatch)
    assert c.rf_state[1]["burst"] is False
    assert clean == []


def test_baseline_mode_takes_both_arms(monkeypatch, clean):
    c.mode = "baseline"
    post({"board": 1, "devs": DEVS,
          "rf": {"n": 5000, "min": 780, "max": 810, "avg": 800, "hits": 0}}, monkeypatch)
    assert MAC in c.baseline
    assert c.rf_cal[1] == [780.0]
    assert clean == []


# ---------------- Hunt arm ----------------
# The board learns its target from the reply to its own POST, so the reply is as much
# part of the firmware contract as the request is.

def test_a_device_without_a_channel_still_ingests(monkeypatch):
    """BLE-only entries carry no "c", and so do boards flashed before hunt mode."""
    post({"board": 1, "devs": [{"m": MAC, "r": -55, "s": 1, "f": 0, "p": 3, "b": 0}]},
         monkeypatch)
    assert c.boards[1][MAC]["ch"] is None
    assert c.channel_of(MAC) is None


def test_reply_is_null_when_nothing_is_being_hunted(monkeypatch):
    body, code, hdr = post({"board": 1, "devs": DEVS}, monkeypatch)
    assert json.loads(body) == {"hunt": None}
    assert (code, hdr["Content-Type"]) == (200, "application/json")


def test_reply_carries_the_target_and_its_channel(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)          # learn the channel first
    c.set_hunt(MAC)
    body, _, _ = post({"board": 1, "devs": DEVS}, monkeypatch)
    assert json.loads(body) == {"hunt": {"m": MAC, "c": 6}}


def test_hunt_channel_resolves_late(monkeypatch):
    """Aiming at a MAC heard only over BLE yields no channel; the reply stays null
    until the board hears it on WiFi, then starts naming a channel."""
    post({"board": 1, "devs": [{"m": MAC, "r": -60, "s": 1, "f": 0, "p": 2, "b": 0}]},
         monkeypatch)
    assert c.set_hunt(MAC)["ch"] is None
    body, _, _ = post({"board": 1, "devs": []}, monkeypatch)
    assert json.loads(body) == {"hunt": None}

    post({"board": 1, "devs": DEVS}, monkeypatch)          # now seen on channel 6
    body, _, _ = post({"board": 1, "devs": DEVS}, monkeypatch)
    assert json.loads(body)["hunt"]["c"] == 6


def test_the_view_exposes_each_device_channel(monkeypatch):
    """The CH column is how you catch a board that has stopped hopping: an associated
    station is pinned to its AP's channel, and then every device reports the same one."""
    post({"board": 1, "devs": [dict(DEVS[0], m="AA:BB:CC:DD:EE:01", c=8),
                               dict(DEVS[0], m="AA:BB:CC:DD:EE:02", c=8)]}, monkeypatch)
    view = c.merged_view(c.time.time())
    assert {d["ch"] for d in view} == {8}


def test_channel_of_prefers_the_freshest_board(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)
    post({"board": 2, "devs": [dict(DEVS[0], c=11)]}, monkeypatch)
    assert c.channel_of(MAC) == 11


def test_hunt_stops_itself(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)
    c.set_hunt(MAC, now=c.time.time() - c.HUNT_MAX_SECS - 1)
    body, _, _ = post({"board": 1, "devs": DEVS}, monkeypatch)
    assert json.loads(body) == {"hunt": None}
    assert c.hunt["mac"] is None


def test_api_hunt_sets_and_clears(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)
    args = types.SimpleNamespace(get=lambda k, d="": {"mac": MAC.lower()}.get(k, d))
    body, _, _ = get(args, monkeypatch, c.api_hunt)
    assert json.loads(body)["mac"] == MAC                  # normalized to upper case
    assert json.loads(body)["ch"] == 6

    args = types.SimpleNamespace(get=lambda k, d="": d)
    body, _, _ = get(args, monkeypatch, c.api_hunt)
    assert json.loads(body)["mac"] is None


# ---------------- Alerts ----------------
def test_notify_forces_a_vibrate_then_notifies(monkeypatch):
    """A silenced phone swallows termux-notification's buzz, so every alert must
    fire `termux-vibrate -f` (which ignores silent mode / DND) first."""
    calls = []
    monkeypatch.setattr(c.subprocess, "run", lambda cmd, **kw: calls.append(cmd))
    REAL_NOTIFY("phone used: near front-left")
    assert calls[0][0] == "termux-vibrate" and "-f" in calls[0]
    assert calls[1][0] == "termux-notification"


def test_notify_survives_without_termux_api(monkeypatch):
    def boom(*a, **k):
        raise FileNotFoundError
    monkeypatch.setattr(c.subprocess, "run", boom)
    REAL_NOTIFY("no termux-api installed")   # must not raise; console still shows everything
