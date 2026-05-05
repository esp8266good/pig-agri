import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    import inference.pipeline as pipeline_mod
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
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


def test_serve_hls_file_returns_200(tmp_path, monkeypatch):
    import inference.pipeline as pipeline_mod
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    ts_file = tmp_path / "cam_01" / "rgb" / "2026-05-04-14" / "seg_000.ts"
    ts_file.parent.mkdir(parents=True)
    ts_file.write_bytes(b"fake ts content")
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            resp = c.get("/stream/hls/cam_01/rgb/2026-05-04-14/seg_000.ts")
    assert resp.status_code == 200


def test_serve_hls_file_returns_404_when_missing(tmp_path, monkeypatch):
    import inference.pipeline as pipeline_mod
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            resp = c.get("/stream/hls/cam_01/rgb/2026-05-04-14/seg_999.ts")
    assert resp.status_code == 404


def test_live_unknown_camera_returns_404(client):
    resp = client.get("/stream/unknown_cam/live")
    assert resp.status_code == 404


def test_vod_returns_m3u8_content(client):
    fake_m3u8 = "#EXTM3U\n#EXT-X-ENDLIST\n"
    with patch("routers.stream.build_vod_m3u8", return_value=fake_m3u8):
        resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=rgb")
    assert resp.status_code == 200
    assert resp.text == fake_m3u8
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")


def test_vod_returns_404_when_no_segments(client):
    with patch("routers.stream.build_vod_m3u8", return_value=None):
        resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=rgb")
    assert resp.status_code == 404


def test_vod_returns_400_for_invalid_type(client):
    resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=invalid")
    assert resp.status_code == 400


def test_vod_returns_404_for_unknown_camera(client):
    resp = client.get("/stream/unknown_cam/vod?start=1000&end=4600")
    assert resp.status_code == 404
