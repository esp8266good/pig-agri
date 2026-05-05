# Phase 5 — 分析與通知 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 實作 3σ 異常偵測排程、health_alerts DB 寫入、前端豬隻狀態面板（底部 tab）、通知中心、bbox 異常標示（紅框 + 圖示）、Live/VOD 雙模式 anomalyMap 同步。

**Architecture:** asyncio 排程 loop（lifespan task）每 30 分鐘對 `tracking_logs` 跑 3σ 分析，結果寫 `health_alerts` + in-memory `_anomaly_cache`。前端 Live 模式每 30 秒 poll `/alerts/active`，VOD 模式在 `loadVod()` 時一次抓歷史 alerts，由 `onVodTimeUpdate()` 依播放時間更新 `anomalyMap`。`drawBoxes()` 根據 `anomalyMap` 決定 bbox 顏色與圖示。

**Tech Stack:** asyncpg pool、numpy、FastAPI、原生 JS

---

## 檔案清單

| 動作 | 檔案 |
|------|------|
| 修改 | `config.py` |
| 新增 | `analysis/scheduler.py` |
| 修改 | `analysis/__init__.py` |
| 修改 | `db_writer.py` |
| 修改 | `routers/alerts.py` |
| 修改 | `main.py` |
| 修改 | `static/index.html` |
| 新增 | `tests/test_analysis_scheduler.py` |
| 新增 | `tests/test_alerts_router.py` |
| 修改 | `tests/test_db_writer.py` |
| 修改 | `tests/test_main.py` |

---

### Task 1: config.py — 分析視窗設定更新

**Files:**
- Modify: `config.py:46-47`

- [ ] **Step 1: 修改 config.py**

將 `analysis_window_hours: int = 6` 替換為 `analysis_window_minutes: int = 30`：

```python
# config.py 第 46-47 行，替換：
    analysis_interval_minutes: int = 30
    analysis_window_minutes: int = 30    # 取代原本的 analysis_window_hours: int = 6
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50
```

- [ ] **Step 2: 驗證 config 可匯入**

```bash
cd /home/lazoark/OneDrive/Curriculum/pig-agri
uv run python -c "from config import settings; print(settings.analysis_window_minutes)"
```

Expected output: `30`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: replace analysis_window_hours with analysis_window_minutes=30"
```

---

### Task 2: db_writer.py — health alert DB 函式

**Files:**
- Modify: `db_writer.py`
- Modify: `tests/test_db_writer.py`

- [ ] **Step 1: 在 tests/test_db_writer.py 末尾加入 5 個測試**

```python
# 加在 tests/test_db_writer.py 末尾

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
```

- [ ] **Step 2: 確認測試失敗**

```bash
uv run pytest tests/test_db_writer.py::test_write_health_alert_returns_id -v
```

Expected: FAIL with `ImportError` or `AttributeError`

- [ ] **Step 3: 在 db_writer.py 末尾加入三個函式**

```python
# 加在 db_writer.py 末尾

async def write_health_alert(
    pool: asyncpg.Pool,
    *,
    camera_id: str,
    object_id: int,
    metric: str,
    current_value: float,
    mean_value: float,
    std_value: float,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO health_alerts
           (camera_id, object_id, metric, current_value, mean_value, std_value)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        camera_id, object_id, metric, current_value, mean_value, std_value,
    )
    return row["id"]


async def query_health_alerts(
    pool: asyncpg.Pool,
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> list[dict]:
    conditions = []
    params: list = []
    idx = 1

    if camera_id is not None:
        conditions.append(f"camera_id=${idx}")
        params.append(camera_id)
        idx += 1
    if unread_only:
        conditions.append("is_read = FALSE")
    if start_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) >= ${idx}")
        params.append(start_ts)
        idx += 1
    if end_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) < ${idx}")
        params.append(end_ts)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    limit_ph = f"${idx}"

    sql = f"""
        SELECT id, camera_id, object_id, metric,
               current_value, mean_value, std_value, is_read,
               EXTRACT(EPOCH FROM triggered_at)::float AS triggered_at_unix
        FROM health_alerts
        {where}
        ORDER BY triggered_at DESC
        LIMIT {limit_ph}
    """
    rows = await pool.fetch(sql, *params)
    return [dict(r) for r in rows]


