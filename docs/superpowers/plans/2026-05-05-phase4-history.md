# Phase 4 — 歷史查詢 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 追蹤結果寫入 PostgreSQL tracking_logs，實作 VOD 回放（動態 m3u8 含 EXT-X-PROGRAM-DATE-TIME），前端新增 7 天時間軸（週導航）與歷史 bbox overlay。

**Architecture:** 獨立 `db_writer.py`（async DB 函式）+ `vod_generator.py`（同步 m3u8 生成，解析各小時 index.m3u8）。Pipeline 以 `run_coroutine_threadsafe` 寫入 tracking_logs 並計算 thermal intensity。前端以 `hls.playingDate.getTime()/1000` 驅動歷史 bbox 查詢。

**Tech Stack:** asyncpg、FastAPI PlainTextResponse、hls.js playingDate、原生 JS

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `db_writer.py` | Create | write_tracking_log, query_tracking_logs, query_timeline_hours |
| `vod_generator.py` | Create | build_vod_m3u8（解析每小時 index.m3u8，串接，加 EXT-X-PROGRAM-DATE-TIME）|
| `hls_manager.py` | Modify | hls_list_size 0 + 移除 delete_segments（VOD 必要前置條件）|
| `inference/pipeline.py` | Modify | _compute_thermal_intensity + run_coroutine_threadsafe DB 寫入 |
| `routers/tracking.py` | Modify | 實作 GET /tracking/{camera_id} 和 GET /tracking/{camera_id}/timeline |
| `routers/stream.py` | Modify | 實作 GET /stream/{camera_id}/vod → PlainTextResponse m3u8 |
| `static/index.html` | Modify | 時間軸 UI（週導航 + 168 槽）+ VOD 模式 + 歷史 bbox overlay |
| `tests/test_db_writer.py` | Create | db_writer 單元測試（mock asyncpg pool）|
| `tests/test_vod_generator.py` | Create | build_vod_m3u8 單元測試（tmp_path 假 m3u8）|
| `tests/test_tracking_get.py` | Create | GET 端點測試（TestClient + mock db_writer）|
| `tests/test_stream_router.py` | Modify | 新增 VOD 端點測試 |
| `tests/test_ws_tracking.py` | Modify | 更新已失效的 not-implemented 斷言 |

---

### Task 1: db_writer.py — 三個 async DB 函式

**Files:**
- Create: `db_writer.py`
- Create: `tests/test_db_writer.py`

- [ ] **Step 1: 建立 `tests/test_db_writer.py`（全部為 failing）**

```python
# tests/test_db_writer.py
import asyncio
from unittest.mock import AsyncMock
import pytest


@pytest.fixture
def mock_pool():
    return AsyncMock()


def test_write_tracking_log_executes_insert(mock_pool):
    from db_writer import write_tracking_log
    asyncio.run(write_tracking_log(
        mock_pool,
        camera_id="cam_01", timestamp=1000.0, frame_id=1, object_id=2,
        bb_left=10.0, bb_top=20.0, bb_width=50.0, bb_height=60.0,
        confidence=0.9, thermal_intensity=128.5,
    ))
    mock_pool.execute.assert_awaited_once()
    sql = mock_pool.execute.call_args[0][0]
    assert "INSERT INTO tracking_logs" in sql


def test_write_tracking_log_passes_none_thermal(mock_pool):
    from db_writer import write_tracking_log
    asyncio.run(write_tracking_log(
        mock_pool,
        camera_id="cam_01", timestamp=1000.0, frame_id=1, object_id=2,
        bb_left=0.0, bb_top=0.0, bb_width=10.0, bb_height=10.0,
        confidence=0.5, thermal_intensity=None,
    ))
    args = mock_pool.execute.call_args[0]
    assert args[-1] is None  # thermal_intensity 最後一個位置引數


def test_query_tracking_logs_returns_formatted_dicts(mock_pool):
    from db_writer import query_tracking_logs
    mock_pool.fetch = AsyncMock(return_value=[{
        "object_id": 3, "bb_left": 10.0, "bb_top": 20.0,
        "bb_width": 50.0, "bb_height": 60.0,
        "confidence": 0.85, "timestamp": 1000.5, "frame_id": 42,
    }])
    result = asyncio.run(query_tracking_logs(mock_pool, "cam_01", 990.0, 1010.0))
    assert len(result) == 1
    assert result[0]["bbox"] == [10.0, 20.0, 50.0, 60.0]
    assert result[0]["object_id"] == 3
    assert result[0]["timestamp"] == 1000.5
    assert result[0]["frame_id"] == 42


def test_query_tracking_logs_with_object_id_uses_fourth_param(mock_pool):
    from db_writer import query_tracking_logs
    mock_pool.fetch = AsyncMock(return_value=[])
    asyncio.run(query_tracking_logs(mock_pool, "cam_01", 990.0, 1010.0, object_id=5))
    sql = mock_pool.fetch.call_args[0][0]
    assert "$4" in sql  # object_id filter 用第 4 個 placeholder


def test_query_tracking_logs_without_object_id_omits_fourth_param(mock_pool):
    from db_writer import query_tracking_logs
    mock_pool.fetch = AsyncMock(return_value=[])
    asyncio.run(query_tracking_logs(mock_pool, "cam_01", 990.0, 1010.0))
    sql = mock_pool.fetch.call_args[0][0]
    assert "$4" not in sql


def test_query_timeline_hours_returns_int_list(mock_pool):
    from db_writer import query_timeline_hours
    mock_pool.fetch = AsyncMock(return_value=[
        {"hour": 1000000}, {"hour": 1003600},
    ])
    result = asyncio.run(query_timeline_hours(mock_pool, "cam_01", 0.0, 9999999.0))
    assert result == [1000000, 1003600]
```

