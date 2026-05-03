import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_live_returns_m3u8_url(client):
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir):
        resp = client.get("/stream/cam_01/live?type=rgb")
    assert resp.status_code == 200
    assert resp.json()["url"] == "/stream/hls/cam_01/rgb/2026-05-04-14/index.m3u8"


def test_live_default_type_is_rgb(client):
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir) as mock_start:
        resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    mock_start.assert_called_with("cam_01", "rgb")


def test_live_invalid_type_returns_400(client):
    resp = client.get("/stream/cam_01/live?type=invalid")
    assert resp.status_code == 400
