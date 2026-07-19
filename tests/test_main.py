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
from contextlib import contextmanager


@contextmanager
def _dummy_zmq_sources():
    """隔離真實部署 .env 的 ZMQ_SOURCES（label 非 "cam_01"），讓測試中
    對 "cam_01" 的 /stream 請求不受實際攝影機設定影響。結束還原。"""
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
def client():
    import inference.pipeline as pipeline_mod
    with _dummy_zmq_sources():
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


def test_cameras_includes_active_types(client):
    import time
    import hls_manager as hm
    with patch.dict(hm.hls_manager._last_seen,
                    {("cam_01", "rgb"): time.time(),
                     ("cam_01", "thermal"): time.time() - 9999},
                    clear=True):
        resp = client.get("/cameras")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cameras"] == ["cam_01"]          # 原形狀不變
    assert data["active_types"] == {"cam_01": ["rgb"]}   # thermal 已過期不算活躍


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

    async def _noop_notify(*a, **k):
        return False
    monkeypatch.setattr(main.ntfy_notifier, "notify", _noop_notify)
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

    async def _noop_notify(*a, **k):
        return False
    monkeypatch.setattr(main.ntfy_notifier, "notify", _noop_notify)
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


def test_push_ntfy_maps_metric_and_calls_notify(monkeypatch):
    import main
    sent = {}

    async def fake_notify(url, title, message, *, priority="default", tags=""):
        sent.update(url=url, title=title, priority=priority, tags=tags)
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)  # 用 app_settings
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)

    asyncio.run(main._push_ntfy("storage_unwritable", 3.0))
    assert sent["url"] == "http://x/pig"
    assert sent["priority"] == "urgent"


def test_push_ntfy_noop_when_disabled(monkeypatch):
    import main
    called = {"n": 0}

    async def fake_notify(*a, **k):
        called["n"] += 1
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", False)
    asyncio.run(main._push_ntfy("storage_unwritable", 3.0))
    assert called["n"] == 0


def test_push_ntfy_revive_body_has_no_disk_suffix(monkeypatch):
    """recording_supervisor_revive 不含 GB 後綴；storage_unwritable 含有。"""
    import main
    sent = []

    async def fake_notify(url, title, message, *, priority="default", tags=""):
        sent.append(message)
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)

    # recording_supervisor_revive 應不含 GB
    asyncio.run(main._push_ntfy("recording_supervisor_revive", 0.0))
    assert len(sent) == 1
    assert "GB" not in sent[0]
    assert "偵測到錄影串流消失" in sent[0]

    # storage_unwritable 應含 GB
    asyncio.run(main._push_ntfy("storage_unwritable", 3.5))
    assert len(sent) == 2
    assert "GB" in sent[1]
    assert "3.5" in sent[1]


def test_push_ntfy_revive_priority_from_app_settings(monkeypatch):
    """無 DB 時，revive 推播優先級取自 app_settings.ntfy_revive_priority。"""
    import main
    sent = {}

    async def fake_notify(url, title, message, *, priority="default", tags=""):
        sent["priority"] = priority
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)
    monkeypatch.setattr(main.app_settings, "ntfy_revive_priority", "default")

    asyncio.run(main._push_ntfy("recording_supervisor_revive", 0.0))
    assert sent["priority"] == "default"   # 不再是寫死的 high


def test_push_ntfy_revive_priority_overridden_by_db(monkeypatch):
    """DB 設定 ntfy_revive_priority 覆蓋 app_settings（前端即時生效）。"""
    import main
    sent = {}

    async def fake_notify(url, title, message, *, priority="default", tags=""):
        sent["priority"] = priority
        return True

    async def fake_get_all_settings(pool):
        return {"ntfy_revive_priority": "low"}

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: object())
    monkeypatch.setattr(main, "get_all_settings", fake_get_all_settings)
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)
    monkeypatch.setattr(main.app_settings, "ntfy_revive_priority", "default")

    asyncio.run(main._push_ntfy("recording_supervisor_revive", 0.0))
    assert sent["priority"] == "low"


def test_push_ntfy_revive_min_suppresses_push(monkeypatch):
    """ntfy_revive_priority='min' → 串流重建事件完全不推播（機器斷線反覆重建不洗版）。
    其他 metric 不受此規則影響。"""
    import main
    calls = {"n": 0}

    async def fake_notify(*a, **k):
        calls["n"] += 1
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)
    monkeypatch.setattr(main.app_settings, "ntfy_revive_priority", "min")

    # revive 在 min → 不推播
    asyncio.run(main._push_ntfy("recording_supervisor_revive", 0.0))
    assert calls["n"] == 0
    # 但其他告警照常推（min 設定只管 revive）
    asyncio.run(main._push_ntfy("storage_unwritable", 1.0))
    assert calls["n"] == 1


def test_storage_loop_helper_sets_inference_active(monkeypatch):
    """抽出的 _apply_gpu_schedule 應依 resolve_gpu_active 設 inference 旗標。"""
    import main
    states = []
    monkeypatch.setattr(main.inference_pipeline, "set_active",
                        lambda v: states.append(v))
    monkeypatch.setattr(main.storage_monitor, "resolve_gpu_active",
                        lambda db, app, now: False)
    main._apply_gpu_schedule(db_settings=None)
    assert states == [False]