- [ ] **Step 2: 確認測試失敗（db_writer 尚不存在）**

```bash
uv run pytest tests/test_db_writer.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'db_writer'`

- [ ] **Step 3: 建立 `db_writer.py`**

```python
# db_writer.py
from typing import Optional

import asyncpg


async def write_tracking_log(
    pool: asyncpg.Pool,
    *,
    camera_id: str,
    timestamp: float,
    frame_id: int,
    object_id: int,
    bb_left: float,
    bb_top: float,
    bb_width: float,
    bb_height: float,
    confidence: float,
    thermal_intensity: Optional[float],
) -> None:
    await pool.execute(
        """INSERT INTO tracking_logs
           (camera_id, timestamp, frame_id, object_id,
            bb_left, bb_top, bb_width, bb_height, confidence, thermal_intensity)
           VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)""",
        camera_id, timestamp, frame_id, object_id,
        bb_left, bb_top, bb_width, bb_height, confidence, thermal_intensity,
    )


async def query_tracking_logs(
    pool: asyncpg.Pool,
    camera_id: str,
    start: float,
    end: float,
    object_id: Optional[int] = None,
) -> list[dict]:
    if object_id is not None:
        rows = await pool.fetch(
            """SELECT object_id, bb_left, bb_top, bb_width, bb_height,
                      confidence, timestamp, frame_id
               FROM tracking_logs
               WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3
                 AND object_id=$4
               ORDER BY timestamp""",
            camera_id, start, end, object_id,
        )
    else:
        rows = await pool.fetch(
            """SELECT object_id, bb_left, bb_top, bb_width, bb_height,
                      confidence, timestamp, frame_id
               FROM tracking_logs
               WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3
               ORDER BY timestamp""",
            camera_id, start, end,
        )
    return [
        {
            "object_id": r["object_id"],
            "bbox": [r["bb_left"], r["bb_top"], r["bb_width"], r["bb_height"]],
            "confidence": r["confidence"],
            "timestamp": r["timestamp"],
            "frame_id": r["frame_id"],
        }
        for r in rows
    ]


async def query_timeline_hours(
    pool: asyncpg.Pool,
    camera_id: str,
    start_ts: float,
    end_ts: float,
) -> list[int]:
    rows = await pool.fetch(
        """SELECT DISTINCT CAST(floor(timestamp / 3600) * 3600 AS BIGINT) AS hour
           FROM tracking_logs
           WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3
           ORDER BY hour""",
        camera_id, start_ts, end_ts,
    )
    return [r["hour"] for r in rows]
```

- [ ] **Step 4: 確認測試通過**

```bash
uv run pytest tests/test_db_writer.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add db_writer.py tests/test_db_writer.py
git commit -m "feat: add db_writer module with tracking log write and query functions"
```

---

### Task 2: hls_manager.py — VOD 必要前置條件（保留所有 segment 檔案）

**Files:**
- Modify: `hls_manager.py:64-65`

**背景：** 目前 `hls_list_size 5` + `delete_segments` 旗標會讓 ffmpeg 主動刪除 playlist 滾出的 .ts 檔案，導致 VOD 完全失效。改為 `hls_list_size 0`（保留全部）+ `append_list`（不刪檔）。

- [ ] **Step 1: 修改 `hls_manager.py` 第 64–65 行**

將：
```python
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list",
```
改為：
```python
        "-hls_list_size", "0",
        "-hls_flags", "append_list",
```

- [ ] **Step 2: 確認現有 HLS 測試仍通過**

```bash
uv run pytest tests/test_hls_manager.py -v
```
Expected: all passed（測試不涉及 ffmpeg 指令字串）

- [ ] **Step 3: Commit**

