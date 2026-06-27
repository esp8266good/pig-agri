import asyncio
import sys
from unittest.mock import MagicMock

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

import database
import zmq_receiver as zmq_mod
from analysis import scheduler as scheduler_mod


@pytest.fixture
def client():
    import inference.pipeline as pipeline_mod
    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
        patch.object(scheduler_mod.Scheduler, "start", new_callable=AsyncMock),
        patch.object(scheduler_mod.Scheduler, "stop", new_callable=AsyncMock),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_stream_live_returns_m3u8_url(client):
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir):
        resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    assert "url" in resp.json()
    assert resp.json()["url"] == "/stream/hls/cam_01/rgb/2026-05-04-14/index.m3u8"



def test_alerts_no_pool_returns_503(client):
    resp = client.get("/alerts")
    assert resp.status_code == 503


def test_settings_get_returns_defaults_when_no_pool(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "hls_retention_days" in data


def test_notes_get_returns_stub(client):
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_cameras_returns_list(client):
    resp = client.get("/cameras")
    assert resp.status_code == 200
    data = resp.json()
    assert "cameras" in data
    assert isinstance(data["cameras"], list)
    assert len(data["cameras"]) > 0


def test_alerts_active_returns_empty_cache(client):
    resp = client.get("/alerts/active")
    assert resp.status_code == 200
    assert resp.json() == {"cache": {}}


def test_storage_alert_writes_health_alert(monkeypatch):
    import main
    captured = {}

    async def fake_write(pool, *, camera_id, object_id, metric,
                         current_value, mean_value, std_value):
        captured.update(camera_id=camera_id, metric=metric, object_id=object_id)
        return 1

    monkeypatch.setattr(main, "write_health_alert", fake_write, raising=False)
    monkeypatch.setattr(main.database, "get_pool", lambda: object())
    asyncio.run(main._storage_alert("storage_unwritable", 3.0, 10.0))
    assert captured["metric"] == "storage_unwritable"
    assert captured["camera_id"] == "_system"
    assert captured["object_id"] == 0


def test_storage_alert_no_pool_does_not_write(monkeypatch):
    """DB 不可用 → 不呼叫 write_health_alert，只 log。"""
    import main
    called = {"n": 0}

    async def fake_write(*a, **k):
        called["n"] += 1
        return 1

    monkeypatch.setattr(main, "write_health_alert", fake_write, raising=False)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    asyncio.run(main._storage_alert("storage_unwritable", 0.0, 10.0))
    assert called["n"] == 0


def test_supervisor_ensures_rgb_and_skips_on_drop(monkeypatch):
    import main
    ensured = []

    class FakeHls:
        def desired_recording_keys(self, cams):
            return [(c, "rgb") for c in cams]
        def has_stream(self, c, t):
            return False
        def ensure_started(self, c, t):
            ensured.append((c, t))

    monkeypatch.setattr(main, "hls_manager", FakeHls())
    monkeypatch.setattr(main.app_settings, "zmq_sources",
                        [type("S", (), {"label": "cam_01"})()])
    main._supervised_prev = set()

    # drop → 不 ensure
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "drop")
    asyncio.run(main._run_recording_supervisor_once())
    assert ensured == []

    # record → ensure rgb
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "record")
    asyncio.run(main._run_recording_supervisor_once())
    assert ("cam_01", "rgb") in ensured


def test_supervisor_fires_revive_alert_when_stream_went_missing(monkeypatch):
    import main
    alerts = []

    async def fake_alert(metric, cur, mean):
        alerts.append(metric)

    class FakeHls:
        def desired_recording_keys(self, cams):
            return [("cam_01", "rgb")]
        def has_stream(self, c, t):
            return False   # 一直不存在 → 需重建
        def ensure_started(self, c, t):
            pass

    monkeypatch.setattr(main, "hls_manager", FakeHls())
    monkeypatch.setattr(main, "_storage_alert", fake_alert)
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "record")
    monkeypatch.setattr(main.app_settings, "zmq_sources",
                        [type("S", (), {"label": "cam_01"})()])

    # 第一輪：首次建立（_supervised_prev 空）→ 不算 revive、不告警
    main._supervised_prev = set()
    asyncio.run(main._run_recording_supervisor_once())
    assert alerts == []
    # 第二輪：上一輪已列入 _supervised_prev、這輪仍 missing → revive 告警
    asyncio.run(main._run_recording_supervisor_once())
    assert "recording_supervisor_revive" in alerts
