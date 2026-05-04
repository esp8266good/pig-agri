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
        camera_id="cam1",
        timestamp=1000.5,
        frame_id=42,
        object_id=10,
        bb_left=10.0,
        bb_top=20.0,
        bb_width=30.0,
        bb_height=40.0,
        confidence=0.95,
        thermal_intensity=35.5,
    ))

    mock_pool.execute.assert_called_once()
    call_args = mock_pool.execute.call_args
    sql = call_args[0][0]
    assert "INSERT INTO tracking_logs" in sql


def test_write_tracking_log_passes_none_thermal(mock_pool):
    """Verify that thermal_intensity=None is passed correctly."""
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
        thermal_intensity=None,
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
