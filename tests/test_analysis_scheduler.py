import asyncio
import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

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
    analysis_window_minutes = 2          # 120s 視窗，方便測試
    anomaly_std_threshold = 1.0
    anomaly_min_samples = 3
    activity_low_ratio = 0.3
    activity_recover_ratio = 0.5
    activity_abs_floor = 2.0
    activity_min_coverage = 0.5
    temp_anomaly_enabled = True


@pytest.fixture(autouse=True)
def clear_cache():
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    yield
    sched_mod._anomaly_cache.clear()


def _log(bb_left, ts, bb_top=0.0, thermal=None):
    return {
        "bb_left": bb_left, "bb_top": bb_top,
        "bb_width": 10.0, "bb_height": 10.0,
        "thermal_intensity": thermal, "timestamp": ts,
    }


def _track(total_px, n=5, span=120.0, thermal=None):
    """產生 n 個點、總位移 total_px、時間跨度 span 的軌跡。"""
    step_px = total_px / (n - 1)
    step_t = span / (n - 1)
    return [_log(i * step_px, i * step_t, thermal=thermal) for i in range(n)]


def test_low_activity_pig_triggers_alert():
    """rates=[5.0,4.0,0.25] → median=4.0, floor=2.0 OK, 0.25 < 4.0*0.3=1.2 → alert."""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is True
    assert cache["cam_01"][3]["activity_state"] == "alerted"
    assert cache["cam_01"][1]["activity_anomaly"] is False
    assert cache["cam_01"][2]["activity_anomaly"] is False
    sql = pool.fetchrow.call_args[0][0]
    assert "INSERT INTO health_alerts" in sql
    assert pool.fetchrow.call_count == 1  # 只有 pig3 一筆


def test_all_resting_no_alert():
    """全欄低速 → median < abs_floor(2.0) → 整欄不標記。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(30.0), _track(24.0), _track(6.0),  # rates 0.25/0.2/0.05, median 0.2 < 2.0
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is False
    pool.fetchrow.assert_not_called()


def test_single_pig_no_baseline_no_alert():
    """通過豬數 < 2 → 無同伴基準 → 不標記。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 7}],
        _track(5.0),
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    assert get_anomaly_cache()["cam_01"][7]["activity_anomaly"] is False
    pool.fetchrow.assert_not_called()


def test_low_coverage_pig_excluded():
    """span 只有 40s < window(120s)*0.5=60s → 該豬被排除（不計入 median、不標記）。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0),
        _track(1.0, span=40.0),  # 低涵蓋率
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_current"] is None
    assert cache["cam_01"][3]["activity_anomaly"] is False


def test_no_duplicate_alert_while_still_low():
    """持續低活動：第二輪不再寫新 alert。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
    ]
    pool.fetchrow.return_value = {"id": 1}
    sch = Scheduler(pool, FakeSettings())

    asyncio.run(sch._run_analysis())
    asyncio.run(sch._run_analysis())

    assert pool.fetchrow.call_count == 1  # 仍只有第一輪那一筆


def test_recovery_then_realert():
    """低→alert；回升→state 清 normal（不寫 DB）；再低→寫新 alert。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(360.0),
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
    ]
    pool.fetchrow.return_value = {"id": 1}
    sch = Scheduler(pool, FakeSettings())

    asyncio.run(sch._run_analysis())
    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_state"] == "alerted"

    asyncio.run(sch._run_analysis())
    assert cache["cam_01"][3]["activity_state"] == "normal"
    assert cache["cam_01"][3]["activity_anomaly"] is False

    asyncio.run(sch._run_analysis())
    assert cache["cam_01"][3]["activity_state"] == "alerted"
    assert pool.fetchrow.call_count == 2  # 輪1 + 輪3，輪2 不寫


def test_temp_anomaly_triggers_when_enabled():
    """thermal 末值大幅偏離 → 體溫 alert（temp_anomaly_enabled 預設 True）。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs = [
        _log(0.0, 0.0, thermal=50.0), _log(0.0, 30.0, thermal=50.0),
        _log(0.0, 60.0, thermal=50.0), _log(0.0, 90.0, thermal=50.0),
        _log(0.0, 120.0, thermal=100.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 5}],
        _track(600.0), logs,
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][5]["temp_anomaly"] is True
    assert cache["cam_01"][5]["temp_state"] == "alerted"


def test_temp_detection_skipped_when_disabled():
    """temp_anomaly_enabled=False → 不算體溫、cache temp 旗標清為 False。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    s = FakeSettings()
    s.temp_anomaly_enabled = False
    pool = AsyncMock()
    logs = [
        _log(0.0, 0.0, thermal=50.0), _log(0.0, 30.0, thermal=50.0),
        _log(0.0, 60.0, thermal=50.0), _log(0.0, 90.0, thermal=50.0),
        _log(0.0, 120.0, thermal=100.0),
    ]
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 5}],
        _track(600.0), logs,
    ]

    asyncio.run(Scheduler(pool, s)._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][5]["temp_anomaly"] is False
    assert cache["cam_01"][5]["temp_state"] == "normal"
    for call in pool.fetchrow.call_args_list:
        assert "temperature" not in str(call)


def test_rebuild_cache_starts_normal_not_latched():
    """重啟：_rebuild_cache 建骨架但 state 一律 normal、旗標 False（不被歷史 alert 閂死）。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.return_value = [
        {"camera_id": "cam_01", "object_id": 3, "metric": "activity"},
        {"camera_id": "cam_01", "object_id": 5, "metric": "temperature"},
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._rebuild_cache())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is False
    assert cache["cam_01"][3]["activity_state"] == "normal"
    assert cache["cam_01"][5]["temp_anomaly"] is False
    assert cache["cam_01"][5]["temp_state"] == "normal"


def test_reload_updates_interval_threshold_window_temp():
    from analysis.scheduler import Scheduler
    pool = AsyncMock()
    sch = Scheduler(pool, FakeSettings())
    assert sch._interval == 30 * 60
    assert sch._threshold == 1.0
    assert sch._window_minutes == 2
    assert sch._temp_enabled is True
    sch.reload(interval_minutes=60, std_threshold=2.5,
               window_minutes=180, temp_anomaly_enabled=False)
    assert sch._interval == 60 * 60
    assert sch._threshold == 2.5
    assert sch._window_minutes == 180
    assert sch._temp_enabled is False
