import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# mock HybridSORT 避免 import 時觸發 GPU init
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
    """TestClient with all lifespan side-effects mocked"""
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


# ── ConnectionManager 單元測試（不需 TestClient，用 mock WebSocket）──

def test_broadcast_sends_text_to_connected_ws():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    mock_ws = MagicMock()
    mock_ws.accept = AsyncMock()
    mock_ws.send_text = AsyncMock()

    loop.run_until_complete(mgr.connect("cam_01", mock_ws))
    loop.run_until_complete(mgr.broadcast("cam_01", {"frame_id": 1, "objects": []}))

    mock_ws.send_text.assert_called_once()
    sent = mock_ws.send_text.call_args[0][0]
    assert '"frame_id": 1' in sent
    loop.close()


def test_broadcast_removes_dead_connection():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    dead_ws = MagicMock()
    dead_ws.accept = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("closed"))

    loop.run_until_complete(mgr.connect("cam_01", dead_ws))
    assert dead_ws in mgr._connections["cam_01"]

    loop.run_until_complete(mgr.broadcast("cam_01", {"frame_id": 1}))
    assert dead_ws not in mgr._connections.get("cam_01", set())
    loop.close()


def test_disconnect_removes_ws():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    mock_ws = MagicMock()
    mock_ws.accept = AsyncMock()

    loop.run_until_complete(mgr.connect("cam_01", mock_ws))
    loop.run_until_complete(mgr.disconnect("cam_01", mock_ws))
    assert mock_ws not in mgr._connections.get("cam_01", set())
    loop.close()


# ── HTTP endpoint 確認（via TestClient）──

def test_get_tracking_requires_query_params(app_client):
    # start/end 現在是必要參數；缺少時回傳 422
    resp = app_client.get("/tracking/cam_01")
    assert resp.status_code == 422
