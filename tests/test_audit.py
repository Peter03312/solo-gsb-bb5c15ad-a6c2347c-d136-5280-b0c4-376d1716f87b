"""排程审计端到端测试（FastAPI TestClient，离线天文计算）。

成功用例使用兴隆山址（约东经 117.6°、北纬 40.4°）与 M31：
10 月初的傍晚，M31 在入夜时已高悬，属于稳定可见目标。
"""
from __future__ import annotations

import sys
import os
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

SITE = {
    "name": "兴隆观测站",
    "latitude": 40.3933,
    "longitude": 117.5786,
    "elevation_m": 900.0,
}
TELESCOPE = {
    "name": "测试相机",
    "filters": ["V", "R", "B"],
    "min_readout_seconds": 10.0,
    "min_exposure_seconds": 0.1,
    "max_exposure_seconds": 600.0,
    "filter_change": {"V->R": 25.0, "R->V": 25.0,
                      "V->B": 30.0, "B->V": 30.0,
                      "R->B": 30.0, "B->R": 30.0},
}
M31 = {"name": "M31", "ra": "00:42:44.3", "dec": "+41:16:09"}


def _base(**over):
    req = {
        "observation_date": "2026-10-06",
        "timezone": "Asia/Shanghai",
        "site": SITE,
        "target": M31,
        "sequence": [
            {"filter": "V", "exposure_seconds": 120.0, "repeats": 3},
            {"filter": "R", "exposure_seconds": 60.0, "repeats": 2},
        ],
        "telescope": TELESCOPE,
        "min_altitude_deg": 20.0,
        "twilight": "astronomical",
    }
    req.update(over)
    return req


def test_health():
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_accepted_timeline_and_switch_costs():
    r = client.post("/api/v1/audit", json=_base())
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["accepted"] is True

    frames = data["frames"]
    assert len(frames) == 5
    # 每帧曝光时长正确
    assert [f["exposure_seconds"] for f in frames] == [120]*3 + [60]*2

    # 帧间空隙：同块相邻帧恰好为最小读出 10s
    def gap(i):
        a = datetime.fromisoformat(frames[i]["end_utc"])
        b = datetime.fromisoformat(frames[i+1]["start_utc"])
        return (b - a).total_seconds()
    assert gap(0) == 10.0
    assert gap(1) == 10.0
    # V->R 切换发生在第 3 帧与第 4 帧之间：10s 读出 + 25s 切换
    assert gap(2) == 35.0
    assert gap(3) == 10.0

    assert len(data["filter_changes"]) == 1
    ch = data["filter_changes"][0]
    assert ch["from_filter"] == "V" and ch["to_filter"] == "R"
    assert ch["seconds"] == 25.0

    totals = data["totals"]
    assert totals["frame_count"] == 5
    assert totals["exposure_seconds"] == 3*120 + 2*60
    assert totals["readout_seconds"] == 4*10.0
    assert totals["filter_change_seconds"] == 25.0
    # 挂钟 = 纯曝光 480 + 帧间空隙（10+10+35+10）= 545
    assert totals["wall_clock_seconds"] == 545.0

    assert data["constraints"]["min_readout_respected"] is True
    assert data["constraints"]["all_frames_in_window"] is True
    assert data["last_frame"]["margin_ok"] is True
    assert data["last_frame"]["status"] == "COMPLETED_IN_WINDOW"

    # 所有帧在对应可见窗口内
    for f in frames:
        assert f["start_utc"] >= f["window_start_utc"]
        assert f["end_utc"] <= f["window_end_utc"]


def test_unknown_timezone_rejected():
    req = _base(timezone="Mars/Olympus")
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["issues"]]
    assert "INVALID_INPUT" in codes


def test_unsupported_filter_rejected():
    req = _base(sequence=[{"filter": "Z", "exposure_seconds": 10,
                           "repeats": 1}])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    issues = r.json()["issues"]
    assert any(i["code"] == "FILTER_NOT_SUPPORTED"
               and i["item_index"] == 0 for i in issues)


