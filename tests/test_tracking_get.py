# tests/test_tracking_get.py
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# mock HybridSORT 避免 GPU init
for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@pytest.fixture
def app_client():
    import database
    import zmq_receiver as zmq_mod
    import inference.pipeline as pipeline_mod
    from fastapi.testclient import TestClient

    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_get_tracking_returns_logs(app_client):
    fake_logs = [
        {"object_id": 1, "bbox": [10.0, 20.0, 50.0, 60.0],
         "confidence": 0.9, "timestamp": 1000.0, "frame_id": 1}
    ]
    import database
    with patch("routers.tracking.query_tracking_logs", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = fake_logs
        resp = app_client.get("/tracking/rpi_sensors?start=990&end=1010")
    assert resp.status_code == 200
    assert resp.json()["logs"] == fake_logs


def test_get_tracking_with_object_id_filter(app_client):
    import database
    with patch("routers.tracking.query_tracking_logs", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = []
        resp = app_client.get("/tracking/rpi_sensors?start=0&end=1000&object_id=3")
    assert resp.status_code == 200
    mock_q.assert_awaited_once()
    _, kwargs = mock_q.call_args
    assert kwargs.get("object_id") == 3


def test_get_tracking_requires_start_and_end(app_client):
    resp = app_client.get("/tracking/rpi_sensors")
    assert resp.status_code == 422  # missing required params


def test_get_timeline_returns_hours(app_client):
    import database
    with patch("routers.tracking.query_timeline_hours", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = [1000000, 1003600]
        resp = app_client.get("/tracking/rpi_sensors/timeline?start_ts=0&end_ts=9999999")
    assert resp.status_code == 200
    assert resp.json()["hours"] == [1000000, 1003600]


def test_get_timeline_requires_start_ts_and_end_ts(app_client):
    resp = app_client.get("/tracking/rpi_sensors/timeline")
    assert resp.status_code == 422
