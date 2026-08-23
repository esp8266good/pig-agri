import asyncio
from unittest.mock import AsyncMock
import pytest


@pytest.fixture
def mock_pool():
    return AsyncMock()


def test_write_tracking_log_executes_insert(mock_pool):
    """Verify that write_tracking_log calls pool.execute with INSERT statement."""
    from db_writer import write_tracking_log

    asyncio.run(write_tracking_log(
        mock_pool,
        camera_id="cam_01",
        timestamp=1000.0,
        frame_id=42,
        object_id=10,
        bb_left=10.0,
        bb_top=20.0,
        bb_width=30.0,
        bb_height=40.0,
        confidence=0.95,
        thermal_celsius=38.5,
    ))

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args
    sql = call_args[0][0]
    assert "INSERT INTO tracking_logs" in sql

    # Verify the 10 data parameters (sql + 10 params = 11 positional args)
    args = mock_pool.execute.call_args[0]
    assert len(args) == 11  # sql + 10 params
    assert args[1] == "cam_01"     # camera_id
    assert args[2] == 1000.0       # timestamp
    assert args[-1] == 38.5        # thermal_celsius


def test_write_tracking_log_passes_none_thermal(mock_pool):
    """Verify that thermal_celsius=None is passed correctly."""
    from db_writer import write_tracking_log

    asyncio.run(write_tracking_log(
        mock_pool,
        camera_id="cam1",
        timestamp=1000.5,
        frame_id=42,
        object_id=10,
        bb_left=10.0,
        bb_top=20.0,
        bb_width=30.0,
        bb_height=40.0,
        confidence=0.95,
        thermal_celsius=None,
    ))

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args
    # The last positional argument should be None
    assert call_args[0][-1] is None


def test_query_tracking_logs_returns_formatted_dicts(mock_pool):
    """Verify query_tracking_logs returns formatted dicts with bbox."""
    from db_writer import query_tracking_logs

    # Mock the fetch response
    mock_pool.fetch.return_value = [
        {
            "object_id": 5,
            "bb_left": 10.0,
            "bb_top": 20.0,
            "bb_width": 30.0,
            "bb_height": 40.0,
            "confidence": 0.95,
            "timestamp": 1000.5,
            "frame_id": 42,
        }
    ]

    result = asyncio.run(query_tracking_logs(
        mock_pool,
        camera_id="cam1",
        start=1000.0,
        end=2000.0,
    ))

    assert len(result) == 1
    assert result[0]["object_id"] == 5
    assert result[0]["bbox"] == [10.0, 20.0, 30.0, 40.0]
    assert result[0]["confidence"] == 0.95
    assert result[0]["timestamp"] == 1000.5
    assert result[0]["frame_id"] == 42


def test_query_tracking_logs_with_object_id_uses_fourth_param(mock_pool):
    """Verify that object_id parameter uses $4 in SQL."""
    from db_writer import query_tracking_logs

    mock_pool.fetch.return_value = []

    asyncio.run(query_tracking_logs(
        mock_pool,
        camera_id="cam1",
        start=1000.0,
        end=2000.0,
        object_id=5,
    ))

    mock_pool.fetch.assert_called_once()
    call_args = mock_pool.fetch.call_args
    sql = call_args[0][0]
    assert "$4" in sql

    # Verify object_id is passed as the 4th data parameter
    args = mock_pool.fetch.call_args[0]
    # args[0]=sql, args[1]=camera_id, args[2]=start, args[3]=end, args[4]=object_id
    assert args[4] == 5


def test_query_tracking_logs_without_object_id_omits_fourth_param(mock_pool):
    """Verify that SQL omits $4 when object_id is None."""
    from db_writer import query_tracking_logs

    mock_pool.fetch.return_value = []

    asyncio.run(query_tracking_logs(
        mock_pool,
        camera_id="cam1",
        start=1000.0,
        end=2000.0,
        object_id=None,
    ))

    mock_pool.fetch.assert_called_once()
    call_args = mock_pool.fetch.call_args
    sql = call_args[0][0]
    assert "$4" not in sql
    assert "$3" in sql


def test_query_timeline_hours_returns_int_list(mock_pool):
    """Verify query_timeline_hours returns list of int (Unix hour seconds)."""
    from db_writer import query_timeline_hours

    # Mock the fetch response
    mock_pool.fetch.return_value = [
        {"hour": 1000000},
        {"hour": 1003600},
    ]

    result = asyncio.run(query_timeline_hours(
        mock_pool,
        camera_id="cam1",
        start_ts=1000000.0,
        end_ts=1010000.0,
    ))

    assert isinstance(result, list)
    assert result == [1000000, 1003600]
    assert all(isinstance(h, int) for h in result)


