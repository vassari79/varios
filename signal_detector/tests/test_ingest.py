"""Ingest tests: the JSON the firmware actually builds must land in the right state.

Flask is only installed on the phone, so the request object is faked here; the
payloads below are byte-for-byte the shape signal_detector.ino emits.
"""

import os
import sys
import types

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (c.boards, c.rf_state, c.rf_floor, c.rf_cal, c.rf_alert,
              c.baseline, c.history, c.marks):
        d.clear()
    c.mode = "live"
    sent = []
    monkeypatch.setattr(c, "notify", sent.append)
    return sent


def post(payload, monkeypatch):
    fake = types.ModuleType("flask")
    fake.request = types.SimpleNamespace(get_json=lambda **kw: payload)
    monkeypatch.setitem(sys.modules, "flask", fake)
    return c.report()


# The firmware's batch: {"board":1,"devs":[{"m","r","s","f","p","b"}...],
#                        "rf":{"n","min","max","avg","hits"}}
DEVS = [{"m": "AA:BB:CC:DD:EE:01", "r": -55, "s": 0, "f": 1, "p": 40, "b": 12000}]


def test_devices_land_in_the_board_table(monkeypatch):
    post({"board": 1, "devs": DEVS}, monkeypatch)
    e = c.boards[1]["AA:BB:CC:DD:EE:01"]
    assert (e["rssi"], e["pkts"], e["bytes"], e["flags"]) == (-55, 40, 12000, 1)


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
    assert "AA:BB:CC:DD:EE:01" in c.baseline
    assert c.rf_cal[1] == [780.0]
    assert clean == []