```bash
git add hls_manager.py
git commit -m "fix: keep all HLS segments on disk (hls_list_size 0, no delete_segments) for VOD support"
```

---

### Task 3: vod_generator.py — 動態 m3u8 生成

**Files:**
- Create: `vod_generator.py`
- Create: `tests/test_vod_generator.py`

- [ ] **Step 1: 建立 `tests/test_vod_generator.py`（全部為 failing）**

```python
# tests/test_vod_generator.py
from datetime import datetime, timezone
from pathlib import Path
import pytest


HOUR_TS = 1746403200  # 2026-05-05 00:00:00 UTC


def _make_hour_dir(base: Path, camera_id: str, stream_type: str, hour_ts: int) -> Path:
    dt = datetime.fromtimestamp(hour_ts, tz=timezone.utc)
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


def test_segment_urls_use_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=2)
    dt = datetime.fromtimestamp(HOUR_TS, tz=timezone.utc)
    dir_name = dt.strftime("%Y-%m-%d-%H")
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert f"/stream/hls/cam_01/rgb/{dir_name}/seg_000.ts" in result


def test_filters_segments_before_start_ts(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3, duration=4.0)
    # 從第 2 個 segment（offset=4s）開始
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
    dt1 = datetime.fromtimestamp(hour1_ts, tz=timezone.utc)
    dt2 = datetime.fromtimestamp(hour2_ts, tz=timezone.utc)
    for hour_ts, dt in [(hour1_ts, dt1), (hour2_ts, dt2)]:
        hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", hour_ts)
        _write_m3u8(hour_dir, segment_count=1)
    result = build_vod_m3u8("cam_01", "rgb", float(hour1_ts), float(hour2_ts + 3600))
    assert result is not None
    assert dt1.strftime("%Y-%m-%d-%H") in result
    assert dt2.strftime("%Y-%m-%d-%H") in result


def test_target_duration_taken_from_m3u8(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    # 手動寫 TARGETDURATION:6
    (hour_dir / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.000000,\nseg_000.ts\n"
    )
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert "#EXT-X-TARGETDURATION:6" in result
```

- [ ] **Step 2: 確認測試失敗**

```bash
uv run pytest tests/test_vod_generator.py -v 2>&1 | head -20
```
Expected: `ModuleNotFoundError: No module named 'vod_generator'`

- [ ] **Step 3: 建立 `vod_generator.py`**

```python
# vod_generator.py
import re
from datetime import datetime, timezone
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
        dt = datetime.fromtimestamp(current_hour, tz=timezone.utc)
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
    pdt = datetime.fromtimestamp(first_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

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

- [ ] **Step 4: 確認測試通過**

```bash
uv run pytest tests/test_vod_generator.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add vod_generator.py tests/test_vod_generator.py
git commit -m "feat: add vod_generator with EXT-X-PROGRAM-DATE-TIME m3u8 building"
```

---

### Task 4: inference/pipeline.py — Thermal intensity 計算 + DB 寫入

**Files:**
- Modify: `inference/pipeline.py`
- Modify: `tests/test_inference_pipeline.py`

- [ ] **Step 1: 新增 thermal intensity 測試至 `tests/test_inference_pipeline.py`**

在檔案末尾新增（HybridSORT mock block 已在檔案頂部）：

```python
def test_compute_thermal_intensity_returns_mean_of_region():
    import numpy as np
    from inference.pipeline import _compute_thermal_intensity
    thermal = np.zeros((120, 160), dtype=np.uint8)
    thermal[10:20, 10:20] = 200  # 該區域均值為 200
    # bbox 在 640×480 空間：x1=40,y1=40,x2=80,y2=80
    # 縮放到 160×120：tx1=10,ty1=10,tx2=20,ty2=20 (scale=0.25)
    result = _compute_thermal_intensity(thermal, 40.0, 40.0, 80.0, 80.0)
    assert result == pytest.approx(200.0)


def test_compute_thermal_intensity_returns_none_when_no_thermal():
    from inference.pipeline import _compute_thermal_intensity
    result = _compute_thermal_intensity(None, 0.0, 0.0, 50.0, 50.0)
    assert result is None


def test_compute_thermal_intensity_clamps_bbox_to_image_bounds():
    import numpy as np
    from inference.pipeline import _compute_thermal_intensity
    thermal = np.full((120, 160), 100, dtype=np.uint8)
    # bbox 超出邊界：x2=800 > 640, y2=600 > 480
    result = _compute_thermal_intensity(thermal, 0.0, 0.0, 800.0, 600.0)
    assert result == pytest.approx(100.0)
