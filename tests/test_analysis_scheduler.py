import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

# Stub heavy ML modules（analysis/scheduler.py 不直接 import，但 db_writer 可能有間接）
for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


class FakeSettings:
    analysis_interval_minutes = 30
    analysis_window_minutes = 30
    anomaly_min_samples = 3
    anomaly_std_threshold = 1.0


@pytest.fixture(autouse=True)
def clear_cache():
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    yield
    sched_mod._anomaly_cache.clear()


def _make_log(bb_left, bb_top, thermal=None, ts=1.0):
    return {
        "bb_left": bb_left, "bb_top": bb_top,
        "bb_width": 10.0, "bb_height": 10.0,
        "thermal_intensity": thermal, "timestamp": ts,
    }


def test_activity_anomaly_low_triggers_alert():
    """displacement [50,50,0]: mean=33.3 std=23.6, current=0 < mean-1σ=9.7 → ANOMALY"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _make_log(0.0, 0.0, ts=1.0),
        _make_log(50.0, 0.0, ts=2.0),
        _make_log(100.0, 0.0, ts=3.0),
        _make_log(100.0, 0.0, ts=4.0),   # no movement → displacement=0
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        logs,
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is True
    sql = pool.fetchrow.call_args[0][0]
    assert "INSERT INTO health_alerts" in sql


def test_activity_normal_no_alert():
    """displacement [50,50,50]: std=0 → guard std>0 → no anomaly"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _make_log(0.0, 0.0, ts=1.0),
        _make_log(50.0, 0.0, ts=2.0),
        _make_log(100.0, 0.0, ts=3.0),
        _make_log(150.0, 0.0, ts=4.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        logs,
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is False
    pool.fetchrow.assert_not_called()


def test_temp_anomaly_high_triggers_alert():
    """temps [50,50,50,100]: mean=62.5 std=21.65, |100-62.5|=37.5 > 1σ=21.65 → ANOMALY"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _make_log(0.0, 0.0, thermal=50.0, ts=1.0),
        _make_log(0.0, 0.0, thermal=50.0, ts=2.0),
        _make_log(0.0, 0.0, thermal=50.0, ts=3.0),
        _make_log(0.0, 0.0, thermal=100.0, ts=4.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        logs,
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["temp_anomaly"] is True
    assert cache["cam_01"][3]["activity_anomaly"] is False  # same position, std=0


def test_temp_anomaly_low_triggers_alert():
    """temps [100,100,100,50]: mean=87.5, |50-87.5|=37.5 > 1σ → ANOMALY (two-tailed)"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _make_log(0.0, 0.0, thermal=100.0, ts=1.0),
        _make_log(0.0, 0.0, thermal=100.0, ts=2.0),
        _make_log(0.0, 0.0, thermal=100.0, ts=3.0),
        _make_log(0.0, 0.0, thermal=50.0, ts=4.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        logs,
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    assert get_anomaly_cache()["cam_01"][3]["temp_anomaly"] is True


def test_temp_normal_no_alert():
    """temps [50,52,48,51]: std≈1.48, last deviation=0.75 < 1σ → no anomaly"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _make_log(0.0, 0.0, thermal=50.0, ts=1.0),
        _make_log(0.0, 0.0, thermal=52.0, ts=2.0),
        _make_log(0.0, 0.0, thermal=48.0, ts=3.0),
        _make_log(0.0, 0.0, thermal=51.0, ts=4.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        logs,
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    assert get_anomaly_cache()["cam_01"][3]["temp_anomaly"] is False
    pool.fetchrow.assert_not_called()


def test_insufficient_samples_skips():
    """2 rows < anomaly_min_samples=3 → skip, no cache entry"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 3}],
        [_make_log(0.0, 0.0, ts=1.0), _make_log(50.0, 0.0, ts=2.0)],
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert 3 not in cache.get("cam_01", {})
    pool.fetchrow.assert_not_called()


def test_rebuild_cache_sets_anomaly_flags():
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"camera_id": "cam_01", "object_id": 3, "metric": "activity"},
        {"camera_id": "cam_01", "object_id": 5, "metric": "temperature"},
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._rebuild_cache())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is True
    assert cache["cam_01"][3]["temp_anomaly"] is False
    assert cache["cam_01"][5]["temp_anomaly"] is True
    assert cache["cam_01"][5]["activity_anomaly"] is False


def test_scheduler_reload_updates_interval_and_threshold():
    from analysis.scheduler import Scheduler
    pool = AsyncMock()
    scheduler = Scheduler(pool, FakeSettings())
    assert scheduler._interval == 30 * 60
    assert scheduler._threshold == 1.0
    scheduler.reload(interval_minutes=60, std_threshold=2.5)
    assert scheduler._interval == 60 * 60
    assert scheduler._threshold == 2.5
