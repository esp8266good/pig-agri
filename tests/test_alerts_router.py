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


# ── 折疊與分頁 ───────────────────────────────────────────────────────
# router 的責任只有一個：一直往回抓原始列，直到湊滿「limit + 1」個折疊群組，
# 才能確定第 limit 個群組已經完整收攏、不會被切到下一頁去變成看起來重複的一條。

_BASE_TS = 1_750_000_000.0


def _raw(alert_id, ts, object_id=3, is_read=False):
    return {"id": alert_id, "camera_id": "cam_01", "object_id": object_id,
            "metric": "activity", "current_value": 12.4, "mean_value": 38.1,
            "std_value": 8.5, "is_read": is_read, "triggered_at_unix": ts}


def test_get_alerts_folds_consecutive_alerts(alert_client):
    rows = [_raw(3, _BASE_TS), _raw(2, _BASE_TS - 3600),
            _raw(1, _BASE_TS - 20 * 3600)]
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=rows):
        resp = alert_client.get("/alerts")
    body = resp.json()
    assert body["total"] == 2
    assert body["alerts"][0]["count"] == 2
    assert body["alerts"][1]["count"] == 1
    assert body["has_more"] is False


def test_get_alerts_keeps_fetching_until_page_is_full(alert_client):
    """原始列滿批但折疊後不夠一頁時，要繼續往回抓，不能就這樣回一個半空的頁。"""
    from routers.alerts import RAW_BATCH
    # 每批 RAW_BATCH 筆全部折成同一條（間隔 1 秒），所以第一批只湊得出 1 個群組。
    batch1 = [_raw(i, _BASE_TS - i) for i in range(RAW_BATCH)]
    batch2 = [_raw(RAW_BATCH + i, _BASE_TS - 10 * 86400 - i) for i in range(2)]
    with patch("routers.alerts.query_health_alerts", new_callable=AsyncMock,
               side_effect=[batch1, batch2]) as mock_q:
        resp = alert_client.get("/alerts?limit=1")
    assert mock_q.await_count == 2, "第一批不夠湊滿 limit+1 個群組，必須再抓一次"
    body = resp.json()
    assert body["total"] == 1
    assert body["has_more"] is True


def test_get_alerts_paging_cursor_points_at_oldest_member_of_last_group(alert_client):
    """cursor 要指向最後一個回傳群組裡「最舊」的那筆，下一頁才不會重複它的成員。"""
    rows = [_raw(3, _BASE_TS), _raw(2, _BASE_TS - 3600),
            _raw(1, _BASE_TS - 20 * 3600)]
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=rows):
        resp = alert_client.get("/alerts?limit=1")
    body = resp.json()
    assert body["has_more"] is True
    assert body["next_before_ts"] == _BASE_TS - 3600
    assert body["next_before_id"] == 2


def test_get_alerts_no_cursor_on_last_page(alert_client):
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=[_raw(1, _BASE_TS)]):
        resp = alert_client.get("/alerts")
    body = resp.json()
    assert body["has_more"] is False
    assert body["next_before_ts"] is None


def test_get_alerts_passes_cursor_through_to_query(alert_client):
    with patch("routers.alerts.query_health_alerts",
               new_callable=AsyncMock, return_value=[]) as mock_q:
        alert_client.get(f"/alerts?before_ts={_BASE_TS}&before_id=9")
    _, kwargs = mock_q.call_args
    assert kwargs["before_ts"] == _BASE_TS
    assert kwargs["before_id"] == 9


# ── 未讀數 ───────────────────────────────────────────────────────────
# badge 原本打 /alerts?unread_only=true 再算 len()，吃的是同一個 limit，
# 所以未讀數其實封頂在 50。這是 bug 不是設計，獨立一支端點修掉。

def test_count_returns_unread_total_beyond_page_limit(alert_client):
    with patch("routers.alerts.count_unread_alerts",
               new_callable=AsyncMock, return_value=137) as mock_c:
        resp = alert_client.get("/alerts/count")
    assert resp.status_code == 200
    assert resp.json()["unread"] == 137
    assert mock_c.call_args.kwargs["camera_id"] is None


def test_count_accepts_camera_filter(alert_client):
    with patch("routers.alerts.count_unread_alerts",
               new_callable=AsyncMock, return_value=4) as mock_c:
        resp = alert_client.get("/alerts/count?camera_id=cam_02")
    assert resp.json()["unread"] == 4
    assert mock_c.call_args.kwargs["camera_id"] == "cam_02"


def test_count_no_pool_returns_503():
    with patch.object(database, "get_pool", return_value=None):
        from routers.alerts import router
        app = FastAPI()
        app.include_router(router)
        resp = TestClient(app).get("/alerts/count")
    assert resp.status_code == 503


# ── 折疊群組的整組操作 ───────────────────────────────────────────────
# 一條通知現在可能代表好幾筆 health_alerts。只標記最新那筆已讀的話，
# 底下的成員仍然未讀，badge 數字不會跟著降，使用者會看到一個永遠清不掉的紅點。

def test_mark_read_many_marks_every_member(alert_client):
    with patch("routers.alerts.mark_alerts_read",
               new_callable=AsyncMock, return_value=3) as mock_m:
        resp = alert_client.put("/alerts/read", json={"ids": [7, 5, 2]})
    assert resp.status_code == 200
    assert resp.json()["updated"] == 3
    assert mock_m.call_args[0][1] == [7, 5, 2]


def test_mark_read_many_rejects_empty_list(alert_client):
    resp = alert_client.put("/alerts/read", json={"ids": []})
    assert resp.status_code == 400


def test_delete_by_ids_removes_every_member(alert_client):
    with patch("routers.alerts.delete_alerts_by_ids",
               new_callable=AsyncMock, return_value=2) as mock_d:
        resp = alert_client.request("DELETE", "/alerts/by-ids", json={"ids": [4, 1]})
    assert resp.status_code == 200
    assert resp.json()["deleted"] == 2
    assert mock_d.call_args[0][1] == [4, 1]


def test_delete_by_ids_rejects_empty_list(alert_client):
    resp = alert_client.request("DELETE", "/alerts/by-ids", json={"ids": []})
    assert resp.status_code == 400
