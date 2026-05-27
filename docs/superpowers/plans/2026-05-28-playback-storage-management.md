# 回放儲存管理（保留 / 書籤 / 批量刪除）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓操作者標記回放時段為「保留」（不被自動刪除）、「書籤」（命名 + 一鍵導航），並可批量刪除存檔（連 DB 軌跡/告警），且 retention 略過受保護時段。

**Architecture:** 新 DB 表 `saved_segments`（camera_id + hour_ts，UNIQUE，label 可選＝書籤）為單一事實來源。新 router `routers/storage.py` 提供 CRUD + 批量刪除。`hls_retention.py` 擴充 `protected` 參數 + 新增 `delete_recording_hours`。`main.py` retention loop 每輪查保留時段傳入略過。前端 `static/index.html` 在現有 timeline 小時格子上加選取模式、浮出操作列、鎖/星標記、書籤清單面板、刪除防呆 modal。時段單位＝整小時，對齊既有 HLS 目錄/timeline/retention。

**Tech Stack:** Python 3（FastAPI, asyncpg, loguru）、PostgreSQL、pytest（`uv run pytest`）、vanilla JS + hls.js（`node --check` 驗語法）。

**測試基線備註：** 測試必須用 `uv run pytest`（asyncpg 只在 uv venv）。既有基線 4 失敗（待辦 #12：ZMQ_SOURCES OS-env gap — `test_config::test_default_mot_worker_threads` + 3 `test_stream_router`），與本計畫無關。全套件跑用 `--ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`。`tests/test_database.py` 需實機 Postgres（無 DB 環境會 error），其 schema 斷言仍須寫對。承接子系統 A（spec `2026-05-28-settings-wiring-retention`），其 `_retention_loop` 已每輪讀 DB、含雙層 try/except。

**標準慣例（每個後端 task 適用）：** 你在 `master` 上直接 commit，**不 push、不開 branch**。只動該 task 列出的檔案。

---

### Task 1: `saved_segments` 資料表

**Files:**
- Modify: `sql/init.sql`
- Test: `tests/test_database.py`（加 1 個 schema 斷言，DB-dependent）

- [ ] **Step 1: 寫測試**

在 `tests/test_database.py` 的 `test_schema_creates_expected_tables`（即斷言 `table_names` 含各表的測試，約 line 28-32 一帶）加一行（與既有 `assert "..." in table_names` 並列）：

```python
    assert "saved_segments" in table_names
```

- [ ] **Step 2: 跑測試確認失敗（需 DB；無 DB 環境則略過此步，靠 Step 4 的 grep 驗證）**

Run: `uv run pytest tests/test_database.py -v`
Expected（有 DB 時）: FAIL — `assert 'saved_segments' in table_names`。無 DB 環境會 collection/connection error，屬預期，改用 Step 4 grep 驗證 schema 正確。

- [ ] **Step 3: 加表到 `sql/init.sql`**

在 `user_settings` 表定義之後、`INSERT INTO user_settings ...` seed 之前（或檔案任一表定義區），新增：

```sql
CREATE TABLE IF NOT EXISTS saved_segments (
    id         BIGSERIAL PRIMARY KEY,
    camera_id  VARCHAR(16) NOT NULL,
    hour_ts    BIGINT NOT NULL,
    label      TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (camera_id, hour_ts)
);
CREATE INDEX IF NOT EXISTS idx_saved_segments_cam ON saved_segments (camera_id, hour_ts);
```

- [ ] **Step 4: 驗證 schema 文字正確**

Run: `grep -n "saved_segments" sql/init.sql`
Expected: 顯示 `CREATE TABLE IF NOT EXISTS saved_segments` + `UNIQUE (camera_id, hour_ts)` + index 行。

有 DB 環境時另跑 `uv run pytest tests/test_database.py -v` 應 PASS。

- [ ] **Step 5: Commit**

```bash
git add sql/init.sql tests/test_database.py
git commit -m "feat(storage): saved_segments 資料表（保留/書籤，整小時單位）"
```

---

### Task 2: `saved_segments` CRUD DB 函式

**Files:**
- Modify: `db_writer.py`（檔案結尾新增函式）
- Test: `tests/test_db_writer.py`（新增測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_db_writer.py` 結尾（`mock_pool` fixture 與 `import asyncio` 已存在於檔首）：

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_db_writer.py -v -k saved or bookmark or protected`
Expected: FAIL — ImportError（函式未定義）。

- [ ] **Step 3: 實作函式**

在 `db_writer.py` 結尾新增（沿用既有 async + asyncpg 風格；狀態字串以 `int(status.split()[-1])` 解析計數）：

