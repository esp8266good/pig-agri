import asyncio
import sys
import time
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
    activity_min_span_seconds = 60.0     # 絕對門檻；120s 軌跡達標、40s 不達標
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
        "thermal_celsius": thermal, "timestamp": ts,
    }


def _track(total_px, n=5, span=120.0, thermal=None, end_ts=None):
    """產生 n 個點、總位移 total_px、時間跨度 span 的軌跡。

    時間戳要落在真實的 unix 時間軸上、而且結束於 end_ts（預設「現在」）：
    _run_analysis 拿 logs 最後一筆的 timestamp 當 last_seen，再用同一輪的
    window_start 逐出。用 0~120 這種假時間的話，每隻豬都會在寫進 cache 的
    同一輪被判定成「早就消失」而立刻被逐出。
    """
    if end_ts is None:
        end_ts = time.time()
    step_px = total_px / (n - 1)
    step_t = span / (n - 1)
    start_ts = end_ts - span
    return [
        _log(i * step_px, start_ts + i * step_t, thermal=thermal)
        for i in range(n)
    ]


def _thermal_track(temps, span=120.0, end_ts=None):
    """原地不動、只有體溫變化的軌跡。時間戳同樣要落在真實時間軸上，
    理由見 _track。"""
    if end_ts is None:
        end_ts = time.time()
    step_t = span / (len(temps) - 1)
    start_ts = end_ts - span
    return [_log(0.0, start_ts + i * step_t, thermal=t) for i, t in enumerate(temps)]


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
    """span 只有 40s < activity_min_span_seconds(60s) → 資料太少，該豬被排除
    （不計入 median、不標記）。門檻為絕對秒數，與視窗長度無關。"""
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


def test_long_window_does_not_require_window_proportional_span():
    """回歸：視窗放大到 120min，但豬（因 MOT ID 跳號）只被連續追蹤約
    10min。舊實作合格門檻 = 視窗比例（span≥0.5×7200=3600s）→ 全員排除、
    median=None、永不標記（使用者實測「2h 視窗下再也看不到異常」的根因）。
    修正後合格門檻改為絕對 activity_min_span_seconds，10min 即達標 →
    低活動豬照樣被抓出來採血。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache

    class LongWin(FakeSettings):
        analysis_window_minutes = 120          # 7200s 視窗
        activity_min_span_seconds = 60.0       # 絕對門檻，與視窗無關

    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        # 每隻只連續追蹤 600s（遠 < 0.5×7200），但 ≥ 絕對門檻 60s
        _track(6000.0, span=600.0),   # rate 10.0
        _track(4800.0, span=600.0),   # rate  8.0
        _track(300.0, span=600.0),    # rate  0.5 → < median(8.0)*0.3
    ]
    pool.fetchrow.return_value = {"id": 1}

    asyncio.run(Scheduler(pool, LongWin())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_anomaly"] is True
    assert cache["cam_01"][1]["activity_anomaly"] is False
    assert cache["cam_01"][2]["activity_anomaly"] is False


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
    logs = _thermal_track([50.0, 50.0, 50.0, 50.0, 100.0])
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
    logs = _thermal_track([50.0, 50.0, 50.0, 50.0, 100.0])
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


def test_activity_mean_updated_even_when_herd_below_floor():
    """herd gate 失敗時仍應更新 activity_mean（spec §1）；anomaly 維持 False。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(30.0), _track(24.0), _track(6.0),  # rates≈0.25/0.2/0.05, median≈0.2 < abs_floor 2.0
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_mean"] == pytest.approx(0.2, abs=0.05)
    assert cache["cam_01"][3]["activity_anomaly"] is False


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


