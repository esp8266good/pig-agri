import sys
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import contextmanager

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


@contextmanager
def _dummy_zmq_sources():
    from config import ZmqSource, settings as _cfg
    _orig = _cfg.zmq_sources
    _cfg.zmq_sources = [ZmqSource(
        name="t", src_host="127.0.0.1", src_port=5555, src_topic="t", label="cam_01",
    )]
    try:
        yield
    finally:
        _cfg.zmq_sources = _orig


@pytest.fixture
def client():
    import inference.pipeline as pipeline_mod
    import analysis.scheduler as scheduler_mod
    mock_pool = AsyncMock()
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
                c._mock_pool = mock_pool
                yield c


def test_get_segments_ok(client):
    with patch("routers.storage.list_saved_segments", new_callable=AsyncMock) as m:
        m.return_value = [{"id": 1, "camera_id": "cam_01", "hour_ts": 1000, "label": None, "note": None}]
        resp = client.get("/storage/segments?camera_id=cam_01&start_ts=0&end_ts=5000")
    assert resp.status_code == 200
    assert resp.json()["segments"][0]["camera_id"] == "cam_01"


def test_get_segments_unknown_camera_404(client):
    resp = client.get("/storage/segments?camera_id=nope&start_ts=0&end_ts=5000")
    assert resp.status_code == 404


def test_post_segments_creates(client):
    with patch("routers.storage.upsert_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = 5
        resp = client.post("/storage/segments",
                           json={"camera_id": "cam_01", "hours": [3600, 7200], "label": "採血前"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert m.await_count == 2


def test_put_segment_404_when_missing(client):
    with patch("routers.storage.update_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = False
        resp = client.put("/storage/segments/99", json={"label": "x", "note": None})
    assert resp.status_code == 404


def test_delete_segment_ok(client):
    with patch("routers.storage.delete_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = True
        resp = client.delete("/storage/segments/5")
    assert resp.status_code == 200


def test_get_bookmarks_ok(client):
    with patch("routers.storage.list_bookmarks", new_callable=AsyncMock) as m:
        m.return_value = [{"id": 2, "camera_id": "cam_01", "hour_ts": 2000, "label": "x", "note": None}]
        resp = client.get("/storage/bookmarks?camera_id=cam_01")
    assert resp.status_code == 200
    assert resp.json()["bookmarks"][0]["label"] == "x"


def test_recordings_delete_calls_fs_and_db(client):
    with (
        patch("routers.storage.delete_recording_hours") as m_fs,
        patch("routers.storage.delete_recordings_in_range", new_callable=AsyncMock) as m_db,
        patch("routers.storage.delete_saved_segments_by_hours", new_callable=AsyncMock) as m_seg,
    ):
        m_fs.return_value = ["d1", "d2"]
        m_db.return_value = {"tracking_logs": 10, "health_alerts": 2}
        m_seg.return_value = 1
        resp = client.post("/storage/recordings/delete",
                           json={"camera_id": "cam_01", "hours": [3600]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["deleted_hours"] == 1
    assert body["dirs_removed"] == 2
    assert body["tracking_logs"] == 10
    assert body["health_alerts"] == 2
    m_fs.assert_called_once()
    m_db.assert_awaited_once()
    m_seg.assert_awaited_once()


def test_post_segments_misaligned_hours_422(client):
    resp = client.post("/storage/segments",
                       json={"camera_id": "cam_01", "hours": [1000]})  # not multiple of 3600
    assert resp.status_code == 422


def test_recordings_delete_empty_hours_400(client):
    resp = client.post("/storage/recordings/delete",
                       json={"camera_id": "cam_01", "hours": []})
    assert resp.status_code == 400


def test_run_retention_once_skips_when_no_pool(client):
    import asyncio as _a
    import main
    with (
        patch("database.get_pool", return_value=None),
        patch("main.purge_expired_hls") as m_purge,
    ):
        _a.run(main._run_retention_once())
    m_purge.assert_not_called()


def test_run_retention_once_purges_with_protected(client):
    import asyncio as _a
    import main
    with (
        patch("database.get_pool", return_value=object()),
        patch("main.get_all_settings", new_callable=AsyncMock) as m_set,
        patch("main.get_protected_hours", new_callable=AsyncMock) as m_prot,
        patch("main.purge_expired_hls") as m_purge,
    ):
        m_set.return_value = {"hls_retention_days": "30"}
        m_prot.return_value = {("cam_01", 1000)}
        _a.run(main._run_retention_once())
    m_purge.assert_called_once()
    assert m_purge.call_args.kwargs.get("protected") == {("cam_01", 1000)}


def test_run_retention_once_skips_when_db_read_fails(client):
    import asyncio as _a
    import main
    with (
        patch("database.get_pool", return_value=object()),
        patch("main.get_all_settings", new_callable=AsyncMock) as m_set,
        patch("main.purge_expired_hls") as m_purge,
    ):
        m_set.side_effect = RuntimeError("db down")
        _a.run(main._run_retention_once())
    m_purge.assert_not_called()