```python
async def list_saved_segments(
    pool: asyncpg.Pool, camera_id: str, start_ts: float, end_ts: float
) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, camera_id, hour_ts, label, note
           FROM saved_segments
           WHERE camera_id=$1 AND hour_ts >= $2 AND hour_ts < $3
           ORDER BY hour_ts""",
        camera_id, int(start_ts), int(end_ts),
    )
    return [dict(r) for r in rows]


async def list_bookmarks(
    pool: asyncpg.Pool, camera_id: Optional[str] = None
) -> list[dict]:
    if camera_id is not None:
        rows = await pool.fetch(
            """SELECT id, camera_id, hour_ts, label, note
               FROM saved_segments
               WHERE label IS NOT NULL AND camera_id=$1
               ORDER BY hour_ts DESC""",
            camera_id,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, camera_id, hour_ts, label, note
               FROM saved_segments
               WHERE label IS NOT NULL
               ORDER BY hour_ts DESC""",
        )
    return [dict(r) for r in rows]


async def upsert_saved_segment(
    pool: asyncpg.Pool,
    camera_id: str,
    hour_ts: int,
    label: Optional[str] = None,
    note: Optional[str] = None,
) -> int:
    """label/note 為 None 時 COALESCE 保留既有值（保留動作不覆蓋既有書籤）；
    非 None 則設定/覆蓋（書籤動作）。回傳 row id。"""
    row = await pool.fetchrow(
        """INSERT INTO saved_segments (camera_id, hour_ts, label, note)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (camera_id, hour_ts) DO UPDATE
             SET label = COALESCE($3, saved_segments.label),
                 note  = COALESCE($4, saved_segments.note)
           RETURNING id""",
        camera_id, int(hour_ts), label, note,
    )
    return row["id"]


async def update_saved_segment(
    pool: asyncpg.Pool, seg_id: int, label: Optional[str], note: Optional[str]
) -> bool:
    """明確 SET（label 可被設成 NULL → 降級成純保留）。"""
    status = await pool.execute(
        "UPDATE saved_segments SET label=$2, note=$3 WHERE id=$1",
        seg_id, label, note,
    )
    return status != "UPDATE 0"


async def delete_saved_segment(pool: asyncpg.Pool, seg_id: int) -> bool:
    status = await pool.execute(
        "DELETE FROM saved_segments WHERE id=$1", seg_id
    )
    return status != "DELETE 0"


async def get_protected_hours(pool: asyncpg.Pool) -> set[tuple[str, int]]:
    rows = await pool.fetch("SELECT camera_id, hour_ts FROM saved_segments")
    return {(r["camera_id"], int(r["hour_ts"])) for r in rows}


async def delete_saved_segments_by_hours(
    pool: asyncpg.Pool, camera_id: str, hours: list[int]
) -> int:
    status = await pool.execute(
        "DELETE FROM saved_segments WHERE camera_id=$1 AND hour_ts = ANY($2)",
        camera_id, [int(h) for h in hours],
    )
    return int(status.split()[-1]) if status else 0
```

（`Optional` 已於 `db_writer.py` 檔首 `from typing import Optional` import，沿用。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_db_writer.py -v`
Expected: PASS（既有 + 7 新測試）。

- [ ] **Step 5: Commit**

```bash
git add db_writer.py tests/test_db_writer.py
git commit -m "feat(storage): saved_segments CRUD + get_protected_hours DB 函式"
```

---

### Task 3: 刪除 DB 軌跡/告警函式

**Files:**
- Modify: `db_writer.py`（結尾新增）
- Test: `tests/test_db_writer.py`（新增測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_db_writer.py` 結尾：

```python
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
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_db_writer.py::test_delete_recordings_in_range_deletes_both_tables -v`
Expected: FAIL — ImportError。

- [ ] **Step 3: 實作函式**

在 `db_writer.py` 結尾新增（`tracking_logs.timestamp` 為 DOUBLE PRECISION unix 秒；`health_alerts.triggered_at` 為 TIMESTAMPTZ → 用 EXTRACT(EPOCH...)）：

```python
async def delete_recordings_in_range(
    pool: asyncpg.Pool, camera_id: str, start_ts: float, end_ts: float
) -> dict:
    """刪該時段的 DB 軌跡與告警，回傳各自刪除列數。"""
    tl_status = await pool.execute(
        """DELETE FROM tracking_logs
           WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3""",
        camera_id, start_ts, end_ts,
    )
    ha_status = await pool.execute(
        """DELETE FROM health_alerts
           WHERE camera_id=$1
             AND EXTRACT(EPOCH FROM triggered_at) >= $2
             AND EXTRACT(EPOCH FROM triggered_at) < $3""",
        camera_id, start_ts, end_ts,
    )
    return {
        "tracking_logs": int(tl_status.split()[-1]) if tl_status else 0,
        "health_alerts": int(ha_status.split()[-1]) if ha_status else 0,
    }
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_db_writer.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add db_writer.py tests/test_db_writer.py
git commit -m "feat(storage): delete_recordings_in_range（刪時段內 tracking/alerts）"
```

---

### Task 4: `hls_retention.py` protected 略過 + delete_recording_hours

**Files:**
- Modify: `hls_retention.py`
- Test: `tests/test_hls_retention.py`（新增測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_hls_retention.py` 結尾（`_mk`、`find_expired_hour_dirs`、`datetime`、`timedelta`、`Path` 已於檔首；新增 import `delete_recording_hours`）：

把檔首 `from hls_retention import (...)` 那組再加一個名字 `delete_recording_hours`，並新增：

```python
def test_find_expired_skips_protected_hours(tmp_path):
    now = datetime(2026, 5, 24, 12, 0, 0)
    old_dt = now - timedelta(days=10)
    old = _mk(tmp_path, "cam_01", "rgb", old_dt)
    old_hour_unix = int(old_dt.replace(minute=0, second=0, microsecond=0).timestamp())
    # 未保護 → 列入刪除
    assert old in find_expired_hour_dirs(tmp_path, 7, now, protected=set())
    # 保護該 (cam, hour) → 不列入
    protected = {("cam_01", old_hour_unix)}
    assert old not in find_expired_hour_dirs(tmp_path, 7, now, protected=protected)