def test_temp_state_recovers_to_normal():
    """体溫 alerted → 第二輪數值平穩 → temp_state 回 normal、temp_anomaly False。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    logs_anomalous = _thermal_track([50.0, 50.0, 50.0, 50.0, 100.0])
    logs_steady = _thermal_track([50.0, 50.0, 50.0, 50.0, 50.0])
    distinct_rows = [
        {"camera_id": "cam_01", "object_id": 1},
        {"camera_id": "cam_01", "object_id": 5},
    ]
    pool.fetch.side_effect = [
        distinct_rows, _track(600.0), logs_anomalous,
        distinct_rows, _track(600.0), logs_steady,
    ]
    pool.fetchrow.return_value = {"id": 1}
    sch = Scheduler(pool, FakeSettings())

    asyncio.run(sch._run_analysis())
    cache = get_anomaly_cache()
    assert cache["cam_01"][5]["temp_state"] == "alerted"
    assert cache["cam_01"][5]["temp_anomaly"] is True

    asyncio.run(sch._run_analysis())
    assert cache["cam_01"][5]["temp_state"] == "normal"
    assert cache["cam_01"][5]["temp_anomaly"] is False


def _stale_entry(temp_anomaly: bool, temp_state: str) -> dict:
    return {
        "activity_anomaly": False, "temp_anomaly": temp_anomaly,
        "activity_state": "normal", "temp_state": temp_state,
        "activity_current": None, "activity_mean": None, "activity_std": None,
        "temp_current": None, "temp_mean": None, "temp_std": None,
        "last_seen": None,
    }


def test_disabled_temp_evicts_stale_cache_entry_outside_window():
    """根因 A：temp 停用時，不在當前分析視窗的殘留 object_id 不得繼續回傳舊紅框。

    以前的做法是把它的 temp 旗標清掉、entry 留著；現在整個 entry 會被逐出
    （視窗內沒出現過＝這個 id 已經不存在了，見 _prune_stale）。逐出比清旗更強，
    要驗的那件事——/alerts/active 不再帶著它——照樣成立。
    """
    from analysis.scheduler import Scheduler, get_anomaly_cache
    import analysis.scheduler as sched_mod
    s = FakeSettings()
    s.temp_anomaly_enabled = False
    sched_mod._anomaly_cache.setdefault("cam_01", {})[99] = _stale_entry(True, "alerted")
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 1}],
        _track(600.0),
    ]

    asyncio.run(Scheduler(pool, s)._run_analysis())

    cache = get_anomaly_cache()
    assert 99 not in cache["cam_01"]


def test_prune_evicts_vanished_object_ids_but_keeps_live_ones():
    """MOT 的 ID 跳號會讓舊 object_id 永遠不再出現。留著它們的話關注清單
    只會長不會縮，畫面上沒有那隻豬、清單卻一直指著牠。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    # 7 是這一輪視窗裡真的有資料的豬；99 是上一輪留下來、已經消失的舊 id。
    sched_mod._anomaly_cache.setdefault("cam_01", {})[99] = _stale_entry(True, "alerted")
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 7}],
        _track(600.0),
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()
    assert 7 in cache["cam_01"]
    assert 99 not in cache["cam_01"]


def test_prune_does_not_reset_state_of_still_present_pig():
    """逐出的判準是『這個 id 還在不在』，不是『時間到了就整批清空』。

    定時清空會把遲滯狀態機一起重置：真正持續低活動的豬每一輪都會被當成新的
    異常重新告警，推播一直響。所以還出現在視窗裡的豬，狀態必須原封不動。
    """
    from analysis.scheduler import Scheduler, get_anomaly_cache
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    entry = _stale_entry(False, "normal")
    entry["activity_state"] = "alerted"
    entry["activity_anomaly"] = True
    sched_mod._anomaly_cache.setdefault("cam_01", {})[7] = entry
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 7}],
        _track(600.0),
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    # 只有一隻豬 → median 算不出來 → herd_ok False → 狀態機整段跳過，維持 alerted。
    assert get_anomaly_cache()["cam_01"][7]["activity_state"] == "alerted"


def test_camera_state_records_no_data_round():
    """視窗內一筆偵測都沒有的相機（夜間全黑、相機斷線）也要記下狀態。

    entry 會被逐出到一個不剩，這時光看 cache 分不出『分析過但全欄休息』與
    『還沒分析過』，關注清單會把一個保護講成一份保證。"""
    from analysis.scheduler import Scheduler, get_camera_state
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    sched_mod._camera_state.clear()
    sched_mod._anomaly_cache["cam_01"] = {5: _stale_entry(False, "normal")}
    pool = AsyncMock()
    pool.fetch.side_effect = [[]]      # 視窗內沒有任何 tracking_logs

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    state = get_camera_state()["cam_01"]
    assert state["analyzed"] is True
    assert state["herd_ok"] is False