def test_exposure_below_instrument_limit_rejected():
    req = _base(sequence=[{"filter": "V", "exposure_seconds": 0.01,
                           "repeats": 1}])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    assert any(i["code"] == "EXPOSURE_UNSUPPORTED"
               for i in r.json()["issues"])


def test_target_below_horizon_rejected():
    # 金牛座区域在 4 月入夜位于地平线以下（取傍晚即落下的南天/白昼目标）
    req = _base(
        observation_date="2026-04-20",
        target={"name": "FAINT_DAY", "ra": "06:00:00", "dec": "-50:00:00"},
        min_altitude_deg=30.0,
        sequence=[{"filter": "V", "exposure_seconds": 60,
                   "repeats": 1}])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    codes = [i["code"] for i in r.json()["issues"]]
    assert "TARGET_NOT_VISIBLE" in codes


def test_sequence_exceeds_window_rejected():
    # 200 帧 ×300s + 读出 ≈ 17 小时，必然超过任何单一可见窗口
    req = _base(sequence=[{"filter": "V", "exposure_seconds": 300.0,
                           "repeats": 200}])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    assert any(i["code"] == "SEQUENCE_EXCEEDS_WINDOW"
               for i in r.json()["issues"])


def test_interleaved_targets_ambiguous():
    req = _base(
        target=None,
        targets=[
            {"name": "A", "ra": "00:42:44.3", "dec": "+41:16:09"},
            {"name": "B", "ra": "22:30:00", "dec": "+30:00:00"},
        ],
        sequence=[
            {"target": "A", "filter": "V", "exposure_seconds": 60,
             "repeats": 1},
            {"target": "B", "filter": "V", "exposure_seconds": 60,
             "repeats": 1},
            {"target": "A", "filter": "R", "exposure_seconds": 60,
             "repeats": 1},
        ])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    issues = r.json()["issues"]
    assert any(i["code"] == "ORDER_AMBIGUOUS" and i["target"] == "A"
               for i in issues)


def test_undefined_switch_pair_rejected():
    telescope = dict(TELESCOPE)
    telescope["filter_change"] = {"V->R": 25.0}  # 缺 R->V
    req = _base(
        telescope=telescope,
        sequence=[
            {"filter": "V", "exposure_seconds": 60, "repeats": 1},
            {"filter": "R", "exposure_seconds": 60, "repeats": 1},
            {"filter": "V", "exposure_seconds": 60, "repeats": 1},
        ])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    assert any(i["code"] == "FILTER_SWITCH_UNDEFINED"
               and i["item_index"] == 2
               for i in r.json()["issues"])


def test_missing_target_reference_rejected():
    req = _base(
        target=None,
        targets=[{"name": "A", "ra": "00:42:44.3", "dec": "+41:16:09"}],
        sequence=[{"target": "X", "filter": "V",
                   "exposure_seconds": 60, "repeats": 1}])
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    assert any(i["code"] == "TARGET_NOT_FOUND"
               for i in r.json()["issues"])


def test_extra_field_is_ambiguity_error():
    req = _base()
    req["unknown_field"] = 123
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 422
    assert any(i["code"] == "AMBIGUOUS_INPUT"
               for i in r.json()["issues"])


def test_start_at_constrains_schedule():
    # 指定较晚开始，首帧起点不早于 start_at
    req = _base(start_at="2026-10-06T23:30:00+08:00")
    r = client.post("/api/v1/audit", json=req)
    assert r.status_code == 200, r.text
    first = datetime.fromisoformat(r.json()["frames"][0]["start_utc"])
    assert first >= datetime.fromisoformat("2026-10-06T15:30:00+00:00")


def test_repeats_are_contiguous_blocks():
    r = client.post("/api/v1/audit", json=_base())
    seqs = r.json()["sequence"]
    assert [s["frames"] for s in seqs] == [3, 2]
    assert all(s["within_visible_window"] for s in seqs)