```

- [ ] **Step 2: 確認新測試失敗**

```bash
uv run pytest tests/test_inference_pipeline.py::test_compute_thermal_intensity_returns_mean_of_region -v
```
Expected: `ImportError: cannot import name '_compute_thermal_intensity'`

- [ ] **Step 3: 修改 `inference/pipeline.py`**

在 `import numpy as np` 之後、`@dataclass` 之前新增模組級函式：

```python
def _compute_thermal_intensity(
    thermal_np: "np.ndarray | None",
    x1: float, y1: float, x2: float, y2: float,
    orig_w: int = 640, orig_h: int = 480,
    thermal_w: int = 160, thermal_h: int = 120,
) -> "float | None":
    if thermal_np is None:
        return None
    sx = thermal_w / orig_w
    sy = thermal_h / orig_h
    tx1 = int(max(0, x1 * sx))
    ty1 = int(max(0, y1 * sy))
    tx2 = int(min(thermal_w, x2 * sx))
    ty2 = int(min(thermal_h, y2 * sy))
    if tx2 <= tx1 or ty2 <= ty1:
        return None
    return float(np.mean(thermal_np[ty1:ty2, tx1:tx2]))
```

在 `_process_batch` 方法的 tracker 結果迴圈中，將現有的：

```python
            for cam, frame_data, fut in futures:
                online_targets = fut.result()
                objects = []
                for t in online_targets:
                    x1, y1, x2, y2 = float(t[0]), float(t[1]), float(t[2]), float(t[3])
                    objects.append({
                        "object_id": int(t[4]),
                        "bbox": [x1, y1, x2 - x1, y2 - y1],
                        "confidence": float(t[5]) if len(t) > 5 else 0.0,
                    })
```

替換為：

```python
            for cam, frame_data, fut in futures:
                online_targets = fut.result()
                objects = []
                for t in online_targets:
                    x1, y1, x2, y2 = float(t[0]), float(t[1]), float(t[2]), float(t[3])
                    obj_id = int(t[4])
                    conf = float(t[5]) if len(t) > 5 else 0.0
                    ti = _compute_thermal_intensity(frame_data.thermal_np, x1, y1, x2, y2)
                    objects.append({
                        "object_id": obj_id,
                        "bbox": [x1, y1, x2 - x1, y2 - y1],
                        "confidence": conf,
                    })
                    pool = database.get_pool()
                    if pool is not None:
                        asyncio.run_coroutine_threadsafe(
                            write_tracking_log(
                                pool,
                                camera_id=cam,
                                timestamp=frame_data.ts,
                                frame_id=frame_data.frame_id,
                                object_id=obj_id,
                                bb_left=x1,
                                bb_top=y1,
                                bb_width=x2 - x1,
                                bb_height=y2 - y1,
                                confidence=conf,
                                thermal_intensity=ti,
                            ),
                            self._event_loop,
                        )
```

在 `inference/pipeline.py` 的 import 區塊末尾新增：

```python
import database
from db_writer import write_tracking_log
```

（放在 `from loguru import logger` 之後）

- [ ] **Step 4: 確認所有 pipeline 測試通過**

```bash
uv run pytest tests/test_inference_pipeline.py -v
```
Expected: 全部通過（新增的 3 個 + 原有的）

- [ ] **Step 5: Commit**

```bash
git add inference/pipeline.py tests/test_inference_pipeline.py
git commit -m "feat: add thermal intensity computation and DB write to inference pipeline"
```

---

### Task 5: routers/tracking.py — 實作 GET 端點

**Files:**
- Modify: `routers/tracking.py`
- Create: `tests/test_tracking_get.py`
- Modify: `tests/test_ws_tracking.py:93-95`

- [ ] **Step 1: 建立 `tests/test_tracking_get.py`**

```python
# tests/test_tracking_get.py
import sys
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