async def mark_alert_read(pool: asyncpg.Pool, alert_id: int) -> bool:
    result = await pool.execute(
        "UPDATE health_alerts SET is_read = TRUE WHERE id = $1",
        alert_id,
    )
    return result != "UPDATE 0"
```

- [ ] **Step 4: 執行所有 db_writer 測試**

```bash
uv run pytest tests/test_db_writer.py -v
```

Expected: 全部 PASS（原有 6 個 + 新增 5 個 = 11 個）

- [ ] **Step 5: Commit**

```bash
git add db_writer.py tests/test_db_writer.py
git commit -m "feat: add write_health_alert, query_health_alerts, mark_alert_read to db_writer"
```

---

### Task 3: analysis/scheduler.py — Scheduler 類別

**Files:**
- Create: `analysis/scheduler.py`
- Modify: `analysis/__init__.py`
- Create: `tests/test_analysis_scheduler.py`

- [ ] **Step 1: 建立 tests/test_analysis_scheduler.py**

```python
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
```

- [ ] **Step 2: 確認測試失敗**

```bash
uv run pytest tests/test_analysis_scheduler.py -v 2>&1 | head -20
```

Expected: 全部 FAIL with `ModuleNotFoundError: No module named 'analysis.scheduler'`

- [ ] **Step 3: 建立 analysis/scheduler.py**

```python
import asyncio
import math
import time
from typing import Optional

import numpy as np
from loguru import logger

from db_writer import write_health_alert

_anomaly_cache: dict[str, dict[int, dict]] = {}


def get_anomaly_cache() -> dict:
    return _anomaly_cache


class Scheduler:
    def __init__(self, pool, settings) -> None:
        self._pool = pool
        self._settings = settings
        self._task: Optional[asyncio.Task] = None

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

    async def _loop(self) -> None:
        interval = self._settings.analysis_interval_minutes * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_analysis()
            except Exception:
                logger.exception("Scheduler._run_analysis error")

    async def _rebuild_cache(self) -> None:
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
                cam = row["camera_id"]
                oid = row["object_id"]
                metric = row["metric"]
                entry = _anomaly_cache.setdefault(cam, {}).setdefault(oid, {
                    "activity_anomaly": False, "temp_anomaly": False,
                    "activity_current": None, "activity_mean": None, "activity_std": None,
                    "temp_current": None, "temp_mean": None, "temp_std": None,
                })
                if metric == "activity":
                    entry["activity_anomaly"] = True
                elif metric == "temperature":
                    entry["temp_anomaly"] = True
        except Exception:
            logger.exception("Scheduler._rebuild_cache error")

    async def _run_analysis(self) -> None:
        if self._pool is None:
            return
        now = time.time()
        window_start = now - self._settings.analysis_window_minutes * 60

        rows = await self._pool.fetch(
            """SELECT DISTINCT camera_id, object_id
               FROM tracking_logs
               WHERE timestamp >= $1 AND timestamp < $2""",
            window_start, now,
        )

        for r in rows:
            camera_id = r["camera_id"]
            object_id = r["object_id"]
            logs = await self._pool.fetch(
                """SELECT bb_left, bb_top, bb_width, bb_height, thermal_intensity, timestamp
                   FROM tracking_logs
                   WHERE camera_id=$1 AND object_id=$2
                     AND timestamp >= $3 AND timestamp < $4
                   ORDER BY timestamp""",
                camera_id, object_id, window_start, now,
            )
            if len(logs) < self._settings.anomaly_min_samples:
                continue

            centers = [
                (log["bb_left"] + log["bb_width"] / 2, log["bb_top"] + log["bb_height"] / 2)
                for log in logs
            ]
            displacements = [
                math.hypot(centers[i][0] - centers[i-1][0], centers[i][1] - centers[i-1][1])
                for i in range(1, len(centers))
            ]
            temps = [
                log["thermal_intensity"] for log in logs
                if log["thermal_intensity"] is not None
            ]

            entry = _anomaly_cache.setdefault(camera_id, {}).setdefault(object_id, {
                "activity_anomaly": False, "temp_anomaly": False,
                "activity_current": None, "activity_mean": None, "activity_std": None,
                "temp_current": None, "temp_mean": None, "temp_std": None,
            })

            if len(displacements) >= 2:
                mean_a = float(np.mean(displacements))
                std_a = float(np.std(displacements))
                current_a = displacements[-1]
                entry.update({
                    "activity_current": current_a,
                    "activity_mean": mean_a,
                    "activity_std": std_a,
                })
                if std_a > 0 and current_a < mean_a - self._settings.anomaly_std_threshold * std_a:
                    entry["activity_anomaly"] = True
                    await write_health_alert(
                        self._pool, camera_id=camera_id, object_id=object_id,
                        metric="activity", current_value=current_a,
                        mean_value=mean_a, std_value=std_a,
                    )
                else:
                    entry["activity_anomaly"] = False

            if len(temps) >= 2:
                mean_t = float(np.mean(temps))
                std_t = float(np.std(temps))
                current_t = temps[-1]
                entry.update({
                    "temp_current": current_t,
                    "temp_mean": mean_t,
                    "temp_std": std_t,
                })
                if std_t > 0 and abs(current_t - mean_t) > self._settings.anomaly_std_threshold * std_t:
                    entry["temp_anomaly"] = True
                    await write_health_alert(
                        self._pool, camera_id=camera_id, object_id=object_id,
                        metric="temperature", current_value=current_t,
                        mean_value=mean_t, std_value=std_t,
                    )
                else:
                    entry["temp_anomaly"] = False