def test_find_expired_protected_none_is_backward_compatible(tmp_path):
    now = datetime(2026, 5, 24, 12, 0, 0)
    old = _mk(tmp_path, "cam_01", "rgb", now - timedelta(days=10))
    # 不傳 protected（預設 None）→ 行為同舊：列入刪除
    assert old in find_expired_hour_dirs(tmp_path, 7, now)


def test_delete_recording_hours_removes_rgb_and_thermal(tmp_path):
    now = datetime(2026, 5, 24, 9, 0, 0)
    rgb = _mk(tmp_path, "cam_01", "rgb", now)
    thermal = _mk(tmp_path, "cam_01", "thermal", now)
    other = _mk(tmp_path, "cam_01", "rgb", now - timedelta(hours=1))
    hour_ts = int(now.timestamp())
    deleted = delete_recording_hours(tmp_path, "cam_01", [hour_ts])
    assert rgb in deleted and thermal in deleted
    assert not rgb.exists() and not thermal.exists()
    assert other.exists()  # 其他小時不受影響


def test_delete_recording_hours_skips_missing_type_dir(tmp_path):
    now = datetime(2026, 5, 24, 9, 0, 0)
    rgb = _mk(tmp_path, "cam_01", "rgb", now)  # 只有 rgb，沒有 thermal
    hour_ts = int(now.timestamp())
    deleted = delete_recording_hours(tmp_path, "cam_01", [hour_ts])
    assert deleted == [rgb]  # thermal 不存在 → 跳過不報錯
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_retention.py -v`
Expected: FAIL — `find_expired_hour_dirs() got an unexpected keyword argument 'protected'` 與 `cannot import name 'delete_recording_hours'`。

- [ ] **Step 3: 改 `hls_retention.py`**

3a. `find_expired_hour_dirs` 簽名加 `protected`，並在判定過期後檢查保護集合。完整替換該函式為：

```python
def find_expired_hour_dirs(
    base_dir,
    retention_days: float,
    now: datetime,
    protected: "set[tuple[str, int]] | None" = None,
) -> list[Path]:
    """回傳 base_dir 下早於 (now - retention_days) 的小時目錄清單。
    protected（{(camera_id, hour_unix)}）內的小時目錄即使過期也跳過不刪。
    base_dir 不存在或無合格目錄回 []。"""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    cutoff = now - timedelta(days=retention_days)
    expired: list[Path] = []
    # 結構：base/<camera>/<stream_type>/<YYYY-MM-DD-HH>
    for cam_dir in base.iterdir():
        if not cam_dir.is_dir():
            continue
        for type_dir in cam_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for hour_dir in type_dir.iterdir():
                if not hour_dir.is_dir():
                    continue
                try:
                    dt = datetime.strptime(hour_dir.name, _HOUR_FMT)
                except ValueError:
                    continue  # 非小時命名 → 略過，絕不誤刪
                if dt >= cutoff:
                    continue
                if protected and (cam_dir.name, int(dt.timestamp())) in protected:
                    continue  # 受保留/書籤 → 不刪
                expired.append(hour_dir)
    return expired
```

3b. `purge_expired_hls` 簽名加 `protected` 並透傳。完整替換為：

```python
def purge_expired_hls(
    base_dir,
    retention_days: float,
    now: datetime | None = None,
    protected: "set[tuple[str, int]] | None" = None,
) -> list[Path]:
    """刪除所有過期且未受保護的小時目錄，回傳實際刪除的目錄清單。"""
    now = now or datetime.now()
    expired = find_expired_hour_dirs(base_dir, retention_days, now, protected=protected)
    deleted: list[Path] = []
    for d in expired:
        try:
            shutil.rmtree(d)
            deleted.append(d)
        except OSError as e:
            logger.warning(f"HLS retention：刪除 {d} 失敗：{e}")
    if deleted:
        logger.info(f"HLS retention：刪除 {len(deleted)} 個過期小時目錄（>{retention_days}d）")
    return deleted
```

3c. 在 `effective_retention_days` 函式之後（檔案結尾）新增：

```python
def delete_recording_hours(
    base_dir, camera_id: str, hour_ts_list: list[int]
) -> list[Path]:
    """刪指定攝影機指定小時的 rgb + thermal 目錄（存在才刪），回實際刪除清單。
    hour_ts 為該小時起點 unix 秒；目錄名以本地時區 %Y-%m-%d-%H 互轉，與
    hls_manager 產生端一致。"""
    base = Path(base_dir)
    deleted: list[Path] = []
    for hour_ts in hour_ts_list:
        name = datetime.fromtimestamp(hour_ts).strftime(_HOUR_FMT)
        for stype in ("rgb", "thermal"):
            d = base / camera_id / stype / name
            if d.is_dir():
                try:
                    shutil.rmtree(d)
                    deleted.append(d)
                except OSError as e:
                    logger.warning(f"刪除錄影 {d} 失敗：{e}")
    return deleted
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_retention.py -v`
Expected: PASS（既有 8 + 新 4 = 12）。

- [ ] **Step 5: Commit**

```bash
git add hls_retention.py tests/test_hls_retention.py
git commit -m "feat(storage): retention protected 略過 + delete_recording_hours"
```

---

### Task 5: `routers/storage.py` + main.py 掛載

**Files:**
- Create: `routers/storage.py`
- Modify: `main.py`（include router）
- Test: `tests/test_storage_router.py`（新建）

- [ ] **Step 1: 寫失敗測試**

新建 `tests/test_storage_router.py`（沿用 `tests/test_settings_router.py` 的 `_dummy_zmq_sources` + TestClient + mock pool 樣板；此處重現該樣板，勿 import 它）：

```python
import sys
from unittest.mock import AsyncMock, MagicMock, patch
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@contextmanager
def _dummy_zmq_sources():
    from config import ZmqSource, settings as _cfg
    _orig = _cfg.zmq_sources
    _cfg.zmq_sources = [ZmqSource(
        name="t", src_host="127.0.0.1", src_port=5555, src_topic="t", label="cam_01",
    )]
    try:
        yield
    finally:
        _cfg.zmq_sources = _orig


