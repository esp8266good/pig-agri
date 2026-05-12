# Phase 6：設定頁面 + VOD/BBox 修復 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修復 VOD 回放（時區 bug + bbox 不同步），並實作設定頁面（GET/PUT /settings + Scheduler 熱重載 + 前端設定 UI）。

**Architecture:** vod_generator 改用本地時間對齊 hls_manager；新增磁碟掃描 timeline endpoint 取代 DB 查詢；Scheduler 新增 `_interval`/`_threshold` 實例變數供 `reload()` 更新；settings router 透過 `request.app.state.scheduler` 觸發熱重載；前端新增設定 Tab。

**Tech Stack:** FastAPI, asyncpg, Python datetime（本地時間）, vanilla JS

---

## 檔案結構

| 檔案 | 異動 |
|------|------|
| `vod_generator.py` | 修改：UTC → 本地時間，PDT 格式加時區偏移 |
| `tests/test_vod_generator.py` | 修改：`_make_hour_dir` 改本地時間，新增 PDT 測試 |
| `routers/stream.py` | 修改：新增 `GET /stream/{camera_id}/timeline` |
| `tests/test_stream_router.py` | 修改：新增 timeline 測試 |
| `db_writer.py` | 修改：新增 `get_all_settings`、`upsert_settings` |
| `analysis/scheduler.py` | 修改：新增 `_interval`/`_threshold` 實例變數 + `reload()` |
| `tests/test_db_writer.py` | 修改：新增 2 個 settings DB 測試 |
| `tests/test_analysis_scheduler.py` | 修改：新增 reload 測試 |
| `main.py` | 修改：`app.state.scheduler = scheduler` |
| `routers/settings.py` | 修改：實作 GET + PUT |
| `tests/test_settings_router.py` | 新增 |
| `static/index.html` | 修改：timeline endpoint、pickClosestFrame、switchToLive、設定 UI |

---

### Task 1: vod_generator.py — 修正本地時間

**Files:**
- Modify: `vod_generator.py`
- Modify: `tests/test_vod_generator.py`

- [ ] **Step 1: 執行現有測試確認目前通過**

```bash
pytest tests/test_vod_generator.py -v
```
Expected: 6 tests PASS（目前用 UTC，等等修改後這些會需要更新）

- [ ] **Step 2: 更新 `tests/test_vod_generator.py` 改用本地時間**

完整替換 `tests/test_vod_generator.py`：

```python
# tests/test_vod_generator.py
from datetime import datetime
from pathlib import Path
import re
import pytest

HOUR_TS = 1746403200  # 2026-05-05 00:00:00 UTC


def _make_hour_dir(base: Path, camera_id: str, stream_type: str, hour_ts: int) -> Path:
    dt = datetime.fromtimestamp(hour_ts)  # local time, matches hls_manager._hour_dir()
    dir_name = dt.strftime("%Y-%m-%d-%H")
    hour_dir = base / camera_id / stream_type / dir_name
    hour_dir.mkdir(parents=True, exist_ok=True)
    return hour_dir


def _write_m3u8(hour_dir: Path, segment_count: int = 3, duration: float = 4.0) -> None:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(segment_count):
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"seg_{i:03d}.ts")
    (hour_dir / "index.m3u8").write_text("\n".join(lines) + "\n")


def test_returns_none_when_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is None


def test_returns_m3u8_string_with_required_tags(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is not None
    assert "#EXTM3U" in result
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in result
    assert "#EXT-X-PROGRAM-DATE-TIME:" in result
    assert "#EXT-X-ENDLIST" in result


def test_pdt_tag_contains_timezone_offset(tmp_path, monkeypatch):
    """PDT must use RFC 3339 offset (+HH:MM / -HH:MM), not bare 'Z'."""
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=1)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert re.search(
        r'#EXT-X-PROGRAM-DATE-TIME:\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}',
        result,
    )


def test_segment_urls_use_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=2)
    dt = datetime.fromtimestamp(HOUR_TS)  # local time
    dir_name = dt.strftime("%Y-%m-%d-%H")
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert f"/stream/hls/cam_01/rgb/{dir_name}/seg_000.ts" in result


def test_filters_segments_before_start_ts(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3, duration=4.0)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS + 4), float(HOUR_TS + 3600))
    assert result is not None
    assert "seg_000.ts" not in result
    assert "seg_001.ts" in result
    assert "seg_002.ts" in result


def test_spans_multiple_hour_directories(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour1_ts = HOUR_TS
    hour2_ts = HOUR_TS + 3600
    for hour_ts in [hour1_ts, hour2_ts]:
        _make_hour_dir(tmp_path, "cam_01", "rgb", hour_ts)
        hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", hour_ts)
        _write_m3u8(hour_dir, segment_count=1)
    dt1 = datetime.fromtimestamp(hour1_ts)
    dt2 = datetime.fromtimestamp(hour2_ts)
    result = build_vod_m3u8("cam_01", "rgb", float(hour1_ts), float(hour2_ts + 3600))
    assert result is not None
    assert dt1.strftime("%Y-%m-%d-%H") in result
    assert dt2.strftime("%Y-%m-%d-%H") in result


def test_target_duration_taken_from_m3u8(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    (hour_dir / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.000000,\nseg_000.ts\n"
    )
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert "#EXT-X-TARGETDURATION:6" in result
```