# mock HybridSORT 避免 GPU init
for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@pytest.fixture
def app_client():
    import database
    import zmq_receiver as zmq_mod
    import inference.pipeline as pipeline_mod
    from fastapi.testclient import TestClient

    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
        patch.object(pipeline_mod.inference_pipeline, "start"),
        patch.object(pipeline_mod.inference_pipeline, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_get_tracking_returns_logs(app_client):
    fake_logs = [
        {"object_id": 1, "bbox": [10.0, 20.0, 50.0, 60.0],
         "confidence": 0.9, "timestamp": 1000.0, "frame_id": 1}
    ]
    import database
    with patch("routers.tracking.query_tracking_logs", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = fake_logs
        resp = app_client.get("/tracking/rpi_sensors?start=990&end=1010")
    assert resp.status_code == 200
    assert resp.json()["logs"] == fake_logs


def test_get_tracking_with_object_id_filter(app_client):
    import database
    with patch("routers.tracking.query_tracking_logs", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = []
        resp = app_client.get("/tracking/rpi_sensors?start=0&end=1000&object_id=3")
    assert resp.status_code == 200
    mock_q.assert_awaited_once()
    _, kwargs = mock_q.call_args
    assert kwargs.get("object_id") == 3


def test_get_tracking_requires_start_and_end(app_client):
    resp = app_client.get("/tracking/rpi_sensors")
    assert resp.status_code == 422  # missing required params


def test_get_timeline_returns_hours(app_client):
    import database
    with patch("routers.tracking.query_timeline_hours", new_callable=AsyncMock) as mock_q, \
         patch.object(database, "get_pool", return_value=MagicMock()):
        mock_q.return_value = [1000000, 1003600]
        resp = app_client.get("/tracking/rpi_sensors/timeline?start_ts=0&end_ts=9999999")
    assert resp.status_code == 200
    assert resp.json()["hours"] == [1000000, 1003600]


def test_get_timeline_requires_start_ts_and_end_ts(app_client):
    resp = app_client.get("/tracking/rpi_sensors/timeline")
    assert resp.status_code == 422
```

- [ ] **Step 2: 確認新測試失敗**

```bash
uv run pytest tests/test_tracking_get.py -v 2>&1 | head -30
```
Expected: 多數失敗（endpoint 回傳 `not implemented` 或 422）

- [ ] **Step 3: 修改 `routers/tracking.py`**

在現有 import 區塊新增：

```python
import database
from db_writer import query_tracking_logs, query_timeline_hours
```

（放在 `from fastapi import APIRouter, WebSocket, WebSocketDisconnect` 之後）

將現有的 `get_tracking` 函式替換為：

```python
@router.get("/tracking/{camera_id}/timeline")
async def get_timeline(
    camera_id: str,
    start_ts: float,
    end_ts: float,
):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    hours = await query_timeline_hours(pool, camera_id, start_ts, end_ts)
    return {"hours": hours}


@router.get("/tracking/{camera_id}")
async def get_tracking(
    camera_id: str,
    start: float,
    end: float,
    object_id: Optional[int] = None,
):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    logs = await query_tracking_logs(pool, camera_id, start, end, object_id)
    return {"logs": logs}
```

注意：`/timeline` 路由必須定義在 `/{camera_id}` **之前**，否則 `timeline` 會被捕獲為 `object_id` 的一部分（但 FastAPI 以路徑段數區分，實際上不衝突；仍建議先定義更具體的路由）。

在 import 中加入 `HTTPException`：

```python
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
```

- [ ] **Step 4: 更新 `tests/test_ws_tracking.py` 第 93–95 行**

將：
```python
def test_ws_tracking_http_endpoint_still_works(app_client):
    resp = app_client.get("/tracking/cam_01")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}
```

改為：
```python
def test_get_tracking_requires_query_params(app_client):
    # start/end 現在是必要參數；缺少時回傳 422
    resp = app_client.get("/tracking/cam_01")
    assert resp.status_code == 422
```

- [ ] **Step 5: 確認所有 tracking 測試通過**

```bash
uv run pytest tests/test_tracking_get.py tests/test_ws_tracking.py -v
```
Expected: 全部通過

- [ ] **Step 6: Commit**

```bash
git add routers/tracking.py tests/test_tracking_get.py tests/test_ws_tracking.py
git commit -m "feat: implement GET /tracking/{camera_id} and /timeline endpoints"
```

---

### Task 6: routers/stream.py — VOD endpoint

**Files:**
- Modify: `routers/stream.py`
- Modify: `tests/test_stream_router.py`

- [ ] **Step 1: 在 `tests/test_stream_router.py` 末尾新增 VOD 測試**

```python
def test_vod_returns_m3u8_content(client):
    fake_m3u8 = "#EXTM3U\n#EXT-X-ENDLIST\n"
    with patch("routers.stream.build_vod_m3u8", return_value=fake_m3u8):
        resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=rgb")
    assert resp.status_code == 200
    assert resp.text == fake_m3u8
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")


def test_vod_returns_404_when_no_segments(client):
    with patch("routers.stream.build_vod_m3u8", return_value=None):
        resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=rgb")
    assert resp.status_code == 404


def test_vod_returns_400_for_invalid_type(client):
    resp = client.get("/stream/rpi_sensors/vod?start=1000&end=4600&type=invalid")
    assert resp.status_code == 400


def test_vod_returns_404_for_unknown_camera(client):
    resp = client.get("/stream/unknown_cam/vod?start=1000&end=4600")
    assert resp.status_code == 404
```

- [ ] **Step 2: 確認新測試失敗**

```bash
uv run pytest tests/test_stream_router.py::test_vod_returns_m3u8_content -v
```
Expected: FAIL（endpoint 回傳 `not implemented`）

- [ ] **Step 3: 修改 `routers/stream.py`**

在現有 import 區塊加入：

```python
from fastapi.responses import FileResponse, PlainTextResponse
from vod_generator import build_vod_m3u8
```

將現有的 `get_vod_stream` stub 替換為：

```python
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
```

- [ ] **Step 4: 確認所有 stream 測試通過**

```bash
uv run pytest tests/test_stream_router.py -v
```
Expected: 全部通過

- [ ] **Step 5: Commit**

```bash
git add routers/stream.py tests/test_stream_router.py
git commit -m "feat: implement GET /stream/{camera_id}/vod returning dynamic m3u8"
```

---

### Task 7: static/index.html — 時間軸 UI + VOD 模式 + 歷史 overlay

**Files:**
- Modify: `static/index.html`

此 Task 為純前端，無自動化測試，完成後以瀏覽器手動驗證。

- [ ] **Step 1: 在 `</style>` 之前（第 328 行前）插入時間軸 CSS**

```css
    /* ── Timeline ──────────────────────────────────────────── */
    #timeline-section {
      width: 100%;
      max-width: 840px;
      display: flex;
      flex-direction: column;
      gap: var(--space-2);
    }
    #week-nav {
      display: flex;
      align-items: center;
      gap: var(--space-3);
      flex-wrap: wrap;
    }
    #week-nav-btn {
      padding: var(--space-1) var(--space-3);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      font-size: var(--text-sm);
      transition: background var(--transition);
    }
    #week-nav-btn:hover:not(:disabled) { background: var(--surface-3); }
    #week-nav-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    .week-nav-btn {
      padding: var(--space-1) var(--space-3);
      background: var(--surface-2);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      font-size: var(--text-sm);
      transition: background var(--transition);
    }
    .week-nav-btn:hover:not(:disabled) { background: var(--surface-3); }
    .week-nav-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    #week-label {
      flex: 1;
      text-align: center;
      font-size: var(--text-sm);
      color: var(--text-muted);
    }
    #live-btn {
      padding: var(--space-1) var(--space-3);
      background: var(--accent);
      border-radius: var(--radius-full);
      border: none;
      color: #000;
      font-weight: 600;
      font-size: var(--text-xs);
      letter-spacing: 0.04em;
      cursor: pointer;
    }
    #timeline-bar {
      display: flex;
      width: 100%;
      height: 24px;
      background: var(--surface-2);
      border-radius: var(--radius-md);
      overflow: hidden;
      gap: 1px;
    }
    .timeline-slot {
      flex: 1;
      background: var(--surface-3);
    }
    .timeline-slot.has-data {
      background: var(--accent-dim);
      cursor: pointer;
      transition: background var(--transition);
    }
    .timeline-slot.has-data:hover { background: rgba(34,187,119,0.35); }
    .timeline-slot.selected { background: var(--accent); }
    .status-pill.vod {
      color: var(--warning);
      background: rgba(224,160,48,0.15);
      border-color: rgba(224,160,48,0.3);
    }