def test_write_health_alert_returns_id(mock_pool):
    from db_writer import write_health_alert
    mock_pool.fetchrow.return_value = {"id": 42}

    result = asyncio.run(write_health_alert(
        mock_pool,
        camera_id="cam_01",
        object_id=3,
        metric="activity",
        current_value=12.4,
        mean_value=38.1,
        std_value=8.5,
    ))

    assert result == 42
    mock_pool.fetchrow.assert_called_once()
    sql = mock_pool.fetchrow.call_args[0][0]
    assert "INSERT INTO health_alerts" in sql


def test_query_health_alerts_returns_list(mock_pool):
    from db_writer import query_health_alerts
    mock_pool.fetch.return_value = [
        {
            "id": 1, "camera_id": "cam_01", "object_id": 3,
            "metric": "activity", "current_value": 12.4,
            "mean_value": 38.1, "std_value": 8.5,
            "is_read": False, "triggered_at_unix": 1746444720.0,
        }
    ]

    result = asyncio.run(query_health_alerts(mock_pool, camera_id="cam_01"))

    assert len(result) == 1
    assert result[0]["camera_id"] == "cam_01"
    assert result[0]["triggered_at_unix"] == 1746444720.0


def test_query_health_alerts_time_filter_uses_extract(mock_pool):
    from db_writer import query_health_alerts
    mock_pool.fetch.return_value = []

    asyncio.run(query_health_alerts(mock_pool, start_ts=1000.0, end_ts=2000.0))

    sql = mock_pool.fetch.call_args[0][0]
    assert "EXTRACT(EPOCH FROM triggered_at)" in sql


def test_mark_alert_read_returns_true_when_found(mock_pool):
    from db_writer import mark_alert_read
    mock_pool.execute.return_value = "UPDATE 1"

    result = asyncio.run(mark_alert_read(mock_pool, 42))

    assert result is True


def test_mark_alert_read_returns_false_when_not_found(mock_pool):
    from db_writer import mark_alert_read
    mock_pool.execute.return_value = "UPDATE 0"

    result = asyncio.run(mark_alert_read(mock_pool, 999))

    assert result is False


def test_get_all_settings_returns_dict(mock_pool):
    from db_writer import get_all_settings
    mock_pool.fetch.return_value = [
        {"key": "jpeg_quality", "value": "85"},
        {"key": "analysis_interval_minutes", "value": "30"},
    ]
    result = asyncio.run(get_all_settings(mock_pool))
    assert result == {"jpeg_quality": "85", "analysis_interval_minutes": "30"}


def test_upsert_settings_calls_executemany(mock_pool):
    from db_writer import upsert_settings
    mock_pool.executemany.return_value = None
    asyncio.run(upsert_settings(mock_pool, {"jpeg_quality": "90", "hls_retention_days": "7"}))
    mock_pool.executemany.assert_called_once()
    sql, pairs = mock_pool.executemany.call_args[0]
    assert "INSERT INTO user_settings" in sql
    assert ("jpeg_quality", "90") in pairs
    assert ("hls_retention_days", "7") in pairs


def test_list_saved_segments_queries_range(mock_pool):
    from db_writer import list_saved_segments
    mock_pool.fetch.return_value = [
        {"id": 1, "camera_id": "cam_01", "hour_ts": 1000, "label": None, "note": None},
    ]
    result = asyncio.run(list_saved_segments(mock_pool, "cam_01", 0, 5000))
    mock_pool.fetch.assert_called_once()
    sql = mock_pool.fetch.call_args[0][0]
    assert "FROM saved_segments" in sql
    assert result[0]["camera_id"] == "cam_01"


def test_list_bookmarks_filters_label_not_null(mock_pool):
    from db_writer import list_bookmarks
    mock_pool.fetch.return_value = [
        {"id": 2, "camera_id": "cam_01", "hour_ts": 2000, "label": "採血前", "note": None},
    ]
    result = asyncio.run(list_bookmarks(mock_pool, "cam_01"))
    sql = mock_pool.fetch.call_args[0][0]
    assert "label IS NOT NULL" in sql
    assert result[0]["label"] == "採血前"


def test_upsert_saved_segment_uses_on_conflict(mock_pool):
    from db_writer import upsert_saved_segment
    mock_pool.fetchrow.return_value = {"id": 7}
    seg_id = asyncio.run(upsert_saved_segment(mock_pool, "cam_01", 3000, label="x", note=None))
    sql = mock_pool.fetchrow.call_args[0][0]
    assert "INSERT INTO saved_segments" in sql
    assert "ON CONFLICT" in sql
    assert seg_id == 7


