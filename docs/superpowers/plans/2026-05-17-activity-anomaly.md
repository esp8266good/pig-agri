# 活動量異常檢測重做 + 體溫偵測開關 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把失效的活動量告警改成「時間正規化路徑速率 + 同伴中位數比例 + 遲滯狀態機」，並新增前端體溫偵測開關。

**Architecture:** 演算法集中在 `analysis/scheduler.py`：每隻豬在視窗內算 px/s 速率，與同欄中位數比，低於 `median×low_ratio` 開 alert、回升超過 `median×recover_ratio` 解除（遲滯，一隻一次）。`config.py` 新增可線上調的參數，`routers/settings.py` + `static/index.html` 加體溫開關與視窗下拉。

**Tech Stack:** Python 3 / asyncio / asyncpg(mock in tests) / numpy / pytest / FastAPI / 原生 JS。

**設計依據：** `docs/superpowers/specs/2026-05-17-activity-anomaly-design.md`

---

## File Structure

| 檔案 | 責任 | 動作 |
|---|---|---|
| `config.py` | 新增/調整分析設定欄位 | Modify `:94-98` |
| `analysis/scheduler.py` | 速率計算、同伴判定、狀態機、temp 開關、reload、rebuild | 全檔重寫 |
| `tests/test_analysis_scheduler.py` | scheduler 新行為測試（舊測試編碼已移除的行為，整檔重寫） | 全檔重寫 |
| `routers/settings.py` | `temp_anomaly_enabled`/`analysis_window_minutes` 設定鍵 + reload | Modify |
| `tests/test_settings_router.py` | 設定 router 新行為（reload 簽名變更） | Modify |
| `static/index.html` | 視窗下拉 8 選項 + 體溫開關 checkbox + load/save JS | Modify |

`main.py` 不需改：它以 `Scheduler(database.get_pool(), app_settings)` 建構，新欄位由 `config.py` 提供，`__init__` 用 `getattr` 讀取。

---

### Task 1: config.py 新增分析設定欄位

**Files:**
- Modify: `config.py:94-98`

- [ ] **Step 1: 修改設定欄位**

把 `config.py` 第 94–98 行這段：

```python
    # ── 分析排程 ───────────────────────────────────────────────
    analysis_interval_minutes: int = 30
    analysis_window_minutes: int = 30
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50
```

改成：

```python
    # ── 分析排程 ───────────────────────────────────────────────
    analysis_interval_minutes: int = 30
    analysis_window_minutes: int = 60
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50
    # 活動量（同伴相對）參數
    activity_low_ratio: float = 0.3
    activity_recover_ratio: float = 0.5
    activity_abs_floor: float = 2.0
    activity_min_coverage: float = 0.5
    # 體溫異常偵測總開關
    temp_anomaly_enabled: bool = True
```

- [ ] **Step 2: 驗證 import 不壞**

Run: `python -c "from config import Settings; s=Settings.model_construct(); print(Settings.model_fields['activity_low_ratio'].default, Settings.model_fields['temp_anomaly_enabled'].default)"`
Expected: 輸出 `0.3 True`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): 活動量同伴相對參數 + 體溫偵測開關，視窗預設改 60min"
```

---

### Task 2: scheduler.py 重寫（速率 + 同伴中位數 + 狀態機 + temp 開關）

**Files:**
- Modify（全檔重寫）: `analysis/scheduler.py`
- Modify（全檔重寫）: `tests/test_analysis_scheduler.py`

> 舊 `tests/test_analysis_scheduler.py` 斷言的是已被本 spec 移除的行為（`displacements[-1]` 單樣本、`_rebuild_cache` 灌 True、舊 `reload` 簽名），無法保留，整檔以新行為重寫。

- [ ] **Step 1: 寫失敗測試（整檔覆蓋 `tests/test_analysis_scheduler.py`）**

用以下內容**完整覆蓋** `tests/test_analysis_scheduler.py`：

```python
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
    # pig3 被排除：activity_current 應為 None，不應觸發 alert
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
        # 輪1：pig3 低 → alert
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(30.0),
        # 輪2：pig3 回升 rate=3.0 > median(4.0)*0.5=2.0 → 解除
        [{"camera_id": "cam_01", "object_id": 1},
         {"camera_id": "cam_01", "object_id": 2},
         {"camera_id": "cam_01", "object_id": 3}],
        _track(600.0), _track(480.0), _track(360.0),
        # 輪3：pig3 又低 → 新 alert
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
    # 沒有體溫 alert 被寫
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_analysis_scheduler.py -q`
Expected: FAIL（多個 test，因 `Scheduler` 仍是舊實作；如 `AttributeError: _window_minutes` / 斷言不符）

- [ ] **Step 3: 全檔重寫 `analysis/scheduler.py`**

用以下內容**完整覆蓋** `analysis/scheduler.py`：

```python
import asyncio
import math
import time
from collections import defaultdict
from typing import Optional

