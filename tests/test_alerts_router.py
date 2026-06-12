import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

import database


@pytest.fixture
def alert_client():
    with patch.object(database, "get_pool", return_value=AsyncMock()):
        from fastapi import FastAPI
        from routers.alerts import router
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)


def test_get_active_all_cameras(alert_client):
    fake_cache = {"cam_01": {3: {"activity_anomaly": True, "temp_anomaly": False,
                                  "activity_current": 12.4, "activity_mean": 38.1,
                                  "activity_std": 8.5, "temp_current": None,
                                  "temp_mean": None, "temp_std": None}}}
    with patch("routers.alerts.get_anomaly_cache", return_value=fake_cache):
        resp = alert_client.get("/alerts/active")
    assert resp.status_code == 200
    data = resp.json()
    assert "cache" in data
    assert "cam_01" in data["cache"]


def test_get_active_single_camera(alert_client):
    fake_cache = {
        "cam_01": {3: {"activity_anomaly": True, "temp_anomaly": False,
                       "activity_current": None, "activity_mean": None, "activity_std": None,
                       "temp_current": None, "temp_mean": None, "temp_std": None}},
        "cam_02": {},
    }
    with patch("routers.alerts.get_anomaly_cache", return_value=fake_cache):
        resp = alert_client.get("/alerts/active?camera_id=cam_01")
    assert resp.status_code == 200
    assert list(resp.json()["cache"].keys()) == ["cam_01"]


def test_get_alerts_returns_list(alert_client):
    fake_alerts = [{"id": 1, "camera_id": "cam_01", "object_id": 3,
                    "metric": "activity", "current_value": 12.4, "mean_value": 38.1,
                    "std_value": 8.5, "is_read": False, "triggered_at_unix": 1746444720.0}]
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=fake_alerts):
        resp = alert_client.get("/alerts?camera_id=cam_01")
    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["alerts"][0]["metric"] == "activity"


def test_get_alerts_unread_only(alert_client):
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=[]) as mock_q:
        resp = alert_client.get("/alerts?unread_only=true")
    assert resp.status_code == 200
    _, kwargs = mock_q.call_args
    assert kwargs["unread_only"] is True


def test_put_alert_read_success(alert_client):
    with patch("routers.alerts.mark_alert_read",
               new_callable=AsyncMock, return_value=True):
        resp = alert_client.put("/alerts/1/read")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_put_alert_read_not_found(alert_client):
    with patch("routers.alerts.mark_alert_read",
               new_callable=AsyncMock, return_value=False):
        resp = alert_client.put("/alerts/999/read")
    assert resp.status_code == 404


# ── 子系統 D:alert 永久刪除 ────────────────────────────────────────────

def test_delete_alert_ok(alert_client):
    with patch("routers.alerts.delete_alert",
               new_callable=AsyncMock, return_value=True):
        resp = alert_client.delete("/alerts/5")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_delete_alert_404_when_missing(alert_client):
    with patch("routers.alerts.delete_alert",
               new_callable=AsyncMock, return_value=False):
        resp = alert_client.delete("/alerts/999")
    assert resp.status_code == 404


def test_delete_alerts_bulk_default_read_only(alert_client):
    with patch("routers.alerts.delete_alerts_bulk",
               new_callable=AsyncMock, return_value=7) as m:
        resp = alert_client.delete("/alerts")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 7}
    kwargs = m.await_args.kwargs
    assert kwargs.get("read_only") is True
    assert kwargs.get("camera_id") is None


def test_delete_alerts_bulk_with_camera(alert_client):
    with patch("routers.alerts.delete_alerts_bulk",
               new_callable=AsyncMock, return_value=3) as m:
        resp = alert_client.delete("/alerts?camera_id=cam_01")
    assert resp.status_code == 200
    kwargs = m.await_args.kwargs
    assert kwargs.get("camera_id") == "cam_01"
