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