```

- [ ] **Step 2: 在 `<div class="toast"` 之前（第 389 行前）插入時間軸 HTML**

```html
  <!-- Timeline -->
  <div id="timeline-section">
    <div id="week-nav">
      <button class="week-nav-btn" id="prev-week-btn" onclick="prevWeek()" aria-label="上一週">&#8249;</button>
      <span id="week-label"></span>
      <button class="week-nav-btn" id="next-week-btn" onclick="nextWeek()" aria-label="下一週">&#8250;</button>
      <button id="live-btn" onclick="switchToLive()" style="display:none">● Live</button>
    </div>
    <div id="timeline-bar" role="list" aria-label="時間軸（每格代表一小時）"></div>
  </div>

```

- [ ] **Step 3: 在 JS State 區塊（`let animFrameId = null;` 之後）新增狀態變數**

```javascript
    let isLive = true;
    let currentWeekStart = null;   // 本週週一 00:00:00 UTC 的 Unix 秒
    let vodDebounceTimer = null;
```

- [ ] **Step 4: 在 DOM refs 區塊末尾新增 timeline DOM refs**

在 `const toastEl = document.getElementById('toast');` 之後：

```javascript
    const liveBtn        = document.getElementById('live-btn');
    const prevWeekBtn    = document.getElementById('prev-week-btn');
    const nextWeekBtn    = document.getElementById('next-week-btn');
    const weekLabelEl    = document.getElementById('week-label');
    const timelineBar    = document.getElementById('timeline-bar');
```

- [ ] **Step 5: 在 `// ── Type toggle` 之前插入 Timeline 與 VOD 函式**