import numpy as np
from loguru import logger

from db_writer import write_health_alert

_anomaly_cache: dict[str, dict[int, dict]] = {}


def get_anomaly_cache() -> dict:
    return _anomaly_cache


def _default_entry() -> dict:
    return {
        "activity_anomaly": False, "temp_anomaly": False,
        "activity_state": "normal", "temp_state": "normal",
        "activity_current": None, "activity_mean": None, "activity_std": None,
        "temp_current": None, "temp_mean": None, "temp_std": None,
    }


def _activity_rate(logs: list, window_seconds: float, min_coverage: float) -> Optional[float]:
    """視窗內路徑長度 ÷ 時間跨度（px/s）。資料不足回 None。"""
    if len(logs) < 2:
        return None
    centers = [
        (lg["bb_left"] + lg["bb_width"] / 2, lg["bb_top"] + lg["bb_height"] / 2)
        for lg in logs
    ]
    ts = [lg["timestamp"] for lg in logs]
    span = ts[-1] - ts[0]
    if span < 60.0:
        return None
    if window_seconds <= 0 or span / window_seconds < min_coverage:
        return None
    path = sum(
        math.hypot(centers[i][0] - centers[i - 1][0], centers[i][1] - centers[i - 1][1])
        for i in range(1, len(centers))
    )
    return path / span


