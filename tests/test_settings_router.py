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


def test_storage_keys_in_allowed():
    from routers.settings import ALLOWED_KEYS
    for k in ("storage_min_free_gb", "storage_check_interval_seconds",
              "storage_min_free_inodes_ratio", "storage_debounce_count",
              "storage_volume_marker", "recording_schedule_enabled",
              "recording_off_start", "recording_off_end"):
        assert k in ALLOWED_KEYS


def test_new_ops_keys_allowed():
    from routers.settings import ALLOWED_KEYS
    for k in ("ntfy_url", "ntfy_enabled", "ntfy_revive_priority",
              "gpu_off_schedule_enabled", "gpu_off_start", "gpu_off_end"):
        assert k in ALLOWED_KEYS


# ── 值域檢查 ────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,value", [
    # hls_retention_days=0 → retention cutoff 變成「現在」→ 下一輪把所有
    # 未受保護的小時目錄 rmtree 掉。這是整組檢查最主要要擋的東西。
    ("hls_retention_days", "0"),
    ("hls_retention_days", "-1"),
    ("hls_retention_days", "abc"),
    ("analysis_interval_minutes", "0"),
    ("analysis_window_minutes", "0"),
    ("anomaly_std_threshold", "0"),
    ("storage_check_interval_seconds", "1"),
    ("storage_min_free_inodes_ratio", "2"),
    ("storage_debounce_count", "0"),
    ("recording_off_start", "25:00"),
    ("recording_off_end", "6:30"),        # 需補零
    ("gpu_off_start", "晚上十點"),
    ("temp_anomaly_enabled", "yes"),
    ("ntfy_revive_priority", "critical"),
    ("storage_volume_marker", "../etc/passwd"),
])
def test_validate_setting_rejects_bad_values(key, value):
    from routers.settings import validate_setting
    with pytest.raises(ValueError):
        validate_setting(key, value)


@pytest.mark.parametrize("key,value", [
    ("hls_retention_days", "90"),
    ("analysis_interval_minutes", "30"),
    ("analysis_window_minutes", "60"),
    ("anomaly_std_threshold", "3.0"),
    ("storage_check_interval_seconds", "20"),
    ("storage_min_free_gb", "10"),
    ("storage_min_free_inodes_ratio", "0.02"),
    ("storage_debounce_count", "2"),
    ("recording_off_start", "17:00"),
    ("recording_off_end", "06:30"),
    ("gpu_off_start", "22:00"),
    ("temp_anomaly_enabled", "true"),
    ("recording_schedule_enabled", "false"),
    ("ntfy_revive_priority", "default"),
    ("ntfy_url", "https://ntfy.ed716.duckdns.org/pig"),
    ("ntfy_url", ""),                     # 空＝不推播，合法
    ("storage_volume_marker", ""),        # 空＝不檢查標記，合法
    ("storage_volume_marker", ".pig_disk"),
])
def test_validate_setting_accepts_current_ui_values(key, value):
    """前端設定抽屜實際會送出的值都必須通過——上下界刻意對齊 index.html
    各 input 的 min/max，正常操作不可能撞到 400。"""
    from routers.settings import validate_setting
    validate_setting(key, value)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/pig",
    "http://localhost/pig",
    "http://192.168.50.1/admin",
    "http://10.0.0.5/pig",
    "http://[::1]/pig",
    "http://169.254.169.254/latest/meta-data",   # cloud metadata
    "file:///etc/passwd",
    "gopher://evil/x",
    "https://ntfy.example.com",                  # 缺 topic 路徑
    "not-a-url",
])
def test_ntfy_url_rejects_ssrf_targets(url):
    """ntfy_url 被 ntfy_notifier.notify 直接 POST 出去 → 等於讓呼叫端指定
    後端要連的主機。不擋內網/本機位址的話這個端點就是一支 SSRF。"""
    from routers.settings import validate_setting
    with pytest.raises(ValueError):
        validate_setting("ntfy_url", url)


def test_put_settings_rejects_retention_zero(client_with_pool):
    """繞過 UI 直接打 API 也擋得住，且不會寫進 DB。"""
    with patch("routers.settings.upsert_settings", new_callable=AsyncMock) as mock_upsert:
        resp = client_with_pool.put("/settings", json={"hls_retention_days": "0"})
    assert resp.status_code == 400
    assert "hls_retention_days" in resp.json()["detail"]
    mock_upsert.assert_not_awaited()


def test_put_settings_rejects_internal_ntfy_url(client_with_pool):
    with patch("routers.settings.upsert_settings", new_callable=AsyncMock) as mock_upsert:
        resp = client_with_pool.put(
            "/settings", json={"ntfy_url": "http://169.254.169.254/x"})
    assert resp.status_code == 400
    mock_upsert.assert_not_awaited()


def test_put_settings_skips_blank_numeric_without_error(client_with_pool):
    """使用者手動清空數值欄位時：略過該 key、保留既有值，其餘照存，
    不因為一格空白就讓整次存檔顯示失敗。"""
    with patch("routers.settings.get_all_settings", new_callable=AsyncMock) as mock_get, \
         patch("routers.settings.upsert_settings", new_callable=AsyncMock):
        mock_get.return_value = {"analysis_interval_minutes": "30"}
        resp = client_with_pool.put(
            "/settings",
            json={"hls_retention_days": "", "storage_min_free_gb": "10"},
        )
    assert resp.status_code == 200
    assert resp.json()["updated"] == ["storage_min_free_gb"]