- [ ] **Step 3: 執行更新後的測試，確認 FAIL（因為 vod_generator 還在用 UTC）**

```bash
pytest tests/test_vod_generator.py -v
```
Expected: 多數測試 FAIL（`test_pdt_tag_contains_timezone_offset` 新加的應 FAIL，其他可能 FAIL 或 PASS 取決於系統時區）

- [ ] **Step 4: 修改 `vod_generator.py`，改用本地時間**

完整替換 `vod_generator.py`：

```python
# vod_generator.py
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings


def build_vod_m3u8(
    camera_id: str,
    stream_type: str,
    start_ts: float,
    end_ts: float,
) -> Optional[str]:
    base = Path(settings.hls_base_dir)
    start_hour = int(start_ts // 3600) * 3600
    end_hour = int(end_ts // 3600) * 3600

    all_segments: list[tuple[float, float, str]] = []
    max_target_duration = 4

    current_hour = start_hour
    while current_hour <= end_hour:
        dt = datetime.fromtimestamp(current_hour)  # local time, matches hls_manager._hour_dir()
        dir_name = dt.strftime("%Y-%m-%d-%H")
        m3u8_path = base / camera_id / stream_type / dir_name / "index.m3u8"
        if m3u8_path.exists():
            segs, td = _parse_hour_m3u8(m3u8_path, current_hour, camera_id, stream_type, dir_name)
            all_segments.extend(segs)
            max_target_duration = max(max_target_duration, td)
        current_hour += 3600

    in_range = [
        (ts, dur, url) for ts, dur, url in all_segments
        if ts >= start_ts and ts < end_ts
    ]
    if not in_range:
        return None

    first_ts = in_range[0][0]
    first_dt_local = datetime.fromtimestamp(first_ts).astimezone()
    tz_str = first_dt_local.strftime("%z")        # e.g. "+0800"
    tz_fmt = tz_str[:3] + ":" + tz_str[3:]        # e.g. "+08:00"
    pdt = first_dt_local.strftime("%Y-%m-%dT%H:%M:%S") + tz_fmt

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max_target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-PROGRAM-DATE-TIME:{pdt}",
    ]
    for _ts, dur, url in in_range:
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _parse_hour_m3u8(
    m3u8_path: Path,
    hour_unix: int,
    camera_id: str,
    stream_type: str,
    dir_name: str,
) -> tuple[list[tuple[float, float, str]], int]:
    text = m3u8_path.read_text()

    td_match = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
    target_duration = int(td_match.group(1)) if td_match else 4

    segments: list[tuple[float, float, str]] = []
    accumulated = 0.0
    for m in re.finditer(r"#EXTINF:([\d.]+),[^\r\n]*\r?\n([^\r\n]+)", text):
        duration = float(m.group(1))
        filename = m.group(2).strip()
        seg_start = float(hour_unix) + accumulated
        url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{filename}"
        segments.append((seg_start, duration, url))
        accumulated += duration

    return segments, target_duration
```