def test_reload_disable_temp_clears_all_cache_flags():
    """根因 A（延遲面）：reload 關閉 temp 時立即清掉整個 cache 的 temp 旗標，
    不必等下一個 _interval tick。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.setdefault("cam_01", {})[42] = _stale_entry(True, "alerted")
    sch = Scheduler(AsyncMock(), FakeSettings())

    sch.reload(interval_minutes=30, std_threshold=1.0,
               window_minutes=2, temp_anomaly_enabled=False)

    cache = get_anomaly_cache()
    assert cache["cam_01"][42]["temp_anomaly"] is False
    assert cache["cam_01"][42]["temp_state"] == "normal"


def test_start_applies_db_persisted_temp_setting(monkeypatch):
    """根因 C：啟動時 DB 持久化設定應覆蓋建構時的 config 預設
    （重啟後不可靜默重啟體溫偵測）。"""
    from analysis.scheduler import Scheduler
    pool = AsyncMock()

    async def fake_get_all_settings(_pool):
        return {"temp_anomaly_enabled": "false"}

    monkeypatch.setattr("db_writer.get_all_settings", fake_get_all_settings)
    sch = Scheduler(pool, FakeSettings())  # FakeSettings.temp_anomaly_enabled = True
    assert sch._temp_enabled is True

    asyncio.run(sch._apply_db_settings())

    assert sch._temp_enabled is False


def test_alerted_pig_stays_flagged_when_herd_unmeasurable():
    """pig 3 先 alerted；第二輪全欄低速 herd_ok=False → pig 3 警報不被清除（採血意圖保留）。"""
    from analysis.scheduler import Scheduler, get_anomaly_cache
    pool = AsyncMock()
    distinct_rows_r1 = [
        {"camera_id": "cam_01", "object_id": 1},
        {"camera_id": "cam_01", "object_id": 2},
        {"camera_id": "cam_01", "object_id": 3},
    ]
    distinct_rows_r2 = [
        {"camera_id": "cam_01", "object_id": 1},
        {"camera_id": "cam_01", "object_id": 2},
        {"camera_id": "cam_01", "object_id": 3},
    ]
    pool.fetch.side_effect = [
        distinct_rows_r1, _track(600.0), _track(480.0), _track(30.0),
        distinct_rows_r2, _track(30.0), _track(24.0), _track(6.0),
    ]
    pool.fetchrow.return_value = {"id": 1}
    sch = Scheduler(pool, FakeSettings())

    asyncio.run(sch._run_analysis())
    cache = get_anomaly_cache()
    assert cache["cam_01"][3]["activity_state"] == "alerted"
    assert cache["cam_01"][3]["activity_anomaly"] is True
    assert pool.fetchrow.call_count == 1

    asyncio.run(sch._run_analysis())
    # herd_ok=False（全欄 rates median≈0.2 < abs_floor 2.0）→ 不進 activity 狀態機 → 警報保留
    assert cache["cam_01"][3]["activity_state"] == "alerted"
    assert cache["cam_01"][3]["activity_anomaly"] is True
    assert pool.fetchrow.call_count == 1  # 第二輪不寫新 alert


def test_last_seen_records_actual_last_appearance_not_analysis_time():
    """last_seen 要寫「這個 id 最後一次真的出現」，不是「最後一次被分析到」。

    寫分析當下的時間的話，一個在視窗最前緣出現一次就死掉的 id，下一輪仍然
    落在 [window_start, now] 裡面，要多撐 1~2 輪才逐得掉；而且這個欄位就
    沒辦法回答「牠離開畫面多久了」——關注清單要靠它把死掉的編號降級。
    """
    from analysis.scheduler import Scheduler, get_anomaly_cache
    import analysis.scheduler as sched_mod
    sched_mod._anomaly_cache.clear()
    now = time.time()
    # 兩隻都在 120s 視窗內，但 8 號的軌跡在 30 秒前就結束了。
    fresh = _track(600.0, end_ts=now)
    gone  = _track(480.0, end_ts=now - 30.0)
    pool = AsyncMock()
    pool.fetch.side_effect = [
        [{"camera_id": "cam_01", "object_id": 7},
         {"camera_id": "cam_01", "object_id": 8}],
        fresh, gone,
    ]

    asyncio.run(Scheduler(pool, FakeSettings())._run_analysis())

    cache = get_anomaly_cache()["cam_01"]
    assert cache[7]["last_seen"] == pytest.approx(fresh[-1]["timestamp"])
    assert cache[8]["last_seen"] == pytest.approx(gone[-1]["timestamp"])
    # 兩者相差就是那 30 秒；寫 now 的舊做法會讓它們一模一樣。
    assert cache[7]["last_seen"] - cache[8]["last_seen"] == pytest.approx(30.0)
