"""Tests for the RF (AD8317) arm of the collector."""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import collector as c


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for d in (c.rf_state, c.rf_floor, c.rf_cal, c.rf_alert, c.baseline):
        d.clear()
    c.mode = "live"
    sent = []
    monkeypatch.setattr(c, "notify", sent.append)
    return sent


def batch(peak_mv, hits=10, quiet_mv=800, n=5000):
    return {"n": n, "min": peak_mv, "max": quiet_mv, "avg": quiet_mv - 5, "hits": hits}


# ---------------- conversion ----------------
def test_dbm_is_inverting():
    assert c.rf_dbm(c.RF_V_REF_MV) == pytest.approx(c.RF_P_REF_DBM)
    assert c.rf_dbm(400) > c.rf_dbm(600)                       # less voltage = more power
    assert c.rf_dbm(500 - c.RF_SLOPE_MV_DB) == pytest.approx(c.RF_P_REF_DBM + 1)


def test_db_over_ambient():
    assert c.rf_db_over(800 - 3 * c.RF_SLOPE_MV_DB, 800) == pytest.approx(3.0)
    assert c.rf_db_over(800, 800) == 0.0


# ---------------- burst detection ----------------
def test_no_floor_means_no_burst(clean):
    assert c.rf_report(1, batch(100), now=1.0) is False        # loud, but nothing to compare to
    assert clean == []
    assert c.rf_state[1]["over"] == 0.0


def test_burst_needs_margin_and_hits(clean):
    c.rf_floor[1] = 800.0
    quiet = 800 - (c.RF_MARGIN_DB - 1) * c.RF_SLOPE_MV_DB      # 2 dB over: under the margin
    loud  = 800 - (c.RF_MARGIN_DB + 1) * c.RF_SLOPE_MV_DB      # 4 dB over
    assert c.rf_report(1, batch(quiet), now=1.0) is False
    assert c.rf_report(1, batch(loud, hits=c.RF_MIN_HITS - 1), now=2.0) is False
    assert c.rf_report(1, batch(loud, hits=c.RF_MIN_HITS), now=3.0) is True
    assert len(clean) == 1
    assert "burst" in clean[0] and "front-left" in clean[0]


def test_alert_cooldown(clean):
    c.rf_floor[1] = 800.0
    loud = 800 - 10 * c.RF_SLOPE_MV_DB
    c.rf_report(1, batch(loud), now=100.0)
    c.rf_report(1, batch(loud), now=100.0 + c.RF_ALERT_COOLDOWN - 1)
    assert len(clean) == 1
    c.rf_report(1, batch(loud), now=100.0 + c.RF_ALERT_COOLDOWN)
    assert len(clean) == 2


def test_boards_alert_independently(clean):
    c.rf_floor.update({1: 800.0, 2: 800.0})
    loud = 800 - 10 * c.RF_SLOPE_MV_DB
    c.rf_report(1, batch(loud), now=1.0)
    c.rf_report(2, batch(loud), now=1.0)
    assert len(clean) == 2


# ---------------- baseline ----------------
def test_baseline_collects_and_never_alerts(clean):
    c.mode = "baseline"
    c.rf_floor[1] = 800.0
    c.rf_report(1, batch(100), now=1.0)                        # very loud, but we are calibrating
    assert clean == []
    assert c.rf_cal[1] == [100.0]


def test_finish_baseline_takes_low_quantile(clean):
    c.mode = "baseline"
    for mv in [700, 701, 702, 703, 704, 705, 706, 707, 708, 400]:   # 400 = one outlier spike
        c.rf_report(1, batch(mv), now=1.0)
    c.rf_finish_baseline()
    assert c.rf_floor[1] == 700.0                              # outlier excluded, ambient peak kept
    assert c.rf_cal == {}


def test_mode_change_out_of_baseline_finishes_it(clean):
    c.mode = "idle"
    c._set_mode("baseline", quiet=True)
    for mv in (750, 751, 752):
        c.rf_report(1, batch(mv), now=1.0)
    c._set_mode("live", quiet=True)
    assert c.rf_floor[1] == 750.0
    c._set_mode("baseline", quiet=True)                        # a new baseline drops old samples
    assert c.rf_cal == {}


# ---------------- persistence ----------------
def test_baseline_roundtrip(tmp_path, monkeypatch, clean):
    monkeypatch.setattr(c, "BASELINE_FILE", str(tmp_path / "baseline.json"))
    c.baseline.update({"AA:BB:CC:DD:EE:FF"})
    c.rf_floor[1] = 812.5
    c.save_baseline()
    c.baseline.clear()
    c.rf_floor.clear()
    c.load_baseline()
    assert c.baseline == {"AA:BB:CC:DD:EE:FF"}
    assert c.rf_floor == {1: 812.5}


def test_loads_pre_rf_baseline_format(tmp_path, monkeypatch, clean):
    p = tmp_path / "baseline.json"
    p.write_text(json.dumps(["AA:BB:CC:DD:EE:FF"]))            # old format: a bare list
    monkeypatch.setattr(c, "BASELINE_FILE", str(p))
    c.load_baseline()
    assert c.baseline == {"AA:BB:CC:DD:EE:FF"}
    assert c.rf_floor == {}


# ---------------- view ----------------
def test_stale_boards_drop_out_of_the_view(clean):
    c.rf_floor[1] = 800.0
    c.rf_report(1, batch(700), now=1000.0)
    assert len(c.rf_view(1000.0)) == 1
    assert c.rf_view(1000.0 + c.RF_STALE_SECS + 1) == []


def test_view_reports_time_since_last_burst(clean):
    c.rf_floor[1] = 800.0
    c.rf_report(1, batch(800 - 10 * c.RF_SLOPE_MV_DB), now=1000.0)
    c.rf_report(1, batch(800), now=1005.0)                     # quiet again
    row = c.rf_view(1005.0)[0]
    assert row["burst"] is False
    assert row["since"] == 5.0