- [ ] **Step 5: 執行測試，確認全部通過**

```bash
pytest tests/test_vod_generator.py -v
```
Expected: 7 tests PASS（包含新增的 `test_pdt_tag_contains_timezone_offset`）

- [ ] **Step 6: Commit**

```bash
git add vod_generator.py tests/test_vod_generator.py
git commit -m "fix: align vod_generator to local time, matching hls_manager directory naming"
```

---

### Task 2: routers/stream.py — 新增 HLS timeline endpoint

**Files:**
- Modify: `routers/stream.py`
- Modify: `tests/test_stream_router.py`

- [ ] **Step 1: 在 `tests/test_stream_router.py` 末尾新增三個 timeline 測試**

在 `test_vod_returns_404_for_unknown_camera` 之後加入：

```python
def test_hls_timeline_returns_matching_hours(tmp_path, monkeypatch):
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    monkeypatch.setattr("routers.stream.settings.camera_topics", ["cam_01"])
    (tmp_path / "cam_01" / "rgb" / "2026-05-05-10").mkdir(parents=True)
    (tmp_path / "cam_01" / "rgb" / "2026-05-05-11").mkdir(parents=True)
    from datetime import datetime
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.stream import router as stream_router
    app = FastAPI()
    app.include_router(stream_router)
    c = TestClient(app)
    ts10 = int(datetime.strptime("2026-05-05-10", "%Y-%m-%d-%H").timestamp())
    ts11 = int(datetime.strptime("2026-05-05-11", "%Y-%m-%d-%H").timestamp())
    resp = c.get(f"/stream/cam_01/timeline?start_ts={ts10}&end_ts={ts11 + 3600}")
    assert resp.status_code == 200
    hours = resp.json()["hours"]
    assert ts10 in hours
    assert ts11 in hours


def test_hls_timeline_empty_when_no_directories(tmp_path, monkeypatch):
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    monkeypatch.setattr("routers.stream.settings.camera_topics", ["cam_01"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.stream import router as stream_router
    app = FastAPI()
    app.include_router(stream_router)
    c = TestClient(app)
    resp = c.get("/stream/cam_01/timeline?start_ts=0&end_ts=3600")
    assert resp.status_code == 200
    assert resp.json()["hours"] == []


def test_hls_timeline_unknown_camera_returns_404(tmp_path, monkeypatch):
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    monkeypatch.setattr("routers.stream.settings.camera_topics", ["cam_01"])
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from routers.stream import router as stream_router
    app = FastAPI()
    app.include_router(stream_router)
    c = TestClient(app)
    resp = c.get("/stream/unknown_cam/timeline?start_ts=0&end_ts=3600")
    assert resp.status_code == 404
```

- [ ] **Step 2: 執行確認 FAIL**

```bash
pytest tests/test_stream_router.py::test_hls_timeline_returns_matching_hours \
       tests/test_stream_router.py::test_hls_timeline_empty_when_no_directories \
       tests/test_stream_router.py::test_hls_timeline_unknown_camera_returns_404 -v
```
Expected: 3 tests FAIL（endpoint 不存在）

- [ ] **Step 3: 在 `routers/stream.py` 新增 timeline endpoint**

在現有 import 區段加上 `from datetime import datetime`，並在 `get_vod_stream` 之後新增：

