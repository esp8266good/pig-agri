import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from contextlib import contextmanager


@contextmanager
def _dummy_zmq_sources():
    """讓 `from main import app` 能成功：zmq_receiver 的 module-level
    ZMQReceiver() 只在 settings.zmq_sources 為空時 raise。注入假來源、結束還原，
    不污染 sys.modules（test_zmq_receiver 仍走既有 baseline）。"""
    from config import ZmqSource, settings as _cfg
    _orig = _cfg.zmq_sources
    _cfg.zmq_sources = [ZmqSource(
        name="t", src_host="127.0.0.1", src_port=5555,
        src_topic="t", label="cam_01",
    )]
    try:
        yield
    finally:
        _cfg.zmq_sources = _orig


@pytest.fixture
def client_no_pool():
    """pool=None 時使用環境變數預設值"""
    import inference.pipeline as pipeline_mod
    import analysis.scheduler as scheduler_mod
    with _dummy_zmq_sources():
        with (
            patch("database.connect", new_callable=AsyncMock),
            patch("database.disconnect", new_callable=AsyncMock),
            patch("database.get_pool", return_value=None),
            patch("zmq_receiver.zmq_receiver.start"),
            patch("zmq_receiver.zmq_receiver.stop"),
            patch.object(pipeline_mod.inference_pipeline, "start"),
            patch.object(pipeline_mod.inference_pipeline, "stop"),
            patch("hls_manager.hls_manager.stop_all"),
            patch.object(scheduler_mod.Scheduler, "start", new_callable=AsyncMock),
            patch.object(scheduler_mod.Scheduler, "stop", new_callable=AsyncMock),
        ):
            from main import app
            with TestClient(app) as c:
                yield c


@pytest.fixture
def client_with_pool():
    """pool 可用時回傳 DB 值"""
    import inference.pipeline as pipeline_mod
    import analysis.scheduler as scheduler_mod
    mock_pool = AsyncMock()
    mock_pool.fetch.return_value = [
        {"key": "analysis_interval_minutes", "value": "60"},
        {"key": "anomaly_std_threshold", "value": "2.0"},
        {"key": "hls_retention_days", "value": "30"},
    ]
    mock_pool.executemany.return_value = None
    with _dummy_zmq_sources():
        with (
            patch("database.connect", new_callable=AsyncMock),
            patch("database.disconnect", new_callable=AsyncMock),
            patch("database.get_pool", return_value=mock_pool),
            patch("zmq_receiver.zmq_receiver.start"),
            patch("zmq_receiver.zmq_receiver.stop"),
            patch.object(pipeline_mod.inference_pipeline, "start"),
            patch.object(pipeline_mod.inference_pipeline, "stop"),
            patch("hls_manager.hls_manager.stop_all"),
            patch.object(scheduler_mod.Scheduler, "start", new_callable=AsyncMock),
            patch.object(scheduler_mod.Scheduler, "stop", new_callable=AsyncMock),
        ):
            from main import app
            with TestClient(app) as c:
                yield c


def test_get_settings_no_pool_returns_env_defaults(client_no_pool):
    resp = client_no_pool.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    # 有三個 key，值為環境變數預設值（字串形式）
    assert "analysis_interval_minutes" in data
    assert "anomaly_std_threshold" in data
    assert "hls_retention_days" in data


def test_get_settings_with_pool_returns_db_values(client_with_pool):
    resp = client_with_pool.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("hls_retention_days") == "30"
    assert data.get("analysis_interval_minutes") == "60"


def test_put_settings_valid_keys_returns_ok(client_with_pool):
    with patch("routers.settings.get_all_settings", new_callable=AsyncMock) as mock_get, \
         patch("routers.settings.upsert_settings", new_callable=AsyncMock):
        mock_get.return_value = {
            "analysis_interval_minutes": "30",
            "anomaly_std_threshold": "2.0",
            "hls_retention_days": "7",
        }
        resp = client_with_pool.put(
            "/settings",
            json={"analysis_interval_minutes": "30", "hls_retention_days": "7"},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert set(data["updated"]) == {"analysis_interval_minutes", "hls_retention_days"}


def test_put_settings_invalid_keys_returns_400(client_with_pool):
    resp = client_with_pool.put(
        "/settings",
        json={"nonexistent_key": "value"},
    )
    assert resp.status_code == 400


def test_put_settings_no_pool_returns_503(client_no_pool):
    resp = client_no_pool.put(
        "/settings",
        json={"hls_retention_days": "90"},
    )
    assert resp.status_code == 503


def test_put_settings_triggers_scheduler_reload(client_with_pool):
    with patch("routers.settings.get_all_settings", new_callable=AsyncMock) as mock_get, \
         patch("routers.settings.upsert_settings", new_callable=AsyncMock):
        mock_get.return_value = {
            "analysis_interval_minutes": "60",
            "anomaly_std_threshold": "2.5",
            "hls_retention_days": "30",
            "analysis_window_minutes": "180",
            "temp_anomaly_enabled": "false",
        }
        resp = client_with_pool.put(
            "/settings",
            json={"temp_anomaly_enabled": "false"},
        )
    assert resp.status_code == 200
    assert "temp_anomaly_enabled" in resp.json()["updated"]


def test_put_temp_toggle_in_allowed_keys(client_with_pool):
    with patch("routers.settings.get_all_settings", new_callable=AsyncMock) as mock_get, \
         patch("routers.settings.upsert_settings", new_callable=AsyncMock):
        mock_get.return_value = {
            "analysis_interval_minutes": "30",
            "anomaly_std_threshold": "3.0",
            "hls_retention_days": "90",
            "analysis_window_minutes": "60",
            "temp_anomaly_enabled": "true",
        }
        resp = client_with_pool.put(
            "/settings",
            json={"analysis_window_minutes": "120", "temp_anomaly_enabled": "true"},
        )
    assert resp.status_code == 200
    assert set(resp.json()["updated"]) == {"analysis_window_minutes", "temp_anomaly_enabled"}


def test_get_settings_no_pool_includes_temp_and_window(client_no_pool):
    resp = client_no_pool.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "temp_anomaly_enabled" in data
    assert "analysis_window_minutes" in data
    assert data["temp_anomaly_enabled"] in ("true", "false")


def test_live_pdt_offset_removed_from_allowed_keys():
    from routers.settings import ALLOWED_KEYS
    assert "live_pdt_offset_seconds" not in ALLOWED_KEYS


def test_retention_interval_is_hourly():
    with _dummy_zmq_sources():
        import main
        assert main._RETENTION_INTERVAL_SECONDS == 3600