```javascript
    // ── Timeline helpers ──────────────────────────────────────
    function getWeekStart(date) {
      const d = new Date(date);
      const day = d.getUTCDay();               // 0=Sun, 1=Mon...
      const diff = day === 0 ? -6 : 1 - day;  // 距離週一的天數
      d.setUTCDate(d.getUTCDate() + diff);
      d.setUTCHours(0, 0, 0, 0);
      return d.getTime() / 1000;
    }

    function formatWeekLabel(weekStartTs) {
      const s = new Date(weekStartTs * 1000);
      const e = new Date((weekStartTs + 6 * 86400) * 1000);
      const pad = n => String(n).padStart(2, '0');
      const startStr = `${pad(s.getUTCMonth()+1)}/${pad(s.getUTCDate())}`;
      const endStr   = `${pad(e.getUTCMonth()+1)}/${pad(e.getUTCDate())}`;
      return `${s.getUTCFullYear()}年${s.getUTCMonth()+1}月 ${startStr} – ${endStr}`;
    }

    function updateWeekNavButtons() {
      const nowWeekStart = getWeekStart(new Date());
      const minWeekStart = getWeekStart(new Date(Date.now() - 90 * 86400 * 1000));
      prevWeekBtn.disabled = currentWeekStart <= minWeekStart;
      nextWeekBtn.disabled = currentWeekStart >= nowWeekStart;
      weekLabelEl.textContent = formatWeekLabel(currentWeekStart);
    }

    function prevWeek() {
      currentWeekStart -= 7 * 86400;
      updateWeekNavButtons();
      loadTimeline();
    }

    function nextWeek() {
      currentWeekStart += 7 * 86400;
      updateWeekNavButtons();
      loadTimeline();
    }

    async function loadTimeline() {
      if (!currentCamera || !currentWeekStart) return;
      const startTs = currentWeekStart;
      const endTs   = currentWeekStart + 7 * 24 * 3600;
      try {
        const resp = await fetch(`/tracking/${currentCamera}/timeline?start_ts=${startTs}&end_ts=${endTs}`);
        if (!resp.ok) return;
        const { hours } = await resp.json();
        renderTimeline(hours);
      } catch (_) {}
    }

    function renderTimeline(hours) {
      timelineBar.innerHTML = '';
      const hourSet = new Set(hours);
      for (let i = 0; i < 168; i++) {
        const slotTs = currentWeekStart + i * 3600;
        const hasData = hourSet.has(slotTs);
        const slot = document.createElement('div');
        slot.className = 'timeline-slot' + (hasData ? ' has-data' : '');
        slot.setAttribute('role', 'listitem');
        slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
        if (hasData) {
          slot.addEventListener('click', () => {
            document.querySelectorAll('.timeline-slot.selected')
              .forEach(s => s.classList.remove('selected'));
            slot.classList.add('selected');
            loadVod(slotTs);
          });
        }
        timelineBar.appendChild(slot);
      }
    }

    // ── VOD mode ──────────────────────────────────────────────
    function loadVod(startTs) {
      isLive = false;
      liveBtn.style.display = '';
      latestBoxes = [];
      countBadge.textContent = '—';
      latencyChip.style.display = 'none';

      // 斷開 WS（不重連）
      clearTimeout(wsRetryTimer);
      wsGeneration++;
      if (ws) { ws.close(); ws = null; }

      video.removeEventListener('timeupdate', onVodTimeUpdate);
      if (hls) { hls.destroy(); hls = null; }
      video.src = '';
      setSkeleton(true);
      setStatus('載入回放...', '');

      const vodUrl = `/stream/${currentCamera}/vod?start=${startTs}&end=${startTs + 3600}&type=${currentType}`;

      if (Hls.isSupported()) {
        hls = new Hls({ lowLatencyMode: false, backBufferLength: 0 });
        hls.loadSource(vodUrl);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          video.play().catch(() => {});
          setSkeleton(false);
          const dt = new Date(startTs * 1000);
          const label = dt.toLocaleString('zh-TW', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit',
          });
          setStatus(`回放中 ${label}`, 'vod');
        });

        hls.on(Hls.Events.ERROR, (_, data) => {
          if (data.fatal) {
            setSkeleton(false);
            setStatus(`回放錯誤：${data.details}`, 'error');
          }
        });

        video.addEventListener('timeupdate', onVodTimeUpdate);
      }
    }

    function switchToLive() {
      if (isLive) return;
      isLive = true;
      liveBtn.style.display = 'none';
      video.removeEventListener('timeupdate', onVodTimeUpdate);
      clearTimeout(vodDebounceTimer);
      latestBoxes = [];
      document.querySelectorAll('.timeline-slot.selected')
        .forEach(s => s.classList.remove('selected'));
      wsRetryCount = 0;
      loadStream();
    }

    // ── Historical MOT overlay ────────────────────────────────
    function onVodTimeUpdate() {
      if (isLive || !hls) return;
      clearTimeout(vodDebounceTimer);
      vodDebounceTimer = setTimeout(async () => {
        const pd = hls.playingDate;
        if (!pd) return;
        const ts = pd.getTime() / 1000;
        try {
          const resp = await fetch(
            `/tracking/${currentCamera}?start=${ts - 0.5}&end=${ts + 0.5}`
          );
          if (!resp.ok) return;
          const data = await resp.json();
          latestBoxes = data.logs || [];
          countBadge.textContent = latestBoxes.length;
        } catch (_) {}
      }, 300);
    }
```