```python
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from config import settings
from hls_manager import hls_manager
from vod_generator import build_vod_m3u8

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/hls/{camera_id}/{stream_type}/{date_hour}/{filename}")
async def serve_hls(
    camera_id: str, stream_type: str, date_hour: str, filename: str
):
    base = Path(settings.hls_base_dir).resolve()
    file_path = (base / camera_id / stream_type / date_hour / filename).resolve()
    if not file_path.is_relative_to(base) or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@router.get("/{camera_id}/live")
async def get_live_stream(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in settings.camera_topics:
        raise HTTPException(status_code=404, detail="Camera not found")
    out_dir = hls_manager.ensure_started(camera_id, stream_type)
    return {
        "url": f"/stream/hls/{camera_id}/{stream_type}/{out_dir.name}/index.m3u8"
    }


@router.get("/{camera_id}/vod")
async def get_vod_stream(
    camera_id: str,
    start: float,
    end: float,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in settings.camera_topics:
        raise HTTPException(status_code=404, detail="Camera not found")
    m3u8 = build_vod_m3u8(camera_id, stream_type, start, end)
    if m3u8 is None:
        raise HTTPException(status_code=404, detail="No segments found for this time range")
    return PlainTextResponse(m3u8, media_type="application/vnd.apple.mpegurl")


@router.get("/{camera_id}/timeline")
async def get_hls_timeline(
    camera_id: str,
    start_ts: float,
    end_ts: float,
):
    if camera_id not in settings.camera_topics:
        raise HTTPException(status_code=404, detail="Camera not found")
    rgb_dir = Path(settings.hls_base_dir) / camera_id / "rgb"
    hours: list[int] = []
    if rgb_dir.is_dir():
        for child in rgb_dir.iterdir():
            if not child.is_dir():
                continue
            try:
                dt = datetime.strptime(child.name, "%Y-%m-%d-%H")
                hour_ts = int(dt.timestamp())
                if start_ts <= hour_ts < end_ts:
                    hours.append(hour_ts)
            except ValueError:
                continue
    hours.sort()
    return {"hours": hours}
```

- [ ] **Step 4: 執行測試，確認通過**

```bash
pytest tests/test_stream_router.py -v
```
Expected: 所有 stream router 測試 PASS（原有 + 新增 3 個）

- [ ] **Step 5: Commit**

```bash
git add routers/stream.py tests/test_stream_router.py
git commit -m "feat: add GET /stream/{camera_id}/timeline endpoint scanning HLS disk"
```

---

### Task 3: db_writer.py + analysis/scheduler.py — Settings DB + 熱重載

**Files:**
- Modify: `db_writer.py`
- Modify: `analysis/scheduler.py`
- Modify: `tests/test_db_writer.py`
- Modify: `tests/test_analysis_scheduler.py`

- [ ] **Step 1: 在 `tests/test_db_writer.py` 末尾新增 2 個 settings DB 測試**

```python
def test_get_all_settings_returns_dict(mock_pool):
    from db_writer import get_all_settings
    mock_pool.fetch.return_value = [
        {"key": "jpeg_quality", "value": "70"},
        {"key": "analysis_interval_minutes", "value": "30"},
    ]
    result = asyncio.run(get_all_settings(mock_pool))
    assert result == {"jpeg_quality": "70", "analysis_interval_minutes": "30"}
    sql = mock_pool.fetch.call_args[0][0]
    assert "SELECT key, value FROM user_settings" in sql


def test_upsert_settings_calls_executemany(mock_pool):
    from db_writer import upsert_settings
    mock_pool.executemany.return_value = None
    asyncio.run(upsert_settings(mock_pool, {"jpeg_quality": "80", "hls_retention_days": "60"}))
    mock_pool.executemany.assert_called_once()
    sql, args = mock_pool.executemany.call_args[0]
    assert "ON CONFLICT" in sql
    assert ("jpeg_quality", "80") in args
    assert ("hls_retention_days", "60") in args
```

- [ ] **Step 2: 在 `tests/test_analysis_scheduler.py` 末尾新增 reload 測試**

```python
def test_reload_updates_interval_and_threshold():
    from analysis.scheduler import Scheduler
    pool = AsyncMock()
    sched = Scheduler(pool, FakeSettings())
    assert sched._interval == 30 * 60
    assert sched._threshold == 1.0
    sched.reload(interval_minutes=15, std_threshold=2.5)
    assert sched._interval == 15 * 60
    assert sched._threshold == 2.5
```

- [ ] **Step 3: 執行確認 FAIL**

```bash
pytest tests/test_db_writer.py::test_get_all_settings_returns_dict \
       tests/test_db_writer.py::test_upsert_settings_calls_executemany \
       tests/test_analysis_scheduler.py::test_reload_updates_interval_and_threshold -v
```
Expected: 3 tests FAIL