@pytest.fixture
def client():
    import inference.pipeline as pipeline_mod
    import analysis.scheduler as scheduler_mod
    mock_pool = AsyncMock()
    with _dummy_zmq_sources():
        with (
            patch("database.connect", new_callable=AsyncMock),
            patch("database.disconnect", new_callable=AsyncMock),
            patch("database.get_pool", return_value=mock_pool),
            patch("zmq_receiver.zmq_receiver.start"),
            patch("zmq_receiver.zmq_receiver.stop"),
            patch.object(pipeline_mod.inference_pipeline, "start"),
            patch.object(pipeline_mod.inference_pipeline, "stop"),
            patch("hls_manager.hls_manager.stop_all"),
            patch.object(scheduler_mod.Scheduler, "start", new_callable=AsyncMock),
            patch.object(scheduler_mod.Scheduler, "stop", new_callable=AsyncMock),
        ):
            from main import app
            with TestClient(app) as c:
                c._mock_pool = mock_pool
                yield c


def test_get_segments_ok(client):
    with patch("routers.storage.list_saved_segments", new_callable=AsyncMock) as m:
        m.return_value = [{"id": 1, "camera_id": "cam_01", "hour_ts": 1000, "label": None, "note": None}]
        resp = client.get("/storage/segments?camera_id=cam_01&start_ts=0&end_ts=5000")
    assert resp.status_code == 200
    assert resp.json()["segments"][0]["camera_id"] == "cam_01"


def test_get_segments_unknown_camera_404(client):
    resp = client.get("/storage/segments?camera_id=nope&start_ts=0&end_ts=5000")
    assert resp.status_code == 404