def test_update_saved_segment_returns_bool(mock_pool):
    from db_writer import update_saved_segment
    mock_pool.execute.return_value = "UPDATE 1"
    assert asyncio.run(update_saved_segment(mock_pool, 7, "new", "note")) is True
    mock_pool.execute.return_value = "UPDATE 0"
    assert asyncio.run(update_saved_segment(mock_pool, 99, "x", None)) is False


def test_delete_saved_segment_returns_bool(mock_pool):
    from db_writer import delete_saved_segment
    mock_pool.execute.return_value = "DELETE 1"
    assert asyncio.run(delete_saved_segment(mock_pool, 7)) is True
    mock_pool.execute.return_value = "DELETE 0"
    assert asyncio.run(delete_saved_segment(mock_pool, 99)) is False


def test_get_protected_hours_returns_set_of_tuples(mock_pool):
    from db_writer import get_protected_hours
    mock_pool.fetch.return_value = [
        {"camera_id": "cam_01", "hour_ts": 1000},
        {"camera_id": "cam_02", "hour_ts": 2000},
    ]
    result = asyncio.run(get_protected_hours(mock_pool))
    assert result == {("cam_01", 1000), ("cam_02", 2000)}


def test_delete_saved_segments_by_hours_uses_any(mock_pool):
    from db_writer import delete_saved_segments_by_hours
    mock_pool.execute.return_value = "DELETE 2"
    n = asyncio.run(delete_saved_segments_by_hours(mock_pool, "cam_01", [1000, 2000]))
    sql = mock_pool.execute.call_args[0][0]
    assert "DELETE FROM saved_segments" in sql
    assert "= ANY(" in sql
    assert n == 2


def test_delete_recordings_in_range_deletes_both_tables(mock_pool):
    from db_writer import delete_recordings_in_range
    mock_pool.execute.side_effect = ["DELETE 12", "DELETE 3"]
    result = asyncio.run(
        delete_recordings_in_range(mock_pool, "cam_01", 1000.0, 4600.0)
    )
    assert mock_pool.execute.call_count == 2
    sqls = [c[0][0] for c in mock_pool.execute.call_args_list]
    assert any("DELETE FROM tracking_logs" in s for s in sqls)
    assert any("DELETE FROM health_alerts" in s for s in sqls)
    assert result == {"tracking_logs": 12, "health_alerts": 3}


# ── 子系統 D:alert 永久刪除 ────────────────────────────────────────────

def test_delete_alert_returns_true_when_found(mock_pool):
    from db_writer import delete_alert
    mock_pool.execute.return_value = "DELETE 1"
    assert asyncio.run(delete_alert(mock_pool, 42)) is True
    sql = mock_pool.execute.call_args[0][0]
    assert "DELETE FROM health_alerts" in sql


def test_delete_alert_returns_false_when_missing(mock_pool):
    from db_writer import delete_alert
    mock_pool.execute.return_value = "DELETE 0"
    assert asyncio.run(delete_alert(mock_pool, 999)) is False


def test_delete_alerts_bulk_default_filters_read_only(mock_pool):
    from db_writer import delete_alerts_bulk
    mock_pool.execute.return_value = "DELETE 7"
    n = asyncio.run(delete_alerts_bulk(mock_pool))
    assert n == 7
    sql = mock_pool.execute.call_args[0][0]
    assert "DELETE FROM health_alerts" in sql
    assert "is_read = TRUE" in sql
    # 預設不帶 camera filter
    args = mock_pool.execute.call_args[0]
    assert len(args) == 1  # 只有 sql


def test_delete_alerts_bulk_with_camera_filter(mock_pool):
    from db_writer import delete_alerts_bulk
    mock_pool.execute.return_value = "DELETE 3"
    n = asyncio.run(delete_alerts_bulk(mock_pool, camera_id="cam_01"))
    assert n == 3
    sql = mock_pool.execute.call_args[0][0]
    assert "is_read = TRUE" in sql
    assert "camera_id =" in sql
    args = mock_pool.execute.call_args[0]
    assert args[1] == "cam_01"


def test_delete_alerts_bulk_read_only_false_drops_filter(mock_pool):
    """read_only=False 顯式覆寫(API 不暴露但函式語意要清楚)。"""
    from db_writer import delete_alerts_bulk
    mock_pool.execute.return_value = "DELETE 10"
    asyncio.run(delete_alerts_bulk(mock_pool, read_only=False))
    sql = mock_pool.execute.call_args[0][0]
    assert "is_read" not in sql


def test_delete_alerts_bulk_empty_result_returns_zero(mock_pool):
    from db_writer import delete_alerts_bulk
    mock_pool.execute.return_value = "DELETE 0"
    assert asyncio.run(delete_alerts_bulk(mock_pool)) == 0