class Scheduler:
    def __init__(self, pool, settings) -> None:
        self._pool = pool
        self._settings = settings
        self._task: Optional[asyncio.Task] = None
        self._interval: int = settings.analysis_interval_minutes * 60
        self._threshold: float = float(settings.anomaly_std_threshold)
        self._window_minutes: int = int(settings.analysis_window_minutes)
        self._temp_enabled: bool = bool(getattr(settings, "temp_anomaly_enabled", True))
        self._low_ratio: float = float(getattr(settings, "activity_low_ratio", 0.3))
        self._recover_ratio: float = float(getattr(settings, "activity_recover_ratio", 0.5))
        self._abs_floor: float = float(getattr(settings, "activity_abs_floor", 2.0))
        self._min_coverage: float = float(getattr(settings, "activity_min_coverage", 0.5))

    async def start(self) -> None:
        await self._rebuild_cache()
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    def reload(
        self,
        interval_minutes: int,
        std_threshold: float,
        window_minutes: int,
        temp_anomaly_enabled: bool,
    ) -> None:
        self._interval = interval_minutes * 60
        self._threshold = std_threshold
        self._window_minutes = int(window_minutes)
        self._temp_enabled = bool(temp_anomaly_enabled)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._run_analysis()
            except Exception:
                logger.exception("Scheduler._run_analysis error")

    async def _rebuild_cache(self) -> None:
        """重啟：建立 cache 骨架，但 state 一律 normal、旗標 False（不被歷史 alert 閂死）。"""
        if self._pool is None:
            return
        try:
            rows = await self._pool.fetch(
                """SELECT DISTINCT ON (camera_id, object_id, metric)
                   camera_id, object_id, metric
                   FROM health_alerts
                   ORDER BY camera_id, object_id, metric, triggered_at DESC"""
            )
            for row in rows:
                _anomaly_cache.setdefault(row["camera_id"], {}).setdefault(
                    row["object_id"], _default_entry()
                )
        except Exception:
            logger.exception("Scheduler._rebuild_cache error")

    async def _run_analysis(self) -> None:
        if self._pool is None:
            return
        now = time.time()
        window_seconds = self._window_minutes * 60
        window_start = now - window_seconds

        rows = await self._pool.fetch(
            """SELECT DISTINCT camera_id, object_id
               FROM tracking_logs
               WHERE timestamp >= $1 AND timestamp < $2""",
            window_start, now,
        )

        by_cam: dict[str, list] = defaultdict(list)
        for r in rows:
            by_cam[r["camera_id"]].append(r["object_id"])

        for camera_id, object_ids in by_cam.items():
            rates: dict[int, float] = {}
            logs_by_obj: dict[int, list] = {}

            for object_id in object_ids:
                logs = await self._pool.fetch(
                    """SELECT bb_left, bb_top, bb_width, bb_height,
                              thermal_intensity, timestamp
                       FROM tracking_logs
                       WHERE camera_id=$1 AND object_id=$2
                         AND timestamp >= $3 AND timestamp < $4
                       ORDER BY timestamp""",
                    camera_id, object_id, window_start, now,
                )
                logs_by_obj[object_id] = logs
                entry = _anomaly_cache.setdefault(camera_id, {}).setdefault(
                    object_id, _default_entry()
                )
                rate = _activity_rate(logs, window_seconds, self._min_coverage)
                entry["activity_current"] = rate
                if rate is not None:
                    rates[object_id] = rate

            median_rate = (
                float(np.median(list(rates.values()))) if len(rates) >= 2 else None
            )
            herd_ok = median_rate is not None and median_rate >= self._abs_floor

            for object_id in object_ids:
                entry = _anomaly_cache[camera_id][object_id]
                rate = rates.get(object_id)

                if herd_ok and rate is not None:
                    entry["activity_mean"] = median_rate
                    low = rate < median_rate * self._low_ratio
                    recovered = rate > median_rate * self._recover_ratio
                    if entry["activity_state"] == "normal":
                        if low:
                            await write_health_alert(
                                self._pool, camera_id=camera_id, object_id=object_id,
                                metric="activity", current_value=rate,
                                mean_value=median_rate, std_value=0.0,
                            )
                            entry["activity_state"] = "alerted"
                    else:  # alerted
                        if recovered:
                            entry["activity_state"] = "normal"
                    entry["activity_anomaly"] = entry["activity_state"] == "alerted"

                if self._temp_enabled:
                    temps = [
                        lg["thermal_intensity"] for lg in logs_by_obj[object_id]
                        if lg["thermal_intensity"] is not None
                    ]
                    if len(temps) >= self._settings.anomaly_min_samples:
                        mean_t = float(np.mean(temps))
                        std_t = float(np.std(temps))
                        current_t = temps[-1]
                        entry.update({
                            "temp_current": current_t,
                            "temp_mean": mean_t,
                            "temp_std": std_t,
                        })
                        anomalous = std_t > 0 and abs(current_t - mean_t) > self._threshold * std_t
                        if entry["temp_state"] == "normal":
                            if anomalous:
                                await write_health_alert(
                                    self._pool, camera_id=camera_id, object_id=object_id,
                                    metric="temperature", current_value=current_t,
                                    mean_value=mean_t, std_value=std_t,
                                )
                                entry["temp_state"] = "alerted"
                        else:  # alerted
                            if not anomalous:
                                entry["temp_state"] = "normal"
                        entry["temp_anomaly"] = entry["temp_state"] == "alerted"
                else:
                    entry["temp_anomaly"] = False
                    entry["temp_state"] = "normal"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_analysis_scheduler.py -q`
Expected: PASS（全部 11 個 test 綠）

- [ ] **Step 5: Commit**

```bash
git add analysis/scheduler.py tests/test_analysis_scheduler.py
git commit -m "feat(scheduler): 活動量改同伴中位數比例+遲滯狀態機，temp 開關，rebuild 不閂死"
```

---

### Task 3: routers/settings.py 加 temp/window 設定鍵與 reload

**Files:**
- Modify: `routers/settings.py`
- Modify: `tests/test_settings_router.py`

- [ ] **Step 1: 改 reload 呼叫測試（修改 `tests/test_settings_router.py`）**

把 `tests/test_settings_router.py` 最後的 `test_put_settings_triggers_scheduler_reload` 整個函式換成：

```python
def test_put_settings_triggers_scheduler_reload(client_with_pool):
    with patch("routers.settings.get_all_settings", new_callable=AsyncMock) as mock_get, \
         patch("routers.settings.upsert_settings", new_callable=AsyncMock):
        mock_get.return_value = {
            "jpeg_quality": "85",
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
            "jpeg_quality": "85",
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_settings_router.py -q`
Expected: FAIL（`temp_anomaly_enabled` 不在 ALLOWED_KEYS → updated 不含它；no-pool fallback 無這些鍵）

- [ ] **Step 3: 重寫 `routers/settings.py`**

用以下內容**完整覆蓋** `routers/settings.py`：

```python
from fastapi import APIRouter, HTTPException, Request

import database
from config import settings as app_settings
from db_writer import get_all_settings, upsert_settings

router = APIRouter(prefix="/settings", tags=["settings"])

ALLOWED_KEYS = frozenset({
    "jpeg_quality",
    "analysis_interval_minutes",
    "analysis_window_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
    "temp_anomaly_enabled",
})

_RELOAD_KEYS = {
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "analysis_window_minutes",
    "temp_anomaly_enabled",
}


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


@router.get("")
async def get_settings():
    pool = database.get_pool()
    if pool is None:
        return {
            "jpeg_quality":              str(app_settings.jpeg_quality),
            "analysis_interval_minutes": str(app_settings.analysis_interval_minutes),
            "analysis_window_minutes":   str(app_settings.analysis_window_minutes),
            "anomaly_std_threshold":     str(app_settings.anomaly_std_threshold),
            "hls_retention_days":        str(app_settings.hls_retention_days),
            "temp_anomaly_enabled":      str(app_settings.temp_anomaly_enabled).lower(),
        }
    return await get_all_settings(pool)


@router.put("")
async def update_settings(request: Request, body: dict[str, str]):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    if _RELOAD_KEYS & updates.keys():
        current = await get_all_settings(pool)
        request.app.state.scheduler.reload(
            interval_minutes=int(current.get(
                "analysis_interval_minutes", app_settings.analysis_interval_minutes)),
            std_threshold=float(current.get(
                "anomaly_std_threshold", app_settings.anomaly_std_threshold)),
            window_minutes=int(current.get(
                "analysis_window_minutes", app_settings.analysis_window_minutes)),
            temp_anomaly_enabled=_as_bool(current.get(
                "temp_anomaly_enabled", str(app_settings.temp_anomaly_enabled).lower())),
        )
    return {"ok": True, "updated": list(updates.keys())}
```

- [ ] **Step 4: 跑測試確認通過**

Run: `pytest tests/test_settings_router.py -q`
Expected: PASS（含原有 + 3 個新 test）

- [ ] **Step 5: Commit**

```bash
git add routers/settings.py tests/test_settings_router.py
git commit -m "feat(settings): temp_anomaly_enabled/analysis_window_minutes 設定鍵 + reload 新簽名"
```

---

### Task 4: 前端設定面板 — 視窗下拉 + 體溫開關

**Files:**
- Modify: `static/index.html`（settings form HTML `:786-789`、loadSettings JS `:1256-1270`、saveSettings JS `:1272-1278`）

- [ ] **Step 1: 改視窗下拉選項 + 加體溫開關（HTML）**

在 `static/index.html` 找到這段（約 786–789 行）：

```html
          <select id="set-analysis-interval">
            <option value="15">15 分鐘</option>
            <option value="30">30 分鐘</option>
            <option value="60">60 分鐘</option>
          </select>
```

在它所屬 `</div>`（`settings-field`）之後、`異常閾值` 那個 `settings-field` 之前，插入兩個新欄位：

```html
        <div class="settings-field">
          <label for="set-analysis-window">活動量評估視窗</label>
          <select id="set-analysis-window">
            <option value="15">15 分鐘</option>
            <option value="30">30 分鐘</option>
            <option value="60">1 小時</option>
            <option value="120">2 小時</option>
            <option value="180">3 小時</option>
            <option value="240">4 小時</option>
            <option value="300">5 小時</option>
            <option value="360">6 小時</option>
          </select>
        </div>
        <div class="settings-field">
          <label for="set-temp-enabled">體溫異常偵測</label>
          <select id="set-temp-enabled">
            <option value="true">啟用</option>
            <option value="false">停用（Thermal 鏡頭關閉時）</option>
          </select>
        </div>
```

> 用 `<select>` 而非 checkbox：與既有 `.settings-field` 樣式（input/select 已有 CSS）一致，且值天然是 `"true"`/`"false"` 字串，免額外轉換。

- [ ] **Step 2: loadSettings 帶入新欄位（JS）**

在 `static/index.html` `loadSettings()` 內，找到：

```javascript
        const r = document.getElementById('set-hls-retention');
        if (q && data.jpeg_quality !== undefined)              q.value = data.jpeg_quality;
        if (a && data.analysis_interval_minutes !== undefined) a.value = data.analysis_interval_minutes;
        if (t && data.anomaly_std_threshold !== undefined)     t.value = data.anomaly_std_threshold;
        if (r && data.hls_retention_days !== undefined)        r.value = data.hls_retention_days;
```

改成：

```javascript
        const r = document.getElementById('set-hls-retention');
        const w = document.getElementById('set-analysis-window');
        const te = document.getElementById('set-temp-enabled');
        if (q && data.jpeg_quality !== undefined)              q.value = data.jpeg_quality;
        if (a && data.analysis_interval_minutes !== undefined) a.value = data.analysis_interval_minutes;
        if (t && data.anomaly_std_threshold !== undefined)     t.value = data.anomaly_std_threshold;
        if (r && data.hls_retention_days !== undefined)        r.value = data.hls_retention_days;
        if (w && data.analysis_window_minutes !== undefined)   w.value = data.analysis_window_minutes;
        if (te && data.temp_anomaly_enabled !== undefined)
          te.value = String(data.temp_anomaly_enabled).toLowerCase() === 'true' ? 'true' : 'false';
```

- [ ] **Step 3: saveSettings 送出新欄位（JS）**

在 `static/index.html` `saveSettings()` 內，把：

```javascript
      const body = {
        jpeg_quality:              document.getElementById('set-jpeg-quality').value,
        analysis_interval_minutes: document.getElementById('set-analysis-interval').value,
        anomaly_std_threshold:     document.getElementById('set-anomaly-threshold').value,
        hls_retention_days:        document.getElementById('set-hls-retention').value,
      };
```

改成：

```javascript
      const body = {
        jpeg_quality:              document.getElementById('set-jpeg-quality').value,
        analysis_interval_minutes: document.getElementById('set-analysis-interval').value,
        analysis_window_minutes:   document.getElementById('set-analysis-window').value,
        anomaly_std_threshold:     document.getElementById('set-anomaly-threshold').value,
        hls_retention_days:        document.getElementById('set-hls-retention').value,
        temp_anomaly_enabled:      document.getElementById('set-temp-enabled').value,
      };
```

- [ ] **Step 4: 語法檢查**

Run: `node --check static/index.html 2>/dev/null || python -c "import re,sys; s=open('static/index.html').read(); print('script blocks:', s.count('<script'))"`
Expected: 無錯誤輸出（或印出 script blocks 數，表示檔案可讀、無中斷）

- [ ] **Step 5: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 設定面板加活動量視窗下拉(8選項)與體溫偵測開關"
```

---

### Task 5: 全測試套件驗證 + CLAUDE.md 紀錄

**Files:**
- Modify: `CLAUDE.md`（gitignored，**不要 commit / git add**）

- [ ] **Step 1: 跑全測試套件**

Run: `pytest -q`
Expected: 與本次相關的 `test_analysis_scheduler.py`、`test_settings_router.py` 全綠。
既有 `ZMQ_SOURCES` env gap 造成的 collection error（`test_main`/`test_zmq_receiver`/
`test_stream_router` 等 import `main` 時）與 `test_config::test_default_mot_worker_threads`
為**既有問題、非本次回歸**，記錄即可，不在本 plan 範圍。

- [ ] **Step 2: 比對改動前後既有失敗集合**

Run: `git stash list >/dev/null 2>&1; pytest -q 2>&1 | tail -20`
Expected: 失敗/error 清單僅含上述既有項目；無新增由本次改動造成的 fail。若有新 fail → 回到對應 Task 修正。

- [ ] **Step 3: 更新 `CLAUDE.md`（不 commit）**

在 `CLAUDE.md` 適當位置（建議「環境」段之前）新增一段，記錄：
活動量告警原本失效的 4 個 bug、已改為「同伴中位數比例 + 遲滯狀態機 + 涵蓋率門檻」、
新參數（`activity_low_ratio/recover_ratio/abs_floor/min_coverage`、視窗預設 60min、
`temp_anomaly_enabled` 前端開關）、以及「待長時間實測」項目（誤標率、夜間 floor 是否需調、
recover 遲滯是否足夠、ID 跳號殘留是否仍影響）。內容用繁體中文，風格比照檔內既有段落。

> `CLAUDE.md` 在 `.gitignore`：**只編輯、不要 `git add`/commit**。

- [ ] **Step 4: 最終確認**

Run: `git log --oneline -5 && git status --porcelain`
Expected: 4 個 feat commit（Task 1–4）已在；`git status` 僅顯示 `CLAUDE.md`（未追蹤/已修改，不入版控）與本 plan/spec 文件。

---

## Self-Review

**Spec coverage：**
- §1 設定/速率/同伴判定 → Task 1（設定）、Task 2（`_activity_rate`、median、abs_floor、coverage）✓
- §2 狀態機/`_rebuild_cache`/去重/ID 跳號 → Task 2（`activity_state`、recover 遲滯、`_rebuild_cache` 不灌 True、移除脆弱判斷）✓
- §3 體溫開關（後端 config/settings/scheduler、前端） → Task 1/2/3/4 ✓
- §4 錯誤處理（median 空清單、span=0、per-row 防護）/測試清單 → Task 2 程式內 guard + Task 2/3 測試、Task 5 全套件 ✓

**Placeholder scan：** 無 TBD/TODO；每個改碼步驟均附完整程式碼與確切指令。

**Type consistency：** `reload(interval_minutes, std_threshold, window_minutes, temp_anomaly_enabled)` 在 scheduler.py 定義、settings.py 呼叫、兩處測試一致；cache entry 欄位（`activity_state`/`temp_state`/`activity_current`/`activity_mean`）`_default_entry()` 定義後各處沿用一致；前端 element id（`set-analysis-window`/`set-temp-enabled`）HTML 與 load/save JS 一致；設定鍵字串（`analysis_window_minutes`/`temp_anomaly_enabled`）config/router/前端一致。

無缺口。