- [ ] **Step 4: 在 `db_writer.py` 末尾新增兩個函式**

```python
async def get_all_settings(pool: asyncpg.Pool) -> dict[str, str]:
    rows = await pool.fetch("SELECT key, value FROM user_settings")
    return {r["key"]: r["value"] for r in rows}


async def upsert_settings(pool: asyncpg.Pool, updates: dict[str, str]) -> None:
    await pool.executemany(
        """INSERT INTO user_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
        [(k, v) for k, v in updates.items()],
    )
```

- [ ] **Step 5: 修改 `analysis/scheduler.py`**

在 `__init__` 新增 `_interval` 和 `_threshold` 實例變數，修改 `_loop()` 讀取 `self._interval`，修改 `_run_analysis()` 使用 `self._threshold`，新增 `reload()` 方法：

```python
class Scheduler:
    def __init__(self, pool, settings) -> None:
        self._pool = pool
        self._settings = settings
        self._interval: float = settings.analysis_interval_minutes * 60
        self._threshold: float = settings.anomaly_std_threshold
        self._task: Optional[asyncio.Task] = None

    def reload(self, interval_minutes: int, std_threshold: float) -> None:
        self._interval = interval_minutes * 60
        self._threshold = std_threshold

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
        while True:
            await asyncio.sleep(self._interval)   # reads self._interval each iteration
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
                if std_a > 0 and current_a < mean_a - self._threshold * std_a:
                    if not entry["activity_anomaly"]:
                        await write_health_alert(
                            self._pool, camera_id=camera_id, object_id=object_id,
                            metric="activity", current_value=current_a,
                            mean_value=mean_a, std_value=std_a,
                        )
                    entry["activity_anomaly"] = True
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
                if std_t > 0 and abs(current_t - mean_t) > self._threshold * std_t:
                    if not entry["temp_anomaly"]:
                        await write_health_alert(
                            self._pool, camera_id=camera_id, object_id=object_id,
                            metric="temperature", current_value=current_t,
                            mean_value=mean_t, std_value=std_t,
                        )
                    entry["temp_anomaly"] = True
                else:
                    entry["temp_anomaly"] = False
```

- [ ] **Step 6: 執行測試，確認通過**

```bash
pytest tests/test_db_writer.py tests/test_analysis_scheduler.py -v
```
Expected: 所有 db_writer 和 scheduler 測試 PASS（含新增 3 個）

- [ ] **Step 7: Commit**

```bash
git add db_writer.py analysis/scheduler.py \
        tests/test_db_writer.py tests/test_analysis_scheduler.py
git commit -m "feat: add settings DB helpers and Scheduler.reload() for hot-reload"
```

---

### Task 4: main.py + routers/settings.py — Settings API

**Files:**
- Modify: `main.py`
- Modify: `routers/settings.py`
- Create: `tests/test_settings_router.py`

- [ ] **Step 1: 建立 `tests/test_settings_router.py`**

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
def settings_client():
    from routers.settings import router
    mock_pool = AsyncMock()
    mock_sched = MagicMock()
    with patch.object(database, "get_pool", return_value=mock_pool):
        app = FastAPI()
        app.include_router(router)
        app.state.scheduler = mock_sched
        yield TestClient(app), mock_pool, mock_sched


def test_get_settings_returns_db_values(settings_client):
    client, mock_pool, _ = settings_client
    mock_pool.fetch.return_value = [
        {"key": "jpeg_quality", "value": "80"},
        {"key": "analysis_interval_minutes", "value": "15"},
        {"key": "anomaly_std_threshold", "value": "2.5"},
        {"key": "hls_retention_days", "value": "60"},
    ]
    resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert data["jpeg_quality"] == "80"
    assert data["analysis_interval_minutes"] == "15"