```

- [ ] **Step 4: 修改 analysis/__init__.py**

完整替換為：

```python
from analysis.scheduler import Scheduler, get_anomaly_cache

__all__ = ["Scheduler", "get_anomaly_cache"]
```

- [ ] **Step 5: 執行 scheduler 測試**

```bash
uv run pytest tests/test_analysis_scheduler.py -v
```

Expected: 7 個全部 PASS

- [ ] **Step 6: Commit**

```bash
git add analysis/scheduler.py analysis/__init__.py tests/test_analysis_scheduler.py
git commit -m "feat: add Scheduler with 3sigma anomaly detection and in-memory cache"
```

---

### Task 4: routers/alerts.py — 實作真實 endpoints

**Files:**
- Modify: `routers/alerts.py`
- Create: `tests/test_alerts_router.py`

- [ ] **Step 1: 建立 tests/test_alerts_router.py**

```python
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
```

- [ ] **Step 2: 確認測試失敗**

```bash
uv run pytest tests/test_alerts_router.py -v 2>&1 | head -20
```

Expected: FAIL（endpoint 仍回傳 `{"status": "not implemented"}`）

- [ ] **Step 3: 替換 routers/alerts.py 完整內容**

```python
from typing import Optional

from fastapi import APIRouter, HTTPException

import database
from analysis.scheduler import get_anomaly_cache
from db_writer import mark_alert_read, query_health_alerts

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/active")
async def get_active_alerts(camera_id: Optional[str] = None):
    cache = get_anomaly_cache()
    if camera_id is not None:
        return {"cache": {camera_id: {str(k): v for k, v in cache.get(camera_id, {}).items()}}}
    return {"cache": {cam: {str(k): v for k, v in objs.items()} for cam, objs in cache.items()}}