def test_post_segments_creates(client):
    with patch("routers.storage.upsert_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = 5
        resp = client.post("/storage/segments",
                           json={"camera_id": "cam_01", "hours": [1000, 4600], "label": "採血前"})
    assert resp.status_code == 200
    assert resp.json()["count"] == 2
    assert m.await_count == 2


def test_put_segment_404_when_missing(client):
    with patch("routers.storage.update_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = False
        resp = client.put("/storage/segments/99", json={"label": "x", "note": None})
    assert resp.status_code == 404


def test_delete_segment_ok(client):
    with patch("routers.storage.delete_saved_segment", new_callable=AsyncMock) as m:
        m.return_value = True
        resp = client.delete("/storage/segments/5")
    assert resp.status_code == 200


def test_get_bookmarks_ok(client):
    with patch("routers.storage.list_bookmarks", new_callable=AsyncMock) as m:
        m.return_value = [{"id": 2, "camera_id": "cam_01", "hour_ts": 2000, "label": "x", "note": None}]
        resp = client.get("/storage/bookmarks?camera_id=cam_01")
    assert resp.status_code == 200
    assert resp.json()["bookmarks"][0]["label"] == "x"


def test_recordings_delete_calls_fs_and_db(client):
    with (
        patch("routers.storage.delete_recording_hours") as m_fs,
        patch("routers.storage.delete_recordings_in_range", new_callable=AsyncMock) as m_db,
        patch("routers.storage.delete_saved_segments_by_hours", new_callable=AsyncMock) as m_seg,
    ):
        m_fs.return_value = ["d1", "d2"]
        m_db.return_value = {"tracking_logs": 10, "health_alerts": 2}
        m_seg.return_value = 1
        resp = client.post("/storage/recordings/delete",
                           json={"camera_id": "cam_01", "hours": [1000]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["deleted_hours"] == 1
    m_fs.assert_called_once()
    m_db.assert_awaited_once()
    m_seg.assert_awaited_once()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_router.py -v`
Expected: FAIL — 404（router 尚未掛載）或 import error。

- [ ] **Step 3: 建立 `routers/storage.py`**

```python
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
from config import settings as app_settings
from db_writer import (
    delete_recordings_in_range,
    delete_saved_segment,
    delete_saved_segments_by_hours,
    list_bookmarks,
    list_saved_segments,
    update_saved_segment,
    upsert_saved_segment,
)
from hls_retention import delete_recording_hours

router = APIRouter(prefix="/storage", tags=["storage"])


def _require_pool():
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return pool


def _require_camera(camera_id: str) -> None:
    if camera_id not in [s.label for s in app_settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")


class SegmentCreate(BaseModel):
    camera_id: str
    hours: list[int]
    label: Optional[str] = None
    note: Optional[str] = None


class SegmentUpdate(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = None


class RecordingsDelete(BaseModel):
    camera_id: str
    hours: list[int]


@router.get("/segments")
async def get_segments(camera_id: str, start_ts: float, end_ts: float):
    pool = _require_pool()
    _require_camera(camera_id)
    segments = await list_saved_segments(pool, camera_id, start_ts, end_ts)
    return {"segments": segments}


@router.get("/bookmarks")
async def get_bookmarks(camera_id: Optional[str] = None):
    pool = _require_pool()
    if camera_id is not None:
        _require_camera(camera_id)
    bookmarks = await list_bookmarks(pool, camera_id)
    return {"bookmarks": bookmarks}


@router.post("/segments")
async def create_segments(body: SegmentCreate):
    pool = _require_pool()
    _require_camera(body.camera_id)
    if not body.hours:
        raise HTTPException(status_code=400, detail="hours must not be empty")
    for h in body.hours:
        await upsert_saved_segment(pool, body.camera_id, h, body.label, body.note)
    return {"ok": True, "count": len(body.hours)}


@router.put("/segments/{seg_id}")
async def edit_segment(seg_id: int, body: SegmentUpdate):
    pool = _require_pool()
    found = await update_saved_segment(pool, seg_id, body.label, body.note)
    if not found:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.delete("/segments/{seg_id}")
async def remove_segment(seg_id: int):
    pool = _require_pool()
    found = await delete_saved_segment(pool, seg_id)
    if not found:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.post("/recordings/delete")
async def delete_recordings(body: RecordingsDelete):
    pool = _require_pool()
    _require_camera(body.camera_id)
    if not body.hours:
        raise HTTPException(status_code=400, detail="hours must not be empty")
    dirs = delete_recording_hours(
        app_settings.hls_base_dir, body.camera_id, body.hours
    )
    tl = ha = 0
    for h in body.hours:
        counts = await delete_recordings_in_range(pool, body.camera_id, h, h + 3600)
        tl += counts["tracking_logs"]
        ha += counts["health_alerts"]
    await delete_saved_segments_by_hours(pool, body.camera_id, body.hours)
    return {
        "ok": True,
        "deleted_hours": len(body.hours),
        "dirs_removed": len(dirs),
        "tracking_logs": tl,
        "health_alerts": ha,
    }
```

3b. `main.py`：在既有 router include 區（`app.include_router(notes.router)` 一帶）加入 storage：
- import 行 `from routers import alerts, notes, stream, tracking` 改為加上 `storage`：
  `from routers import alerts, notes, stream, storage, tracking`
- 在 `app.include_router(notes.router)` 之後加：
  `app.include_router(storage.router)`

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_router.py -v`
Expected: PASS（7 測試）。

- [ ] **Step 5: Commit**

```bash
git add routers/storage.py main.py tests/test_storage_router.py
git commit -m "feat(storage): /storage router（segments CRUD + bookmarks + recordings delete）"
```

---

### Task 6: retention loop 傳入 protected 集合

**Files:**
- Modify: `main.py`（`_retention_loop`）
- Test: `tests/test_storage_router.py`（加 1 個 wiring 測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_storage_router.py` 結尾。此測試直接以受控參數呼叫 `_retention_loop` 的單輪邏輯——但迴圈是 `while True`，不便直接測；改為驗證 `main` 有 import `get_protected_hours` 且 `_retention_loop` 原始碼含 `protected=` 傳遞（輕量靜態檢查，避免起 daemon 迴圈）：

```python
def test_retention_loop_passes_protected(client):
    import inspect
    import main
    src = inspect.getsource(main._retention_loop)
    assert "get_protected_hours" in src
    assert "protected=" in src
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_router.py::test_retention_loop_passes_protected -v`
Expected: FAIL — `assert 'get_protected_hours' in src`。

- [ ] **Step 3: 改 `main.py`**

3a. import：`from db_writer import get_all_settings` 改為 `from db_writer import get_all_settings, get_protected_hours`。

3b. `_retention_loop` 內，把 purge 段（子系統 A 寫成）的 DB 讀取與 purge 呼叫擴充為一併取 protected。將迴圈內 `try:` 區塊改為：

```python
        try:
            pool = database.get_pool()
            db_settings = None
            protected: set[tuple[str, int]] = set()
            if pool is not None:
                try:
                    db_settings = await get_all_settings(pool)
                    protected = await get_protected_hours(pool)
                except Exception as e:
                    logger.warning(f"HLS retention 讀取 DB 設定失敗，回退 app_settings：{e}")
            days = effective_retention_days(
                db_settings, app_settings.hls_retention_days
            )
            purge_expired_hls(
                app_settings.hls_base_dir, days, protected=protected
            )
        except Exception as e:  # 巡檢失敗不可拖垮服務
            logger.warning(f"HLS retention 巡檢失敗：{e}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_router.py -v`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_storage_router.py
git commit -m "feat(storage): retention loop 略過保留/書籤時段（protected）"
```

---

### Task 7: 前端選取模式 + 浮出操作列 + 格子標記

**Files:**
- Modify: `static/index.html`

前置：閱讀 `static/index.html` 既有 `renderTimeline`（約 line 1005-1025）、`loadTimeline`（約 993）、`loadVod`、`switchTab`（約 1213）、`camSelect` change handler（約 1971）、狀態變數區（約 909-921）、`#week-nav`/`#timeline-bar`（約 773-779）、CSS `.timeline-slot`（約 401-411）。以下都是additive + 對 `renderTimeline`/重置點的小修改。

- [ ] **Step 1: 加狀態變數**

在現有狀態變數宣告區（如 `let currentCamera` 一帶）加：

```javascript
    let selectMode = false;
    let selectedHours = new Set();        // hour_ts (number)
    let savedSegmentsMap = new Map();     // hour_ts -> {id, label, note}
```

- [ ] **Step 2: 加 CSS**

在 `.timeline-slot.selected` 規則附近加：

```css
    .timeline-slot.slot-selected { outline: 2px solid var(--accent); outline-offset: -2px; }
    .timeline-slot.protected::after { content: "🔒"; position: absolute; top: 0; right: 1px; font-size: 8px; }
    .timeline-slot.bookmarked::after { content: "★"; position: absolute; top: 0; right: 1px; font-size: 8px; color: #ffd24d; }
    .timeline-slot { position: relative; }
    #storage-action-bar { display: none; gap: var(--space-2); align-items: center; padding: var(--space-2) var(--space-3); background: var(--surface-2); border-radius: 8px; margin-top: var(--space-2); }
    #storage-action-bar.visible { display: flex; }
    #storage-action-bar button { padding: 4px 10px; border-radius: 6px; cursor: pointer; }
    .select-toggle { margin-left: auto; display: inline-flex; align-items: center; gap: 4px; font-size: 13px; cursor: pointer; }
```

- [ ] **Step 3: 加 DOM（選取鈕 + 操作列）**

在 `#week-nav` 內（`<button id="live-btn" ...>` 之後）加選取切換鈕：

```html
      <label class="select-toggle"><input type="checkbox" id="select-mode-toggle">選取</label>
```

在 `<div id="timeline-bar" ...></div>` 之後加操作列：

```html
    <div id="storage-action-bar" role="toolbar" aria-label="儲存管理操作">
      <span id="storage-sel-count">已選 0 小時</span>
      <button id="btn-retain" onclick="onRetainClick()">保留</button>
      <button id="btn-bookmark" onclick="onBookmarkClick()">書籤</button>
      <button id="btn-delete-rec" onclick="onDeleteRecClick()">刪除</button>
      <button id="btn-clear-sel" onclick="clearSelection()">取消選取</button>
    </div>
```

- [ ] **Step 4: 改 `renderTimeline` 支援選取/標記**

把既有 `renderTimeline(hours)`（約 1005-1025）整段替換為（保留既有 168 格、has-data 邏輯，新增 selectMode 分支 + 標記）：

```javascript
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
        const seg = savedSegmentsMap.get(slotTs);
        if (seg) slot.classList.add(seg.label ? 'bookmarked' : 'protected');
        if (selectedHours.has(slotTs)) slot.classList.add('slot-selected');
        if (hasData) {
          slot.addEventListener('click', () => {
            if (selectMode) {
              if (selectedHours.has(slotTs)) { selectedHours.delete(slotTs); slot.classList.remove('slot-selected'); }
              else { selectedHours.add(slotTs); slot.classList.add('slot-selected'); }
              updateActionBar();
            } else {
              document.querySelectorAll('.timeline-slot.selected')
                .forEach(s => s.classList.remove('selected'));
              slot.classList.add('selected');
              loadVod(slotTs);
            }
          });
        }
        timelineBar.appendChild(slot);
      }
    }
```

- [ ] **Step 5: 加選取/操作列 helper + 載入標記**

在 `renderTimeline` 之後加：

```javascript
    function updateActionBar() {
      const bar = document.getElementById('storage-action-bar');
      document.getElementById('storage-sel-count').textContent = `已選 ${selectedHours.size} 小時`;
      bar.classList.toggle('visible', selectMode && selectedHours.size > 0);
    }

    function clearSelection() {
      selectedHours.clear();
      document.querySelectorAll('.timeline-slot.slot-selected')
        .forEach(s => s.classList.remove('slot-selected'));
      updateActionBar();
    }

    async function loadSavedSegments() {
      savedSegmentsMap = new Map();
      if (!currentCamera || !currentWeekStart) return;
      const startTs = currentWeekStart, endTs = currentWeekStart + 7 * 24 * 3600;
      try {
        const resp = await fetch(`/storage/segments?camera_id=${currentCamera}&start_ts=${startTs}&end_ts=${endTs}`);
        if (!resp.ok) return;
        const { segments } = await resp.json();
        segments.forEach(s => savedSegmentsMap.set(s.hour_ts, s));
      } catch (_) {}
    }
```

- [ ] **Step 6: 接上選取鈕 + loadTimeline 載標記**

6a. 在 init 區（如綁定 solo-checkbox 那段附近）加：

```javascript
    {
      const t = document.getElementById('select-mode-toggle');
      if (t) t.addEventListener('change', e => {
        selectMode = e.target.checked;
        clearSelection();
      });
    }
```

6b. 把 `loadTimeline` 改成抓完 hours 後也載標記再渲染。將其 `const { hours } = await resp.json(); renderTimeline(hours);` 改為：

```javascript
        const { hours } = await resp.json();
        await loadSavedSegments();
        renderTimeline(hours);
```

6c. 在 `camSelect` change handler 與 `prevWeek`/`nextWeek` 內，於重置處加 `clearSelection();`（避免跨攝影機/跨週殘留選取）。具體：在 `camSelect` change handler 既有的 `document.querySelectorAll('.timeline-slot.selected')...` 重置一帶加 `clearSelection();`。`prevWeek`/`nextWeek` 因會重 `loadTimeline`→重繪，於函式開頭加 `clearSelection();`。

- [ ] **Step 7: 加 onRetain/onBookmark 動作（刪除留 Task 8）**

在 helper 區加：

```javascript
    async function onRetainClick() {
      if (selectedHours.size === 0) return;
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/segments', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours }),
        });
        if (!resp.ok) throw new Error();
        clearSelection();
        await loadTimeline();
      } catch (_) { alert('保留失敗'); }
    }

    async function onBookmarkClick() {
      if (selectedHours.size === 0) return;
      const label = prompt('書籤名稱：');
      if (label === null || label.trim() === '') return;
      const note = prompt('備註（可留空）：') || null;
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/segments', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours, label: label.trim(), note }),
        });
        if (!resp.ok) throw new Error();
        clearSelection();
        await loadTimeline();
        if (typeof loadBookmarks === 'function') loadBookmarks();
      } catch (_) { alert('書籤失敗'); }
    }
```

（`onDeleteRecClick` 在 Task 8 定義；Task 7 先放一個暫時 stub 讓 `node --check` 不報未定義也無妨——但 onclick 只在執行時查找，語法檢查不需先定義。為避免使用者在 Task 7 完成後點刪除報錯，於 helper 區加暫時 stub：）

```javascript
    function onDeleteRecClick() { alert('刪除功能於下一步啟用'); }
```

- [ ] **Step 8: 驗證 JS 語法**

```bash
sed -n '/<script>/,/<\/script>/p' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```
Expected: `JS_OK`。

- [ ] **Step 9: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): timeline 選取模式 + 保留/書籤操作列 + 格子標記"
```

---

### Task 8: 前端書籤清單面板 + 刪除防呆 modal + 全套件驗證

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 加書籤分頁**

1a. 在 `#tab-bar`（約 787-794）的分頁鈕後加：

```html
      <button class="tab-btn" data-tab="bookmarks"
              onclick="switchTab('bookmarks'); loadBookmarks()">書籤</button>
```

1b. 在 `#tab-settings` 等 tab-content 同層加：

```html
    <div id="tab-bookmarks" class="tab-content">
      <ul id="bookmark-list" style="list-style:none;margin:0;padding:var(--space-3) var(--space-4)"></ul>
    </div>
```

- [ ] **Step 2: 加書籤清單 render + 動作**

在 helper 區加（取代 Task 7 的 `loadBookmarks` typeof 檢查為真正函式）：

```javascript
    async function loadBookmarks() {
      const ul = document.getElementById('bookmark-list');
      if (!ul || !currentCamera) return;
      try {
        const resp = await fetch(`/storage/bookmarks?camera_id=${currentCamera}`);
        if (!resp.ok) return;
        const { bookmarks } = await resp.json();
        ul.innerHTML = '';
        if (bookmarks.length === 0) {
          ul.innerHTML = '<li style="opacity:.6">尚無書籤</li>';
          return;
        }
        bookmarks.forEach(b => {
          const li = document.createElement('li');
          li.style.cssText = 'display:flex;gap:8px;align-items:center;padding:6px 0;border-bottom:1px solid var(--surface-3)';
          const when = new Date(b.hour_ts * 1000).toLocaleString('zh-TW',
            { month: '2-digit', day: '2-digit', hour: '2-digit' });
          const link = document.createElement('a');
          link.href = '#'; link.textContent = `★ ${b.label}`;
          link.style.cssText = 'color:var(--accent);text-decoration:none;flex:1';
          link.onclick = (e) => { e.preventDefault(); loadVod(b.hour_ts); };
          const time = document.createElement('span');
          time.textContent = when; time.style.cssText = 'opacity:.7;font-size:12px';
          const del = document.createElement('button');
          del.textContent = '移除'; del.style.cssText = 'padding:2px 8px;cursor:pointer';
          del.onclick = async () => {
            if (!confirm(`移除書籤「${b.label}」？（不刪影片）`)) return;
            await fetch(`/storage/segments/${b.id}`, { method: 'DELETE' });
            loadBookmarks(); loadTimeline();
          };
          li.append(link, time, del);
          ul.appendChild(li);
        });
      } catch (_) {}
    }
```

- [ ] **Step 3: 加刪除防呆 modal（HTML）**

在 `</body>` 前加：

```html
    <div id="delete-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:1000;align-items:center;justify-content:center">
      <div style="background:var(--surface-2);padding:var(--space-4);border-radius:12px;max-width:420px;width:90%">
        <h3 style="margin:0 0 8px">確認刪除存檔</h3>
        <p id="delete-modal-summary"></p>
        <div id="delete-modal-warn" style="display:none;background:rgba(255,68,68,.15);border:1px solid #ff4444;border-radius:8px;padding:8px;margin:8px 0">
          <strong style="color:#ff6666">⚠ 下列時段已被保留/書籤：</strong>
          <ul id="delete-modal-protected" style="margin:6px 0 0;padding-left:18px"></ul>
          <label style="display:flex;gap:6px;margin-top:8px;align-items:center">
            <input type="checkbox" id="delete-confirm-check">我了解這些時段已被保留/書籤，仍要刪除
          </label>
        </div>
        <p style="color:#ff6666;font-size:13px">此操作不可逆，將同時刪除影片與該時段的追蹤/告警紀錄。</p>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button onclick="closeDeleteModal()">取消</button>
          <button id="delete-confirm-btn" onclick="confirmDeleteRecordings()" style="background:#ff4444;color:#fff">刪除</button>
        </div>
      </div>
    </div>
```

- [ ] **Step 4: 加刪除 modal 邏輯（取代 Task 7 的 stub）**

把 Task 7 的 `function onDeleteRecClick() { alert(...); }` stub 整個替換為：

```javascript
    function onDeleteRecClick() {
      if (selectedHours.size === 0) return;
      const hours = [...selectedHours].sort((a, b) => a - b);
      const fmt = ts => new Date(ts * 1000).toLocaleString('zh-TW',
        { month: '2-digit', day: '2-digit', hour: '2-digit' });
      document.getElementById('delete-modal-summary').textContent =
        `將刪除 ${hours.length} 個小時：${hours.map(fmt).join('、')}`;
      const protectedSel = hours.filter(h => savedSegmentsMap.has(h));
      const warn = document.getElementById('delete-modal-warn');
      const check = document.getElementById('delete-confirm-check');
      const btn = document.getElementById('delete-confirm-btn');
      if (protectedSel.length > 0) {
        const ul = document.getElementById('delete-modal-protected');
        ul.innerHTML = '';
        protectedSel.forEach(h => {
          const seg = savedSegmentsMap.get(h);
          const li = document.createElement('li');
          li.textContent = `${fmt(h)}${seg.label ? '（書籤：' + seg.label + '）' : '（保留）'}`;
          ul.appendChild(li);
        });
        warn.style.display = '';
        check.checked = false;
        btn.disabled = true;
        check.onchange = () => { btn.disabled = !check.checked; };
      } else {
        warn.style.display = 'none';
        btn.disabled = false;
        check.onchange = null;
      }
      document.getElementById('delete-modal').style.display = 'flex';
    }

    function closeDeleteModal() {
      document.getElementById('delete-modal').style.display = 'none';
    }

    async function confirmDeleteRecordings() {
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/recordings/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours }),
        });
        if (!resp.ok) throw new Error();
        const r = await resp.json();
        closeDeleteModal();
        clearSelection();
        await loadTimeline();
        if (typeof loadBookmarks === 'function') loadBookmarks();
        alert(`已刪除 ${r.deleted_hours} 小時（影片目錄 ${r.dirs_removed}、軌跡 ${r.tracking_logs}、告警 ${r.health_alerts}）`);
      } catch (_) { alert('刪除失敗'); }
    }
```

- [ ] **Step 5: 切攝影機時刷新書籤**

在 `camSelect` change handler 末尾（既有 refresh 呼叫一帶）加：`if (typeof loadBookmarks === 'function') loadBookmarks();`

- [ ] **Step 6: 驗證 JS 語法**

```bash
sed -n '/<script>/,/<\/script>/p' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```
Expected: `JS_OK`。

- [ ] **Step 7: 確認無遺留 stub**

```bash
grep -n "刪除功能於下一步啟用" static/index.html || echo "NO_STUB"
```
Expected: `NO_STUB`（Task 7 的暫時 stub 已被 Step 4 取代）。

- [ ] **Step 8: 全套件驗證**

```bash
uv run pytest --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py -q
```
Expected: 既有通過數 + 本計畫新測試（Task2 七 + Task3 一 + Task4 四 + Task5 七 + Task6 一 = 20）皆綠；維持 4 既有 ZMQ_SOURCES 失敗，零新回歸。

- [ ] **Step 9: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 書籤清單面板 + 刪除防呆 modal"
```

---

## Self-Review（撰寫後對照 spec）

- **§3 資料表 saved_segments** → Task 1。✅
- **§4 DB 函式（list/list_bookmarks/upsert/update/delete/get_protected_hours/delete_saved_segments_by_hours）** → Task 2；`delete_recordings_in_range` → Task 3。✅
- **§5 檔案函式（find/purge protected、delete_recording_hours）** → Task 4。✅
- **§6 API（/storage segments CRUD + bookmarks + recordings/delete）+ main 掛載** → Task 5。✅
- **§7 retention 整合（protected）** → Task 6。✅
- **§8.1 選取模式 / §8.2 操作列 / §8.3 標記** → Task 7；**§8.3 書籤清單 / §8.4 刪除防呆 modal** → Task 8。✅
- **§9 測試策略** → 各 task TDD 步驟（db_writer mock、hls_retention tmp_path、storage router TestClient、node --check、全套件）。✅
- **Placeholder 掃描**：無 TBD；每個 code step 有完整程式碼。Task 7 的 `onDeleteRecClick` 暫時 stub 明確標示並於 Task 8 Step 4 取代 + Step 7 grep 驗證無殘留。✅
- **型別/命名一致**：`upsert_saved_segment(pool, camera_id, hour_ts, label, note)`、`get_protected_hours → set[tuple[str,int]]`、`delete_recording_hours(base_dir, camera_id, hour_ts_list)`、`delete_recordings_in_range(pool, camera_id, start_ts, end_ts)`、`delete_saved_segments_by_hours(pool, camera_id, hours)` 在 router（Task 5）與 retention（Task 6）呼叫處簽名一致。前端 `savedSegmentsMap`/`selectedHours`/`selectMode`/`loadSavedSegments`/`loadBookmarks`/`clearSelection`/`updateActionBar` 跨 Task 7/8 一致。✅
- **DRY/YAGNI**：合一張表、一個 POST 端點兩用（保留/書籤）、retention 沿用子系統 A 結構僅加 protected；非目標明確排除。✅