def test_get_settings_no_pool_returns_env_defaults(settings_client):
    client, _, _ = settings_client
    with patch.object(database, "get_pool", return_value=None):
        resp = client.get("/settings")
    assert resp.status_code == 200
    data = resp.json()
    assert "jpeg_quality" in data
    assert "analysis_interval_minutes" in data
    assert "anomaly_std_threshold" in data
    assert "hls_retention_days" in data


def test_put_settings_valid_key_returns_ok(settings_client):
    client, mock_pool, _ = settings_client
    mock_pool.executemany.return_value = None
    mock_pool.fetch.return_value = [
        {"key": "jpeg_quality", "value": "85"},
        {"key": "analysis_interval_minutes", "value": "30"},
        {"key": "anomaly_std_threshold", "value": "3.0"},
        {"key": "hls_retention_days", "value": "90"},
    ]
    resp = client.put("/settings", json={"jpeg_quality": "85"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert "jpeg_quality" in body["updated"]


def test_put_settings_invalid_key_returns_400(settings_client):
    client, _, _ = settings_client
    resp = client.put("/settings", json={"nonexistent_key": "value"})
    assert resp.status_code == 400


def test_put_settings_no_pool_returns_503(settings_client):
    client, _, _ = settings_client
    with patch.object(database, "get_pool", return_value=None):
        resp = client.put("/settings", json={"jpeg_quality": "85"})
    assert resp.status_code == 503


def test_put_settings_scheduler_reload_called(settings_client):
    client, mock_pool, mock_sched = settings_client
    mock_pool.executemany.return_value = None
    mock_pool.fetch.return_value = [
        {"key": "jpeg_quality", "value": "70"},
        {"key": "analysis_interval_minutes", "value": "15"},
        {"key": "anomaly_std_threshold", "value": "2.0"},
        {"key": "hls_retention_days", "value": "90"},
    ]
    resp = client.put("/settings", json={"analysis_interval_minutes": "15"})
    assert resp.status_code == 200
    mock_sched.reload.assert_called_once_with(interval_minutes=15, std_threshold=2.0)
```

- [ ] **Step 2: 執行確認 FAIL**

```bash
pytest tests/test_settings_router.py -v
```
Expected: 6 tests FAIL（settings 仍是 stub）

- [ ] **Step 3: 修改 `main.py`，在 `await scheduler.start()` 後加一行**

找到 `main.py` 中這段：
```python
scheduler = Scheduler(database.get_pool(), app_settings)
await scheduler.start()
```

改為：
```python
scheduler = Scheduler(database.get_pool(), app_settings)
await scheduler.start()
app.state.scheduler = scheduler
```

- [ ] **Step 4: 完整替換 `routers/settings.py`**

```python
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Request

import database
from config import settings as app_settings
from db_writer import get_all_settings, upsert_settings

router = APIRouter(prefix="/settings", tags=["settings"])

ALLOWED_KEYS = frozenset({
    "jpeg_quality",
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
})


@router.get("")
async def get_settings():
    pool = database.get_pool()
    if pool is None:
        return {
            "jpeg_quality":              str(app_settings.jpeg_quality),
            "analysis_interval_minutes": str(app_settings.analysis_interval_minutes),
            "anomaly_std_threshold":     str(app_settings.anomaly_std_threshold),
            "hls_retention_days":        str(app_settings.hls_retention_days),
        }
    return await get_all_settings(pool)


@router.put("")
async def update_settings(
    request: Request,
    body: Annotated[dict[str, str], Body()],
):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    if "analysis_interval_minutes" in updates or "anomaly_std_threshold" in updates:
        current = await get_all_settings(pool)
        request.app.state.scheduler.reload(
            interval_minutes=int(current["analysis_interval_minutes"]),
            std_threshold=float(current["anomaly_std_threshold"]),
        )
    return {"ok": True, "updated": list(updates.keys())}
```

- [ ] **Step 5: 執行測試，確認通過**

```bash
pytest tests/test_settings_router.py tests/test_main.py -v
```
Expected: 全部 PASS

- [ ] **Step 6: Commit**

```bash
git add main.py routers/settings.py tests/test_settings_router.py
git commit -m "feat: implement GET/PUT /settings with DB persistence and Scheduler hot-reload"
```

---

### Task 5: static/index.html — 前端修復 + 設定 UI

**Files:**
- Modify: `static/index.html`

此 Task 無自動化測試（前端），手動在瀏覽器驗證。

- [ ] **Step 1: 修改 `loadTimeline()` 改呼叫 HLS timeline endpoint**

找到 `loadTimeline` 函式內（約第 719 行）：
```js
const resp = await fetch(`/tracking/${currentCamera}/timeline?start_ts=${startTs}&end_ts=${endTs}`);
```
改為：
```js
const resp = await fetch(`/stream/${currentCamera}/timeline?start_ts=${startTs}&end_ts=${endTs}`);
```

- [ ] **Step 2: 在 `onVodTimeUpdate` 函式後新增 `pickClosestFrame` 函式**

在 `// ── Anomaly map` 區塊前（約第 850 行）插入：

```js
    // ── Closest-frame selector ────────────────────────────────
    function pickClosestFrame(logs, ts) {
      if (!logs.length) return [];
      const byFrame = new Map();
      for (const log of logs) {
        if (!byFrame.has(log.frame_id)) byFrame.set(log.frame_id, []);
        byFrame.get(log.frame_id).push(log);
      }
      let bestFrame = null, bestDist = Infinity;
      for (const [, frameLogs] of byFrame) {
        const dist = Math.abs(frameLogs[0].timestamp - ts);
        if (dist < bestDist) { bestDist = dist; bestFrame = frameLogs; }
      }
      return bestFrame || [];
    }
```

- [ ] **Step 3: 在 `onVodTimeUpdate` 中改用 `pickClosestFrame`**

找到（約第 843 行）：
```js
          latestBoxes = data.logs || [];
```
改為：
```js
          latestBoxes = pickClosestFrame(data.logs || [], ts);
```

- [ ] **Step 4: 在 `switchToLive()` 末尾加 `refreshNotifications()`**

找到 `switchToLive()` 最後幾行（約第 824 行）：
```js
      liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
      refreshAnomalyMap();
    }
```
改為：
```js
      liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
      refreshAnomalyMap();
      refreshNotifications();
    }
```

- [ ] **Step 5: 在 CSS 末尾（`</style>` 前）加入設定表單樣式**

在 `.notif-empty { ... }` 規則之後，`</style>` 之前插入：

```css
    /* ── Settings form ──────────────────────────────────────── */
    #settings-form {
      padding: var(--space-4);
      display: flex;
      flex-direction: column;
      gap: var(--space-4);
    }
    .setting-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: var(--space-4);
    }
    .setting-label {
      font-size: var(--text-sm);
      color: var(--text-muted);
      flex: 1;
    }
    .setting-control {
      background: var(--surface-3);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: var(--radius-md);
      padding: var(--space-2) var(--space-3);
      font: inherit;
      font-size: var(--text-sm);
      width: 150px;
      transition: border-color var(--transition);
    }
    .setting-control:hover { border-color: rgba(255,255,255,0.18); }
    .setting-control:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
    #save-settings-btn {
      align-self: flex-end;
      padding: var(--space-2) var(--space-6);
      background: var(--accent);
      color: #000;
      font-weight: 600;
      border-radius: var(--radius-md);
      font-size: var(--text-sm);
      cursor: pointer;
      transition: background var(--transition);
    }
    #save-settings-btn:hover { background: var(--accent-hover); }
    #save-settings-btn:disabled { opacity: 0.5; cursor: not-allowed; }
```

- [ ] **Step 6: 在 `#tab-bar` 加入設定 Tab 按鈕**

找到（約第 597 行）：
```html
      <button class="tab-btn" data-tab="notifications"
              onclick="switchTab('notifications')">通知中心</button>
```
在其後插入：
```html
      <button class="tab-btn" data-tab="settings"
              onclick="switchTab('settings')">設定</button>
```

- [ ] **Step 7: 在通知中心 Tab 之後插入設定 Tab 內容**

找到（約第 613 行）：
```html
    </div>

  <script src=
```
在 `</div>` 之後（底部 panel 結束標籤前）插入：

```html
    <div id="tab-settings" class="tab-content">
      <form id="settings-form" onsubmit="return false">
        <div class="setting-row">
          <label class="setting-label" for="s-jpeg-quality">影像壓縮品質</label>
          <input class="setting-control" type="number"
                 id="s-jpeg-quality" min="50" max="95">
        </div>
        <div class="setting-row">
          <label class="setting-label" for="s-analysis-interval">分析間隔</label>
          <select class="setting-control" id="s-analysis-interval">
            <option value="15">15 分鐘</option>
            <option value="30">30 分鐘</option>
            <option value="60">60 分鐘</option>
          </select>
        </div>
        <div class="setting-row">
          <label class="setting-label" for="s-anomaly-threshold">異常閾值（σ）</label>
          <input class="setting-control" type="number"
                 id="s-anomaly-threshold" min="1.0" step="0.1">
        </div>
        <div class="setting-row">
          <label class="setting-label" for="s-retention-days">影像保留天數</label>
          <input class="setting-control" type="number"
                 id="s-retention-days" min="1" max="365">
        </div>
        <button id="save-settings-btn" onclick="saveSettings()">儲存設定</button>
      </form>
    </div>
```

- [ ] **Step 8: 在 `refreshNotifications` 函式後新增 `loadSettings` 和 `saveSettings`**

在 `// ── Type toggle` 區塊前插入：

```js
    // ── Settings ──────────────────────────────────────────────
    async function loadSettings() {
      try {
        const data = await fetch('/settings').then(r => r.json());
        const jpegQuality    = document.getElementById('s-jpeg-quality');
        const analysisInterval = document.getElementById('s-analysis-interval');
        const anomalyThreshold = document.getElementById('s-anomaly-threshold');
        const retentionDays  = document.getElementById('s-retention-days');
        if (jpegQuality)      jpegQuality.value      = data.jpeg_quality ?? '70';
        if (analysisInterval) analysisInterval.value = data.analysis_interval_minutes ?? '30';
        if (anomalyThreshold) anomalyThreshold.value = data.anomaly_std_threshold ?? '3.0';
        if (retentionDays)    retentionDays.value     = data.hls_retention_days ?? '90';
      } catch (_) {}
    }

    async function saveSettings() {
      const btn = document.getElementById('save-settings-btn');
      btn.disabled = true;
      const body = {
        jpeg_quality:               document.getElementById('s-jpeg-quality').value,
        analysis_interval_minutes:  document.getElementById('s-analysis-interval').value,
        anomaly_std_threshold:      document.getElementById('s-anomaly-threshold').value,
        hls_retention_days:         document.getElementById('s-retention-days').value,
      };
      try {
        const resp = await fetch('/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        showToast('✓ 已儲存', 3000);
      } catch (e) {
        showToast(`儲存失敗：${e.message}`, 5000);
      } finally {
        btn.disabled = false;
      }
    }
```

- [ ] **Step 9: 在 `init()` 末尾呼叫 `loadSettings()`**

找到 `init()` 函式末尾（約第 1237 行），在 `refreshNotifications()` 之後插入：

```js
        loadSettings();
```

- [ ] **Step 10: Commit**

```bash
git add static/index.html
git commit -m "feat: fix VOD timeline endpoint, closest-frame bbox, switchToLive notifications, add settings UI"
```

---

### Task 6: 全測試套件驗證

**Files:** 無（只驗證）

- [ ] **Step 1: 執行完整測試套件**

```bash
pytest tests/ -v
```
Expected: 所有測試 PASS（≥ 116 tests：原有 105 + 新增 7 vod + 3 stream + 2 db_writer + 1 scheduler + 6 settings = 124 tests，扣除更新的原有測試後應有約 116+）

- [ ] **Step 2: 確認測試數量無異常減少**

```bash
pytest tests/ --co -q | tail -5
```
Expected: test count ≥ 116

- [ ] **Step 3: Commit（如有未 commit 的變動）**

若全部 PASS 且無未 commit 變動：

```bash
git log --oneline -6
```
確認最近 6 個 commit 均在本 Phase。