- [ ] **Step 6: 修改 `setType` 函式（加入 VOD 模式感知）**

將現有 `setType` 末尾的 `loadStream();` 替換為：

```javascript
      if (!isLive) {
        switchToLive();
      } else {
        loadStream();
      }
```

- [ ] **Step 7: 修改 `camSelect` change 事件處理（加入 timeline 重載與 VOD 感知）**

將現有：
```javascript
    camSelect.addEventListener('change', () => {
      currentCamera = camSelect.value;
      wsRetryCount = 0;
      latestBoxes = [];
      countBadge.textContent = '—';
      loadStream();
    });
```

替換為：
```javascript
    camSelect.addEventListener('change', () => {
      currentCamera = camSelect.value;
      wsRetryCount = 0;
      latestBoxes = [];
      countBadge.textContent = '—';
      if (!isLive) {
        // 換 camera 時回到 Live 模式
        isLive = true;
        liveBtn.style.display = 'none';
        video.removeEventListener('timeupdate', onVodTimeUpdate);
        clearTimeout(vodDebounceTimer);
        document.querySelectorAll('.timeline-slot.selected')
          .forEach(s => s.classList.remove('selected'));
      }
      loadStream();
      loadTimeline();
    });
```

- [ ] **Step 8: 修改 `init()` 函式（初始化時間軸）**

在 `loadStream();` 之後加入：

```javascript
        currentWeekStart = getWeekStart(new Date());
        updateWeekNavButtons();
        loadTimeline();
```

- [ ] **Step 9: 啟動 dev server 並手動驗證**

```bash
uv run uvicorn main:app --reload --log-level info --port 5005
```

開啟瀏覽器 `http://localhost:5005`，驗證：
- [ ] 時間軸元件顯示在 video card 下方
- [ ] 週導航左右箭頭可切換，超出 90 天時 disabled
- [ ] Live 按鈕初始隱藏
- [ ] 點擊有資料的時間槽後 Live 按鈕出現，狀態顯示「回放中...」
- [ ] 點擊 Live 按鈕後回到即時串流
- [ ] 切換 camera 時時間軸重新載入

- [ ] **Step 10: Commit**

```bash
git add static/index.html
git commit -m "feat: add timeline UI, VOD playback mode, and historical bbox overlay"
```

---

### Task 8: 全測試套件驗證

**Files:** 無新增，驗證所有測試通過

- [ ] **Step 1: 執行完整測試套件**

```bash
uv run pytest --tb=short -q
```
Expected: ≥ 75 passed, 0 failed（Phase 3 的 64 tests + Phase 4 新增 ≥ 11 tests）

- [ ] **Step 2: 若有失敗，逐一修復後重跑，直到全部通過**

- [ ] **Step 3: 確認最終測試數量**

```bash
uv run pytest --tb=short -q 2>&1 | tail -5
```

- [ ] **Step 4: Final commit（若 Step 2 有修復）**

```bash
git add -p  # 只加需要的修復
git commit -m "fix: resolve test failures after Phase 4 integration"
```

---

## 自我審查

**Spec 覆蓋確認：**
- ✅ 追蹤結果寫入 DB：Task 1 (db_writer) + Task 4 (pipeline)
- ✅ 前端時間軸（7 天視窗 + 週導航）：Task 7
- ✅ VOD 回放：Task 2 (HLS fix) + Task 3 (vod_generator) + Task 6 (endpoint)
- ✅ 歷史 MOT overlay（hls.playingDate）：Task 7

**型別一致性確認：**
- `write_tracking_log` 簽名（Task 1）= pipeline 呼叫時傳入的 kwargs（Task 4）✅
- `query_tracking_logs` 回傳的 dict 欄位（Task 1）= 前端 `latestBoxes` 格式（Task 7）✅
- `build_vod_m3u8` 回傳 `str | None`（Task 3）= stream router 使用方式（Task 6）✅
- `query_timeline_hours` 回傳 `list[int]`（Task 1）= 前端 `new Set(hours)` 比對（Task 7）✅

**VOD HLS prerequisite 確認：**
- Task 2 必須在 Task 3/6/7 之前完成，否則 VOD 產生的 m3u8 會指向已刪除的 .ts 檔案