@router.get("")
async def get_alerts(
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    alerts = await query_health_alerts(
        pool,
        camera_id=camera_id,
        unread_only=unread_only,
        limit=limit,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    return {"alerts": alerts, "total": len(alerts)}


@router.put("/{alert_id}/read")
async def mark_read(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    found = await mark_alert_read(pool, alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}
```

- [ ] **Step 4: 執行 alerts router 測試**

```bash
uv run pytest tests/test_alerts_router.py -v
```

Expected: 6 個全部 PASS

- [ ] **Step 5: Commit**

```bash
git add routers/alerts.py tests/test_alerts_router.py
git commit -m "feat: implement /alerts/active, GET /alerts, PUT /alerts/{id}/read"
```

---

### Task 5: main.py + test_main.py — 接入 Scheduler

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: 更新 tests/test_main.py**

在 `test_main.py` 頂端 import 區加入：

```python
from analysis import scheduler as scheduler_mod
```

修改 `client` fixture（在現有的 `with (` 區塊加入兩行 patch）：

```python
@pytest.fixture
def client():
    import inference.pipeline as pipeline_mod
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
```

移除 `test_alerts_returns_stub`，替換為：

```python
def test_alerts_returns_503_when_db_unavailable(client):
    resp = client.get("/alerts")
    assert resp.status_code == 503


def test_alerts_active_returns_empty_cache(client):
    resp = client.get("/alerts/active")
    assert resp.status_code == 200
    assert resp.json() == {"cache": {}}
```

- [ ] **Step 2: 確認測試目前失敗（main.py 還沒 import Scheduler）**

```bash
uv run pytest tests/test_main.py -v 2>&1 | tail -10
```

- [ ] **Step 3: 修改 main.py**

在 `main.py` 頂端 import 區加入：

```python
from analysis.scheduler import Scheduler
```

修改 lifespan 函式：

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    scheduler = Scheduler(database.get_pool(), app_settings)
    await scheduler.start()
    loop = asyncio.get_event_loop()
    inference_pipeline.start(loop)
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    inference_pipeline.stop()
    hls_manager.stop_all()
    await scheduler.stop()
    await database.disconnect()
```

- [ ] **Step 4: 執行完整測試套件**

```bash
uv run pytest -v 2>&1 | tail -20
```

Expected: 全部 PASS（目前 86 + 新增 13 = 約 99 個）

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire Scheduler into lifespan, update test_main for real alerts endpoints"
```

---

### Task 6: static/index.html — anomalyMap 狀態與 drawBoxes 異常標示

**Files:**
- Modify: `static/index.html`

此 task 只改 JavaScript 狀態與 `drawBoxes()`，不動 HTML/CSS。

- [ ] **Step 1: 在 State 區塊加入新狀態變數（在 `let vodDebounceTimer = null;` 之後）**

```javascript
    let anomalyMap = {};           // { object_id: { activity_anomaly, temp_anomaly, ... } }
    let vodAlerts = [];            // VOD 模式下的歷史 alerts
    let liveAnomalyIntervalId = null;
    let currentObjectIds = new Set();  // 最近一次 WS frame 出現的 object_id
```

- [ ] **Step 2: 加入 refreshAnomalyMap 與 updateVodAnomalyMap 函式（在 `// ── Type toggle` 之前）**

```javascript
    // ── Anomaly map ───────────────────────────────────────────
    async function refreshAnomalyMap() {
      if (!currentCamera) return;
      try {
        const data = await fetch(`/alerts/active?camera_id=${currentCamera}`).then(r => r.json());
        const camCache = data.cache?.[currentCamera] ?? {};
        anomalyMap = {};
        for (const [oid, info] of Object.entries(camCache)) {
          anomalyMap[parseInt(oid)] = info;
        }
      } catch (_) {}
    }

    function updateVodAnomalyMap(currentTs) {
      anomalyMap = {};
      for (const alert of vodAlerts) {
        const winStart = alert.triggered_at_unix - 1800;
        const winEnd   = alert.triggered_at_unix;
        if (currentTs >= winStart && currentTs <= winEnd) {
          const entry = anomalyMap[alert.object_id] ?? { activity_anomaly: false, temp_anomaly: false };
          if (alert.metric === 'activity')    entry.activity_anomaly = true;
          if (alert.metric === 'temperature') entry.temp_anomaly = true;
          anomalyMap[alert.object_id] = entry;
        }
      }
    }
```

- [ ] **Step 3: 修改 drawBoxes() — 加入異常顏色與圖示**

找到 `function drawBoxes()` 並完整替換成：

```javascript
    function drawBoxes() {
      const canvas = document.getElementById('overlay');
      const elW = video.offsetWidth  || 1;
      const elH = video.offsetHeight || 1;
      if (canvas.width  !== elW) canvas.width  = elW;
      if (canvas.height !== elH) canvas.height = elH;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, elW, elH);
      const vidW = video.videoWidth;
      const vidH = video.videoHeight;
      if (!vidW || !vidH || !latestBoxes.length) {
        animFrameId = requestAnimationFrame(drawBoxes);
        return;
      }
      const scale   = Math.min(elW / vidW, elH / vidH);
      const renderW = vidW * scale;
      const renderH = vidH * scale;
      const offX = (elW - renderW) / 2;
      const offY = (elH - renderH) / 2;
      const baseColor = getBoxColor();
      ctx.lineWidth = 1.5;
      ctx.font = 'bold 11px "DM Sans", monospace';

      for (const o of latestBoxes) {
        const [x, y, w, h] = o.bbox;
        const px = offX + x * scale;
        const py = offY + y * scale;
        const pw = w * scale;
        const ph = h * scale;
        const anomaly     = anomalyMap[o.object_id];
        const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);
        const color       = isAnomalous ? '#ff4444' : baseColor;

        ctx.strokeStyle = color;
        ctx.fillStyle   = color;
        roundRect(ctx, px, py, pw, ph, 3);
        ctx.stroke();

        // 豬隻 ID 標籤
        const label = `#${o.object_id}`;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = color;
        ctx.fillRect(px - 0.5, py - 16, tw + 6, 15);
        ctx.fillStyle = '#000';
        ctx.fillText(label, px + 2, py - 4);
        ctx.fillStyle = color;

        // 異常圖示（bbox 左下角）
        if (anomaly) {
          let icons = '';
          if (anomaly.activity_anomaly) icons += '⚠';
          if (anomaly.temp_anomaly)     icons += '🌡';
          if (icons) ctx.fillText(icons, px + 2, py + ph - 2);
        }
      }
      animFrameId = requestAnimationFrame(drawBoxes);
    }
```

- [ ] **Step 4: 修改 switchToLive() — 清空 anomalyMap + 啟動 Live poll**

找到 `function switchToLive()` 並完整替換：

```javascript
    function switchToLive() {
      if (isLive) return;
      isLive = true;
      liveBtn.style.display = 'none';
      video.removeEventListener('timeupdate', onVodTimeUpdate);
      clearTimeout(vodDebounceTimer);
      clearInterval(liveAnomalyIntervalId);
      anomalyMap = {};
      vodAlerts  = [];
      latestBoxes = [];
      currentObjectIds.clear();
      document.querySelectorAll('.timeline-slot.selected')
        .forEach(s => s.classList.remove('selected'));
      wsRetryCount = 0;
      loadStream();
      liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
      refreshAnomalyMap();
    }
```

- [ ] **Step 5: 修改 loadVod() — 清空 anomalyMap + 抓 vodAlerts**

在 `function loadVod(startTs)` 內，`clearTimeout(vodDebounceTimer);` 之後加入：

```javascript
      clearInterval(liveAnomalyIntervalId);
      liveAnomalyIntervalId = null;
      anomalyMap = {};
      vodAlerts  = [];
      currentObjectIds.clear();

      // 抓此 VOD 時段的歷史 alerts（含前 30 分鐘）
      const vodEnd = startTs + 3600;
      fetch(`/alerts?camera_id=${currentCamera}&start_ts=${startTs - 1800}&end_ts=${vodEnd + 300}`)
        .then(r => r.json())
        .then(data => { vodAlerts = data.alerts || []; })
        .catch(() => {});
```

- [ ] **Step 6: 修改 onVodTimeUpdate() — 加入 updateVodAnomalyMap 呼叫**

找到 `onVodTimeUpdate` 內的 debounce callback，在 `latestBoxes = data.logs || [];` 之後加入：

```javascript
          currentObjectIds = new Set(latestBoxes.map(o => o.object_id));
          updateVodAnomalyMap(ts);
```

- [ ] **Step 7: 修改 ws.onmessage — 更新 currentObjectIds**

在 `latestBoxes = data.objects || [];` 之後加入：

```javascript
          currentObjectIds = new Set(latestBoxes.map(o => o.object_id));
```

- [ ] **Step 8: 修改 camSelect.addEventListener — 清空 anomalyMap + 重啟 poll**

找到 `camSelect.addEventListener('change', () => {`，在 handler 開頭（`currentCamera = camSelect.value;` 之後）加入：

```javascript
      clearInterval(liveAnomalyIntervalId);
      anomalyMap = {};
      vodAlerts  = [];
      currentObjectIds.clear();
```

並在 `loadStream(); loadTimeline();` 之後加入：

```javascript
      liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
      refreshAnomalyMap();
```

- [ ] **Step 9: 修改 init() — 啟動 Live poll**

在 `loadTimeline();` 之後加入：

```javascript
        liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
        refreshAnomalyMap();
```

- [ ] **Step 10: 確認 Python 測試仍通過**

```bash
uv run pytest -v 2>&1 | tail -5
```

Expected: 全部 PASS

- [ ] **Step 11: Commit**

```bash
git add static/index.html
git commit -m "feat: add anomalyMap with live poll and VOD historical alerts, update drawBoxes with red bbox and anomaly icons"
```

---

### Task 7: static/index.html — 底部面板 UI（豬隻狀態 + 通知中心 + 鈴鐺）

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 在 `<style>` 區塊末尾（`</style>` 之前）加入 CSS**

```css
    /* ── Bell badge ─────────────────────────────────────────── */
    #bell-btn {
      margin-left: auto;
      padding: var(--space-2) var(--space-3);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-full);
      font-size: var(--text-sm);
      color: var(--text-muted);
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: var(--space-1);
      transition: background var(--transition);
    }
    #bell-btn:hover { background: var(--surface-3); }
    #bell-badge {
      background: var(--error);
      color: #fff;
      font-size: 0.7rem;
      font-weight: 700;
      padding: 1px 5px;
      border-radius: var(--radius-full);
      min-width: 16px;
      text-align: center;
    }

    /* ── Bottom panel ──────────────────────────────────────── */
    #bottom-panel {
      width: 100%;
      max-width: 840px;
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      overflow: hidden;
    }
    #tab-bar {
      display: flex;
      border-bottom: 1px solid var(--divider);
      background: var(--surface-2);
    }
    .tab-btn {
      padding: var(--space-2) var(--space-4);
      font-size: var(--text-xs);
      font-weight: 500;
      color: var(--text-muted);
      letter-spacing: 0.04em;
      border-right: 1px solid var(--divider);
      transition: color var(--transition), background var(--transition);
    }
    .tab-btn:hover:not(.active) { color: var(--text); }
    .tab-btn.active { color: var(--accent); background: var(--accent-dim); }
    .tab-content { display: none; }
    .tab-content.active { display: block; }

    /* ── Pig status table ───────────────────────────────────── */
    #pig-status-table {
      width: 100%;
      border-collapse: collapse;
      font-size: var(--text-xs);
    }
    #pig-status-table th {
      padding: var(--space-2) var(--space-3);
      text-align: left;
      color: var(--text-muted);
      font-weight: 500;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      border-bottom: 1px solid var(--divider);
      background: var(--surface-2);
    }
    #pig-status-table td {
      padding: var(--space-2) var(--space-3);
      border-bottom: 1px solid var(--divider);
      color: var(--text);
    }
    .anomaly-row td { background: var(--error-dim); }
    .anomaly-cell { color: var(--error); font-weight: 600; }
    .pig-empty-msg { color: var(--text-faint); font-style: italic; padding: var(--space-3); }

    /* ── Notifications ──────────────────────────────────────── */
    #alert-list {
      list-style: none;
      max-height: 220px;
      overflow-y: auto;
    }
    .alert-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: var(--space-2) var(--space-3);
      border-bottom: 1px solid var(--divider);
      cursor: pointer;
      transition: background var(--transition);
      gap: var(--space-3);
    }
    .alert-item:hover { background: var(--surface-2); }
    .alert-item.unread { border-left: 3px solid var(--error); }
    .alert-info { display: flex; align-items: center; gap: var(--space-3); flex: 1; flex-wrap: wrap; }
    .alert-cam  { font-weight: 600; color: var(--text); font-size: var(--text-xs); }
    .alert-metric { color: var(--error); font-size: var(--text-xs); }
    .alert-time { color: var(--text-faint); font-size: var(--text-xs); }
    .alert-sigma { color: var(--text-muted); font-size: var(--text-xs); }
    .mark-read-btn {
      padding: var(--space-1) var(--space-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      font-size: var(--text-xs);
      color: var(--text-muted);
      background: var(--surface-2);
      flex-shrink: 0;
      white-space: nowrap;
      cursor: pointer;
    }
    .mark-read-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
    .mark-read-btn:disabled { opacity: 0.4; cursor: default; }
    .notif-empty { color: var(--text-faint); font-style: italic; padding: var(--space-3); text-align: center; }
```

- [ ] **Step 2: 在 `<header>` 內加入鈴鐺按鈕**

找到 `</header>` 前（logo div 結束後），加入：

```html
    <button id="bell-btn" onclick="switchTab('notifications'); document.getElementById('bottom-panel').scrollIntoView({behavior:'smooth'})" aria-label="通知中心">
      🔔 <span id="bell-badge" style="display:none">0</span>
    </button>
```

- [ ] **Step 3: 在 `<!-- WS reconnect toast -->` div 之後加入底部面板 HTML**

```html
  <!-- Bottom panel: 豬隻狀態 / 通知中心 -->
  <div id="bottom-panel">
    <div id="tab-bar">
      <button class="tab-btn active" data-tab="pig-status"
              onclick="switchTab('pig-status')">豬隻狀態</button>
      <button class="tab-btn" data-tab="notifications"
              onclick="switchTab('notifications')">通知中心</button>
    </div>

    <div id="tab-pig-status" class="tab-content active">
      <table id="pig-status-table">
        <thead>
          <tr><th>豬隻</th><th>活動量</th><th>體溫</th></tr>
        </thead>
        <tbody id="pig-status-body"></tbody>
      </table>
    </div>

    <div id="tab-notifications" class="tab-content">
      <ul id="alert-list"></ul>
    </div>
  </div>
```

- [ ] **Step 4: 在 DOM refs 區塊加入新的 refs（在 `const timelineBar` 之後）**

```javascript
    const bellBadge      = document.getElementById('bell-badge');
    const pigStatusBody  = document.getElementById('pig-status-body');
    const alertListEl    = document.getElementById('alert-list');
```

- [ ] **Step 5: 加入 switchTab、renderPigStatus、renderNotifications、markAlertRead、refreshNotifications 函式（在 `// ── Anomaly map` 區塊之後）**

```javascript
    // ── Tab panel ─────────────────────────────────────────────
    function switchTab(tabName) {
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
      });
      document.querySelectorAll('.tab-content').forEach(c => {
        c.classList.toggle('active', c.id === `tab-${tabName}`);
      });
    }

    function renderPigStatus() {
      if (!pigStatusBody) return;
      pigStatusBody.innerHTML = '';
      if (currentObjectIds.size === 0) {
        pigStatusBody.innerHTML =
          '<tr><td colspan="3" class="pig-empty-msg">目前無偵測到豬隻</td></tr>';
        return;
      }
      for (const oid of currentObjectIds) {
        const a = anomalyMap[oid] ?? null;
        const actAnomaly  = a?.activity_anomaly ?? false;
        const tempAnomaly = a?.temp_anomaly ?? false;
        const actVal  = a?.activity_current != null ? a.activity_current.toFixed(1) : '—';
        const tempVal = a?.temp_current != null ? a.temp_current.toFixed(1) : '—';
        const row = document.createElement('tr');
        if (actAnomaly || tempAnomaly) row.classList.add('anomaly-row');
        row.innerHTML = `
          <td>#${oid}</td>
          <td class="${actAnomaly ? 'anomaly-cell' : ''}">
            ${actAnomaly ? '⚠ ' : ''}${actVal}
          </td>
          <td class="${tempAnomaly ? 'anomaly-cell' : ''}">
            ${tempAnomaly ? '🌡 ' : ''}${tempVal}
          </td>`;
        pigStatusBody.appendChild(row);
      }
    }

    function renderNotifications(alerts) {
      if (!alertListEl) return;
      alertListEl.innerHTML = '';
      if (!alerts.length) {
        alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
        return;
      }
      for (const alert of alerts) {
        const dt = new Date(alert.triggered_at_unix * 1000)
          .toLocaleString('zh-TW', {year:'numeric',month:'2-digit',day:'2-digit',
                                    hour:'2-digit',minute:'2-digit'});
        const sigma = alert.std_value > 0
          ? ((alert.current_value - alert.mean_value) / alert.std_value).toFixed(1)
          : '—';
        const metricLabel = alert.metric === 'activity' ? '活動量偏低' : '體溫異常';
        const li = document.createElement('li');
        li.className = 'alert-item' + (alert.is_read ? '' : ' unread');
        li.innerHTML = `
          <div class="alert-info">
            <span class="alert-cam">${alert.camera_id} #${alert.object_id}</span>
            <span class="alert-metric">${metricLabel}</span>
            <span class="alert-time">${dt}</span>
            <span class="alert-sigma">偏差 ${sigma}σ</span>
          </div>
          <button class="mark-read-btn"
                  onclick="markAlertRead(${alert.id}, this)"
                  ${alert.is_read ? 'disabled' : ''}>
            ${alert.is_read ? '已讀' : '標記已讀'}
          </button>`;
        li.addEventListener('click', e => {
          if (e.target.classList.contains('mark-read-btn')) return;
          if (alert.camera_id !== currentCamera) {
            camSelect.value = alert.camera_id;
            currentCamera = alert.camera_id;
          }
          loadVod(alert.triggered_at_unix - 1800);
        });
        alertListEl.appendChild(li);
      }
    }

    async function markAlertRead(alertId, btn) {
      try {
        await fetch(`/alerts/${alertId}/read`, { method: 'PUT' });
        btn.textContent = '已讀';
        btn.disabled = true;
        btn.closest('.alert-item').classList.remove('unread');
        const d = await fetch('/alerts?unread_only=true').then(r => r.json());
        const n = (d.alerts || []).length;
        bellBadge.textContent = n;
        bellBadge.style.display = n > 0 ? '' : 'none';
      } catch (_) {}
    }

    async function refreshNotifications() {
      if (!currentCamera) return;
      try {
        const d = await fetch(`/alerts?camera_id=${currentCamera}&limit=50`)
          .then(r => r.json());
        renderNotifications(d.alerts || []);
        const ud = await fetch('/alerts?unread_only=true').then(r => r.json());
        const n = (ud.alerts || []).length;
        bellBadge.textContent = n;
        bellBadge.style.display = n > 0 ? '' : 'none';
      } catch (_) {}
    }
```

- [ ] **Step 6: 在 refreshAnomalyMap 末尾加入 renderPigStatus() 呼叫**

找到 `refreshAnomalyMap` 函式，在 `} catch (_) {}` 之前（anomalyMap 更新完畢後）加入：

```javascript
        renderPigStatus();
```

也在 `updateVodAnomalyMap` 末尾加入：

```javascript
      renderPigStatus();
```

- [ ] **Step 7: 在 camSelect handler 與 init() 加入 refreshNotifications() 呼叫**

在 `camSelect.addEventListener` handler 末尾（`refreshAnomalyMap();` 之後）加入：

```javascript
      refreshNotifications();
```

在 `init()` 的 `refreshAnomalyMap();` 之後加入：

```javascript
        refreshNotifications();
```

- [ ] **Step 8: 確認 Python 測試仍全部通過**

```bash
uv run pytest -v 2>&1 | tail -5
```

Expected: 全部 PASS

- [ ] **Step 9: Commit**

```bash
git add static/index.html
git commit -m "feat: add bottom panel with pig status table, notification center, and bell badge"
```

---

## 完成標準

- `uv run pytest` 全部通過（預計 ~99 個測試）
- `GET /alerts/active` 回傳 `{ "cache": {...} }`
- `GET /alerts` 回傳 `{ "alerts": [...], "total": n }`
- `PUT /alerts/{id}/read` 回傳 `{ "ok": true }` 或 404
- 前端：Live 模式異常豬隻 bbox 變紅 + 顯示 ⚠/🌡 圖示
- 前端：底部面板顯示豬隻狀態表（活動量 + 體溫）
- 前端：通知中心列出 health_alerts，點擊跳轉至對應時段 VOD
- 前端：Header 鈴鐺顯示未讀警示數
- 切換 camera / Live↔VOD 時 anomalyMap 正確清空
