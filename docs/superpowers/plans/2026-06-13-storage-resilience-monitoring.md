# 儲存韌性（故障防護 + 健康監控 + 夜間 Ephemeral Live + 編碼旋鈕）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 pig-agri 在錄影磁碟故障/空間耗盡時不再靜默失敗——主動監控 + 告警 + 自動降級到 ephemeral live（夜間排程或磁碟掛掉都用同一機制），並加可選編碼旋鈕降低寫入量。

**Architecture:** 新增獨立 `storage_monitor.py`（純函式 + 遲滯狀態機 + 一個背景 loop），維護單一「目標模式」`record`/`ephemeral`/`drop`，由磁碟健康 × 夜間排程決定。`hls_manager` 讀這個模式選 ffmpeg 輸出目標（record→`hls_base_dir/<hour>`；ephemeral→`/dev/shm` 滾動 buffer；drop→丟幀），複用既有 hour-rollover 重啟與前端 `checkLiveHandoff` 機制，**不動脆弱的 PDT 內部邏輯**。告警走現成 `health_alerts`/通知中心，不碰 `get_anomaly_cache`（不亂亮紅框）。

**Tech Stack:** Python 3 / FastAPI / asyncpg / loguru / ffmpeg(libx264) / pytest / 原生 `os.statvfs`。

**Spec:** `docs/superpowers/specs/2026-06-13-storage-resilience-monitoring-design.md`

---

## 檔案結構

| 檔案 | 責任 | 動作 |
|---|---|---|
| `storage_monitor.py` | 純函式（空間/分類/去抖/排程）+ 探針 I/O + `StorageMonitor`（狀態機、target_mode、snapshot）+ 背景 evaluate | **建** |
| `tests/test_storage_monitor.py` | 上者全測 | **建** |
| `config.py` | 新增 `hls_ephemeral_dir`/`hls_crf`/`hls_video_codec`/`storage_*`/`recording_*` 預設 | 改 `52-107` |
| `hls_manager.py` | `_make_ffmpeg_cmd` 支援 rolling+crf+codec；`HLSStream` 模式感知 feed/_restart/writer drop guard；`HLSManager.active_out_dir`/ensure_started 模式感知 | 改多處 |
| `tests/test_hls_manager.py` | 新增 rolling/模式/drop 守衛測試 | 改 |
| `routers/stream.py` | `serve_hls` 從 active stream out_dir 撈 segment（含 ephemeral base） | 改 `15-32` |
| `tests/test_stream_router.py` | serve_hls 解析測試 | 改/建 |
| `routers/storage.py` | `GET /storage/health` | 改 |
| `tests/test_storage_router.py` | health endpoint 測試 | 改/建 |
| `routers/settings.py` | `ALLOWED_KEYS` + GET fallback 加新鍵 | 改 `9-40` |
| `tests/test_settings_router.py` | 新鍵接受測試 | 改 |
| `main.py` | 起 `_storage_monitor_loop` + `_storage_alert` 回呼 | 改 `57-73` |
| `static/index.html` | header 狀態小燈 + 設定面板新欄位 | 改 |

---

## Task 1: `storage_monitor.py` 純函式

**Files:**
- Create: `storage_monitor.py`
- Test: `tests/test_storage_monitor.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_storage_monitor.py
from datetime import datetime

import pytest

import storage_monitor as sm


def test_parse_hhmm_valid_and_invalid():
    assert sm.parse_hhmm("17:00") == 17 * 60
    assert sm.parse_hhmm("06:30") == 6 * 60 + 30
    assert sm.parse_hhmm("bad") == -1
    assert sm.parse_hhmm("25:00") == -1


@pytest.mark.parametrize("hour,minute,expected", [
    (12, 0, True),    # 中午 → 錄影時段
    (6, 30, True),    # 邊界：off_end，恢復錄影
    (16, 59, True),   # 17:00 前一刻仍錄
    (17, 0, False),   # 17:00 → 停錄
    (23, 0, False),   # 深夜停錄
    (3, 0, False),    # 凌晨停錄
    (6, 29, False),   # 06:30 前一刻仍停
])
def test_is_recording_time_overnight_window(hour, minute, expected):
    now = datetime(2026, 6, 13, hour, minute)
    assert sm.is_recording_time(now, 17 * 60, 6 * 60 + 30, True) is expected


def test_is_recording_time_disabled_always_true():
    now = datetime(2026, 6, 13, 23, 0)
    assert sm.is_recording_time(now, 17 * 60, 6 * 60 + 30, False) is True


def test_is_recording_time_empty_window_always_true():
    now = datetime(2026, 6, 13, 23, 0)
    assert sm.is_recording_time(now, 600, 600, True) is True
    assert sm.is_recording_time(now, -1, 390, True) is True


def test_classify_health_down_when_not_writable():
    s = sm.StorageSettings()
    assert sm.classify_health(False, True, 10**12, 0.5, s) == "down"
    assert sm.classify_health(True, False, 10**12, 0.5, s) == "down"


def test_classify_health_degraded_on_low_space_or_inodes():
    s = sm.StorageSettings(min_free_bytes=10 * 1024**3, min_free_inodes_ratio=0.02)
    assert sm.classify_health(True, True, 1 * 1024**3, 0.5, s) == "degraded"
    assert sm.classify_health(True, True, 100 * 1024**3, 0.001, s) == "degraded"


def test_classify_health_ok():
    s = sm.StorageSettings(min_free_bytes=10 * 1024**3, min_free_inodes_ratio=0.02)
    assert sm.classify_health(True, True, 100 * 1024**3, 0.5, s) == "ok"


def test_next_state_debounce():
    # reading 與 current 相同 → 不變、count 歸零
    assert sm.next_state("ok", "ok", 3, 2) == ("ok", 0)
    # 第一次不同 → 不翻轉，count=1
    assert sm.next_state("ok", "down", 0, 2) == ("ok", 1)
    # 第二次連續不同 → 翻轉
    assert sm.next_state("ok", "down", 1, 2) == ("down", 0)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: FAIL（`ModuleNotFoundError: No module named 'storage_monitor'`）

- [ ] **Step 3: 寫最小實作**

```python
# storage_monitor.py
"""儲存健康監控 + 目標模式決策（record / ephemeral / drop）。

設計對齊 hls_retention.py（純函式可測）與 analysis/scheduler（遲滯狀態機）。
本模組不 import hls_manager / database（避免循環依賴、保持純函式可測）；
DB 寫入告警由 main.py 以 alert_cb 注入。
"""
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class StorageSettings:
    check_interval_seconds: int = 20
    min_free_bytes: int = 10 * 1024**3
    min_free_inodes_ratio: float = 0.02
    debounce_count: int = 2
    volume_marker: str = ""
    schedule_enabled: bool = True
    off_start_min: int = 17 * 60          # 17:00
    off_end_min: int = 6 * 60 + 30        # 06:30


def parse_hhmm(s: str) -> int:
    """'HH:MM' → minutes-of-day（0..1439）。解析失敗回 -1。"""
    try:
        h, m = str(s).strip().split(":")
        h_i, m_i = int(h), int(m)
        if 0 <= h_i < 24 and 0 <= m_i < 60:
            return h_i * 60 + m_i
    except (ValueError, AttributeError):
        pass
    return -1


def is_recording_time(now: datetime, off_start_min: int, off_end_min: int,
                      enabled: bool) -> bool:
    """now 是否落在「錄影時段」（no-record 窗之外）。停用/無效/空窗 → 永遠錄。
    跨午夜：off 17:00→06:30 ⇒ 錄影 ON 僅 06:30–17:00。"""
    if not enabled:
        return True
    if off_start_min < 0 or off_end_min < 0 or off_start_min == off_end_min:
        return True
    cur = now.hour * 60 + now.minute
    if off_start_min <= off_end_min:
        in_off = off_start_min <= cur < off_end_min
    else:
        in_off = cur >= off_start_min or cur < off_end_min
    return not in_off


def check_free_space(path) -> tuple[int, float, float]:
    """(free_bytes, free_ratio, free_inodes_ratio)。路徑不存在 → OSError。"""
    st = os.statvfs(str(path))
    free_bytes = st.f_bavail * st.f_frsize
    total_bytes = st.f_blocks * st.f_frsize
    free_ratio = (free_bytes / total_bytes) if total_bytes else 0.0
    free_inodes_ratio = (st.f_favail / st.f_files) if st.f_files else 1.0
    return free_bytes, free_ratio, free_inodes_ratio


def classify_health(probe_ok: bool, marker_ok: bool, free_bytes: int,
                    free_inodes_ratio: float, settings: StorageSettings) -> str:
    """probe/marker 任一失敗 → down；空間或 inode 低於門檻 → degraded；否則 ok。"""
    if not probe_ok or not marker_ok:
        return "down"
    if (free_bytes < settings.min_free_bytes
            or free_inodes_ratio < settings.min_free_inodes_ratio):
        return "degraded"
    return "ok"


def next_state(current: str, reading: str, count: int, debounce: int) -> tuple[str, int]:
    """遲滯：需連續 debounce 次 reading != current 才翻轉。回 (new_state, new_count)。"""
    if reading == current:
        return current, 0
    count += 1
    if count >= debounce:
        return reading, 0
    return current, count
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: PASS（10 passed）

- [ ] **Step 5: Commit**

```bash
git add storage_monitor.py tests/test_storage_monitor.py
git commit -m "feat(storage): storage_monitor 純函式（空間分類/去抖/夜間排程）"
```

---

## Task 2: 探針 I/O + ephemeral 路徑 + 設定解析

**Files:**
- Modify: `storage_monitor.py`
- Test: `tests/test_storage_monitor.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_storage_monitor.py 追加
def test_write_probe_success_and_cleanup(tmp_path):
    assert sm.write_probe(tmp_path) is True
    assert not (tmp_path / ".storage_probe").exists()  # 用完即刪


def test_write_probe_fails_on_nonexistent_unwritable(tmp_path):
    bad = tmp_path / "nope" / "deep"
    # 父目錄不可建（指向一個檔案當父）
    f = tmp_path / "afile"
    f.write_text("x")
    assert sm.write_probe(f / "sub") is False


def test_marker_present(tmp_path):
    assert sm.marker_present(tmp_path, "") is True          # 空字串＝不檢查
    assert sm.marker_present(tmp_path, ".vol") is False     # marker 不存在 → 碟沒掛上
    (tmp_path / ".vol").write_text("1")
    assert sm.marker_present(tmp_path, ".vol") is True


def test_effective_ephemeral_dir_fallback(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: p != "/dev/shm")
    assert sm.effective_ephemeral_dir("/dev/shm/pig_live", "data/hls_live") == "data/hls_live"
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: True)
    assert sm.effective_ephemeral_dir("/dev/shm/pig_live", "data/hls_live") == "/dev/shm/pig_live"


class _AppCfg:
    storage_check_interval_seconds = 20
    storage_min_free_gb = 10.0
    storage_min_free_inodes_ratio = 0.02
    storage_debounce_count = 2
    storage_volume_marker = ""
    recording_schedule_enabled = True
    recording_off_start = "17:00"
    recording_off_end = "06:30"


def test_resolve_settings_db_overrides_app():
    db = {"storage_min_free_gb": "20", "recording_off_start": "18:00",
          "recording_schedule_enabled": "false"}
    s = sm.resolve_settings(db, _AppCfg())
    assert s.min_free_bytes == 20 * 1024**3
    assert s.off_start_min == 18 * 60
    assert s.schedule_enabled is False


def test_resolve_settings_falls_back_when_db_none():
    s = sm.resolve_settings(None, _AppCfg())
    assert s.min_free_bytes == 10 * 1024**3
    assert s.off_start_min == 17 * 60
    assert s.schedule_enabled is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: FAIL（`AttributeError: module 'storage_monitor' has no attribute 'write_probe'`）

- [ ] **Step 3: 寫最小實作（追加到 `storage_monitor.py`）**

```python
def write_probe(base_dir) -> bool:
    """在 base_dir 寫極小探針檔 → fsync → 刪除；任何 OSError → False。
    一次抓到唯讀 remount / 掛載消失 / 權限不足。"""
    base = Path(base_dir)
    probe = base / ".storage_probe"
    try:
        base.mkdir(parents=True, exist_ok=True)
        with open(probe, "wb") as fh:
            fh.write(str(time.time()).encode())
            fh.flush()
            os.fsync(fh.fileno())
        probe.unlink()
        return True
    except OSError:
        return False


def marker_present(base_dir, marker: str) -> bool:
    """掛載防誤判：marker 為空＝不檢查（回 True）；否則該標記檔須存在於 base_dir。
    USB 碟 unmount 後目錄變回 root fs 空目錄、probe 仍可寫 → 靠 marker 抓出。"""
    if not marker:
        return True
    return (Path(base_dir) / marker).exists()


def effective_ephemeral_dir(configured: str,
                            fallback: str = "data/pig_monitoring/hls_live") -> str:
    """configured 指向 /dev/shm 但該路徑不可用 → 回退 fallback（系統碟）。"""
    if configured.startswith("/dev/shm") and not os.path.isdir("/dev/shm"):
        logger.warning(f"/dev/shm 不可用，ephemeral live 改用 {fallback}")
        return fallback
    return configured


def _coerce_float(v, default: float) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _coerce_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _coerce_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() == "true"


def resolve_settings(db: "dict | None", app_settings) -> StorageSettings:
    """合併 DB（前端可調）與 app_settings（建構時 .env/預設）→ StorageSettings。
    DB 有值且可解析 → 用 DB；否則回退 app_settings。"""
    def g(key, default):
        if db and key in db and db[key] is not None:
            return db[key]
        return default

    min_gb = _coerce_float(
        g("storage_min_free_gb", app_settings.storage_min_free_gb),
        app_settings.storage_min_free_gb,
    )
    return StorageSettings(
        check_interval_seconds=_coerce_int(
            g("storage_check_interval_seconds", app_settings.storage_check_interval_seconds),
            app_settings.storage_check_interval_seconds),
        min_free_bytes=int(min_gb * 1024**3),
        min_free_inodes_ratio=_coerce_float(
            g("storage_min_free_inodes_ratio", app_settings.storage_min_free_inodes_ratio),
            app_settings.storage_min_free_inodes_ratio),
        debounce_count=_coerce_int(
            g("storage_debounce_count", app_settings.storage_debounce_count),
            app_settings.storage_debounce_count),
        volume_marker=str(g("storage_volume_marker", app_settings.storage_volume_marker) or ""),
        schedule_enabled=_coerce_bool(
            g("recording_schedule_enabled", app_settings.recording_schedule_enabled),
            app_settings.recording_schedule_enabled),
        off_start_min=parse_hhmm(str(g("recording_off_start", app_settings.recording_off_start))),
        off_end_min=parse_hhmm(str(g("recording_off_end", app_settings.recording_off_end))),
    )
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: PASS（17 passed）

- [ ] **Step 5: Commit**

```bash
git add storage_monitor.py tests/test_storage_monitor.py
git commit -m "feat(storage): write_probe/marker/ephemeral 路徑/settings 解析"
```

---

## Task 3: `StorageMonitor` 狀態機 + target_mode + 告警轉換

**Files:**
- Modify: `storage_monitor.py`
- Test: `tests/test_storage_monitor.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_storage_monitor.py 追加
import asyncio
from datetime import datetime


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_target_mode_record_when_writable_and_recording_time(tmp_path, monkeypatch):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)  # 中午 → 錄影時段
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "record"


def test_target_mode_ephemeral_during_no_record_window(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 23, 0)  # 深夜 → no-record
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "ephemeral"


def test_target_mode_ephemeral_when_recording_disk_down(tmp_path):
    """錄影碟掛掉（探針失敗）但在錄影時段 → 自動轉 ephemeral（不 drop）。"""
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    rec_down = bad / "sub"             # probe 失敗
    eph_ok = tmp_path / "eph"
    _run(mon.run_once(recording_base=rec_down, ephemeral_base=eph_ok,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "ephemeral"


def test_target_mode_drop_when_both_down(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    _run(mon.run_once(recording_base=bad / "r", ephemeral_base=bad / "e",
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "drop"


def test_alert_fired_on_recording_disk_down_transition(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    fired = []

    async def cb(metric, cur, mean):
        fired.append(metric)

    _run(mon.run_once(recording_base=bad / "r", ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=cb))
    assert "storage_unwritable" in fired


def test_snapshot_has_expected_keys(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    snap = mon.get_snapshot()
    for k in ("recording_state", "ephemeral_state", "target_mode",
              "recording_time", "recording_free_gb"):
        assert k in snap
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py -k "target_mode or alert or snapshot" -q`
Expected: FAIL（`AttributeError: ... 'StorageMonitor'`）

- [ ] **Step 3: 寫最小實作（追加到 `storage_monitor.py`）**

```python
class StorageMonitor:
    """維護錄影碟/ephemeral 碟兩個遲滯狀態，合成單一 target_mode。
    feed/writer 以 get_target_mode() 讀取（cheap、有鎖）；背景 loop 呼叫 run_once。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._record_state = "ok"
        self._eph_state = "ok"
        self._record_count = 0
        self._eph_count = 0
        self._target_mode = "record"   # 啟動預設＝現狀錄影
        self._snapshot: dict = {
            "recording_state": "ok", "ephemeral_state": "ok",
            "target_mode": "record", "recording_time": True,
            "recording_free_gb": 0.0, "recording_free_ratio": 0.0,
            "ephemeral_free_gb": 0.0, "last_transition_ts": None,
        }

    def get_target_mode(self) -> str:
        with self._lock:
            return self._target_mode

    def get_snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def _read_base(self, base, settings: StorageSettings, check_marker: bool):
        probe = write_probe(base)
        marker = marker_present(base, settings.volume_marker) if check_marker else True
        try:
            free_bytes, free_ratio, free_inodes = check_free_space(base)
        except OSError:
            free_bytes, free_ratio, free_inodes, probe = 0, 0.0, 0.0, False
        reading = classify_health(probe, marker, free_bytes, free_inodes, settings)
        return reading, free_bytes, free_ratio

    async def run_once(self, *, recording_base, ephemeral_base,
                       settings: StorageSettings, now: datetime, alert_cb) -> None:
        rec_reading, rec_free, rec_ratio = self._read_base(recording_base, settings, True)
        eph_reading, eph_free, _ = self._read_base(ephemeral_base, settings, False)
        recording_time = is_recording_time(
            now, settings.off_start_min, settings.off_end_min, settings.schedule_enabled)

        with self._lock:
            prev_record = self._record_state
            self._record_state, self._record_count = next_state(
                self._record_state, rec_reading, self._record_count, settings.debounce_count)
            self._eph_state, self._eph_count = next_state(
                self._eph_state, eph_reading, self._eph_count, settings.debounce_count)
            new_record = self._record_state

            rec_writable = new_record != "down"
            eph_writable = self._eph_state != "down"
            if rec_writable and recording_time:
                mode = "record"
            elif eph_writable:
                mode = "ephemeral"
            else:
                mode = "drop"
            self._target_mode = mode

            transitioned = prev_record != new_record
            self._snapshot = {
                "recording_state": new_record,
                "ephemeral_state": self._eph_state,
                "target_mode": mode,
                "recording_time": recording_time,
                "recording_free_gb": round(rec_free / 1024**3, 2),
                "recording_free_ratio": round(rec_ratio, 4),
                "ephemeral_free_gb": round(eph_free / 1024**3, 2),
                "last_transition_ts": (now.timestamp() if transitioned
                                       else self._snapshot.get("last_transition_ts")),
            }

        if transitioned and alert_cb is not None:
            min_gb = settings.min_free_bytes / 1024**3
            free_gb = rec_free / 1024**3
            if new_record == "degraded" and prev_record == "ok":
                await alert_cb("storage_low_space", free_gb, min_gb)
            elif new_record == "down":
                await alert_cb("storage_unwritable", free_gb, min_gb)
            elif new_record == "ok" and prev_record == "down":
                await alert_cb("storage_recovered", free_gb, min_gb)


monitor = StorageMonitor()


def get_target_mode() -> str:
    """hls_manager feed/writer 的廉價讀取點。"""
    return monitor.get_target_mode()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: PASS（23 passed）

- [ ] **Step 5: Commit**

```bash
git add storage_monitor.py tests/test_storage_monitor.py
git commit -m "feat(storage): StorageMonitor 狀態機 + target_mode + 告警轉換"
```

---

## Task 4: `config.py` 新增設定預設

**Files:**
- Modify: `config.py:57-107`
- Test: `tests/test_config.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_config.py 追加（檔案開頭已有 from config import Settings 之類；若無則加）
def test_storage_and_encoder_defaults():
    from config import Settings
    s = Settings()
    assert s.hls_ephemeral_dir == "/dev/shm/pig_live"
    assert s.hls_crf == 23
    assert s.hls_video_codec == "libx264"
    assert s.storage_check_interval_seconds == 20
    assert s.storage_min_free_gb == 10.0
    assert s.storage_min_free_inodes_ratio == 0.02
    assert s.storage_debounce_count == 2
    assert s.storage_volume_marker == ""
    assert s.recording_schedule_enabled is True
    assert s.recording_off_start == "17:00"
    assert s.recording_off_end == "06:30"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_config.py::test_storage_and_encoder_defaults -q`
Expected: FAIL（`AttributeError: 'Settings' object has no attribute 'hls_ephemeral_dir'`）

- [ ] **Step 3: 寫實作**

在 `config.py` 的 HLS 區塊（`hls_retention_days` 之後，約 `63` 行下）插入：

```python
    # ── Ephemeral live + 編碼旋鈕 ──────────────────────────────
    # 夜間 no-record / 錄影碟掛掉時，live 改寫這裡（滾動 buffer、錄影碟零寫入）。
    # 預設 tmpfs（零磨耗）；不可用時由 storage_monitor.effective_ephemeral_dir 回退系統碟。
    hls_ephemeral_dir: str = "/dev/shm/pig_live"
    hls_crf: int = 23                  # 調高（如 28）→ 檔案變小、寫入量降，畫質降
    hls_video_codec: str = "libx264"

    # ── 儲存健康監控 ───────────────────────────────────────────
    storage_check_interval_seconds: int = 20
    storage_min_free_gb: float = 10.0
    storage_min_free_inodes_ratio: float = 0.02
    storage_debounce_count: int = 2
    storage_volume_marker: str = ""    # 掛載防誤判標記檔名（空＝不檢查）

    # ── 夜間 no-record 排程（前端可調）────────────────────────
    recording_schedule_enabled: bool = True
    recording_off_start: str = "17:00"   # 本地時間 HH:MM
    recording_off_end: str = "06:30"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_config.py::test_storage_and_encoder_defaults -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): ephemeral/編碼/儲存監控/夜間排程 設定預設"
```

---

## Task 5: `_make_ffmpeg_cmd` 支援 rolling + crf + codec

**Files:**
- Modify: `hls_manager.py:64-109`
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_hls_manager.py 追加
def test_ffmpeg_cmd_rolling_uses_delete_segments(tmp_path, monkeypatch):
    from hls_manager import _make_ffmpeg_cmd
    cmd = _make_ffmpeg_cmd(tmp_path, rolling=True)
    joined = " ".join(cmd)
    assert "delete_segments" in joined
    assert cmd[cmd.index("-hls_list_size") + 1] == "8"


def test_ffmpeg_cmd_uses_config_crf_and_codec(tmp_path, monkeypatch):
    from hls_manager import _make_ffmpeg_cmd
    monkeypatch.setattr("hls_manager.settings.hls_crf", 28, raising=False)
    monkeypatch.setattr("hls_manager.settings.hls_video_codec", "libx265", raising=False)
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-crf") + 1] == "28"
    assert cmd[cmd.index("-c:v") + 1] == "libx265"


def test_ffmpeg_cmd_default_still_keeps_all_segments(tmp_path):
    from hls_manager import _make_ffmpeg_cmd
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-hls_list_size") + 1] == "0"
    assert "delete_segments" not in " ".join(cmd)
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_manager.py -k ffmpeg_cmd -q`
Expected: FAIL（`_make_ffmpeg_cmd() got an unexpected keyword argument 'rolling'`）

- [ ] **Step 3: 寫實作**

把 `hls_manager.py` 的 `_make_ffmpeg_cmd`（`64-85`）整段換成：

```python
def _make_ffmpeg_cmd(out_dir: Path, start_number: int = 0, *,
                     rolling: bool = False) -> list[str]:
    gop = TARGET_FPS * 2
    crf = str(getattr(settings, "hls_crf", 23))
    codec = getattr(settings, "hls_video_codec", "libx264")
    # rolling=True（ephemeral live）：限制清單長度 + delete_segments → 滾動視窗、
    # 舊段自動刪、磁碟佔用恆定為數秒。rolling=False（錄影）：全留（現狀）。
    list_size = "8" if rolling else "0"
    flags = "append_list+program_date_time"
    if rolling:
        flags = "delete_segments+" + flags
    return [
        "ffmpeg", "-y",
        "-f", "mjpeg",
        "-framerate", str(TARGET_FPS),
        "-i", "pipe:0",
        "-an",
        "-c:v", codec,
        "-preset", "veryfast",
        "-crf", crf,
        "-g", str(gop),
        "-hls_time", str(_HLS_TIME),
        "-hls_list_size", list_size,
        "-hls_flags", flags,
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        "-start_number", str(start_number),
        "-loglevel", FFMPEG_LOG_LEVEL,
        str(out_dir / "index.m3u8"),
    ]
```

並把 `_start_ffmpeg`（`88-109`）的簽名與呼叫改為透傳 `rolling`：

```python
def _start_ffmpeg(out_dir: Path, start_number: int = 0, *,
                  rolling: bool = False) -> subprocess.Popen:
    stderr_target = (
        subprocess.PIPE
        if FFMPEG_LOG_LEVEL in ("debug", "info", "verbose")
        else subprocess.DEVNULL
    )
    proc = subprocess.Popen(
        _make_ffmpeg_cmd(out_dir, start_number=start_number, rolling=rolling),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=stderr_target,
    )
    if stderr_target == subprocess.PIPE:
        threading.Thread(
            target=_drain_stderr, args=(proc,), daemon=True,
            name=f"ffmpeg-stderr-{out_dir.name}",
        ).start()
    return proc
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_manager.py -k ffmpeg_cmd -q`
Expected: PASS（含既有 `test_ffmpeg_cmd_has_correct_hls_settings`）

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): _make_ffmpeg_cmd 支援 rolling + config crf/codec"
```

---

## Task 6: `HLSStream` 模式感知 feed/_restart/writer + ephemeral sidecar skip

**Files:**
- Modify: `hls_manager.py`（`HLSStream.__init__`/`feed`/`_restart`/`_restart_in_place`/`_writer_tick`/`_scan_new_segments`、`HLSManager.ensure_started`/新增 `active_out_dir`）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_hls_manager.py 追加
def test_feed_drops_frame_in_drop_mode(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "drop")
    before = len(stream._frame_buffer)
    stream.feed(b"jpegdata", capture_ts=123.0)
    assert len(stream._frame_buffer) == before     # 沒進 buffer
    assert stream._dropped_frames == 1


def test_feed_switches_to_ephemeral_dir(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    eph = tmp_path / "eph"
    monkeypatch.setattr("hls_manager._EPHEMERAL_BASE", str(eph))
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "ephemeral")
    # 攔 _restart 只記錄參數，不真的 spawn ffmpeg
    calls = {}
    def fake_restart(new_dir, *, rolling=False, mode="record"):
        calls["dir"] = new_dir; calls["rolling"] = rolling; calls["mode"] = mode
        stream.out_dir = new_dir; stream.mode = mode; stream.rolling = rolling
    monkeypatch.setattr(stream, "_restart", fake_restart)
    stream.feed(b"j", capture_ts=1.0)
    assert calls["mode"] == "ephemeral"
    assert calls["rolling"] is True
    assert calls["dir"].name == "_live"


def test_writer_tick_skips_when_drop(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "drop")
    proc.poll.return_value = 1            # 假裝 ffmpeg 死
    revived = {"n": 0}
    monkeypatch.setattr(stream, "_restart_in_place",
                        lambda: revived.__setitem__("n", revived["n"] + 1))
    stream._writer_tick()
    assert revived["n"] == 0              # drop 模式不 revive（不 spawn 失敗 ffmpeg）


def test_scan_new_segments_skips_sidecar_in_rolling(tmp_path, monkeypatch):
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream.rolling = True
    stream._emit_log.append((round(TARGET_FPS * _HLS_TIME), 1700.0))
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert not (stream.out_dir / "pdt.jsonl").exists()   # rolling 不寫 sidecar
    assert "seg_001.ts" in stream._seg_pdt               # 但 in-memory PDT 仍記（live 需要）


def test_active_out_dir_matches_hour(tmp_path, monkeypatch):
    from hls_manager import HLSManager
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    with patch("hls_manager._start_ffmpeg") as mk:
        mk.return_value = MagicMock(stdin=MagicMock(), poll=MagicMock(return_value=None))
        with patch("hls_manager.HLSStream._start_writer", lambda self: None):
            mgr = HLSManager.__new__(HLSManager)
            mgr._streams = {}
            mgr._lock = threading.Lock()
            d = mgr.ensure_started("cam_01", "rgb")
            assert mgr.active_out_dir("cam_01", "rgb", d.name) == d
            assert mgr.active_out_dir("cam_01", "rgb", "1999-01-01-00") is None
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_manager.py -k "drop or ephemeral or sidecar or active_out_dir" -q`
Expected: FAIL（`AttributeError: 'HLSStream' object has no attribute '_dropped_frames'` 等）

- [ ] **Step 3: 寫實作**

**(a)** `HLSStream.__init__`（約 `176` 行 `self._revive_count = 0` 之後）加：

```python
        # 模式感知（storage_monitor.target_mode）：record（小時目錄全留）/
        # ephemeral（_live 滾動 buffer）。drop 由 feed/writer 守衛處理。
        self.mode: str = "record"
        self.rolling: bool = False
        self._dropped_frames: int = 0
```

**(b)** 在 `_hour_dir`（`351`）之後新增目標選擇：

```python
    def _desired_target(self) -> tuple[str, "Optional[Path]"]:
        """依 storage_monitor.target_mode 決定 (mode, out_dir)。drop → (drop, None)。"""
        import storage_monitor
        mode = storage_monitor.get_target_mode()
        if mode == "drop":
            return "drop", None
        if mode == "ephemeral":
            return "ephemeral", (
                Path(_EPHEMERAL_BASE) / self.camera_id / self.stream_type / "_live"
            )
        return "record", self._hour_dir()
```

**(c)** 把 `feed`（`190-205`）整段換成：

```python
    def feed(self, jpeg_bytes: bytes, capture_ts: Optional[float] = None) -> None:
        """把新幀放入 buffer。依 target_mode 切換輸出目標：drop→丟幀；
        ephemeral/record 目標目錄變更→_restart。capture_ts 為真實擷取牆鐘。"""
        mode, target = self._desired_target()
        if mode == "drop":
            self._dropped_frames += 1
            return
        with self._lock:
            if mode != self.mode or target != self.out_dir:
                self._restart(target, rolling=(mode == "ephemeral"), mode=mode)
        if capture_ts is not None:
            self._last_capture_ts = capture_ts
        self.last_feed_time = time.time()
        self._frame_buffer.append((jpeg_bytes, capture_ts))
        self._buffer_event.set()
```

**(d)** `_restart`（`438-457`）簽名與內文改為帶 `rolling`/`mode`：

```python
    def _restart(self, new_dir: Path, *, rolling: bool = False,
                 mode: str = "record") -> None:
        """切換輸出目標（小時 rollover 或 record↔ephemeral 模式切換）時重啟 ffmpeg。"""
        with self._proc_lock:
            self._close_proc()
            new_dir.mkdir(parents=True, exist_ok=True)
            self.proc = _start_ffmpeg(new_dir, rolling=rolling)
            self.out_dir = new_dir
            self.mode = mode
            self.rolling = rolling
        with self._seg_lock:
            self._seg_pdt.clear()
            self._seen_segs.clear()
        self._emit_log.clear()
        self._emit_idx = 0
        self._seg_index_offset = 0
        self._writer_last_frame = None
        self._last_scan = 0.0
        logger.info(
            f"HLS {self.camera_id}/{self.stream_type} → {new_dir} (mode={mode})"
        )
```

**(e)** `_restart_in_place`（`477-479` 附近的 `_start_ffmpeg` 呼叫）改透傳目前 rolling：

```python
        with self._proc_lock:
            self._close_proc()
            self.proc = _start_ffmpeg(self.out_dir, start_number=next_num,
                                      rolling=self.rolling)
```

**(f)** `_writer_tick`（`387-391` 開頭）加 drop 守衛：

```python
    def _writer_tick(self) -> None:
        import storage_monitor
        if storage_monitor.get_target_mode() == "drop":
            return  # drop：不 emit、不 revive（磁碟死時不 spawn 失敗 ffmpeg）
        if self.proc.poll() is not None:
            try:
                self._restart_in_place()
            except Exception as e:
                logger.error(
                    f"[{self.camera_id}/{self.stream_type}] revive failed: {e}"
                )
                time.sleep(2.0)
            return
        # ...（以下既有取幀/_emit_frame 邏輯不變）
```

**(g)** `_scan_new_segments` 的 sidecar 寫入迴圈（`251-256`）加 rolling 守衛：

```python
        for seg_name, cap in new_rows:
            if self.rolling:
                continue  # ephemeral：不寫 pdt.jsonl（夜間不需 VOD、省寫入）
            try:
                with (out_dir / "pdt.jsonl").open("a") as fh:
                    fh.write(json.dumps({"seg": seg_name, "pdt": cap}) + "\n")
            except OSError as e:
                logger.warning(f"[{self.camera_id}/{self.stream_type}] sidecar write failed: {e}")
```

**(h)** 模組頂部（`FRAME_BUFFER_SIZE` 定義後，約 `126` 行）加 ephemeral base 解析：

```python
import storage_monitor as _sm
_EPHEMERAL_BASE: str = _sm.effective_ephemeral_dir(
    getattr(settings, "hls_ephemeral_dir", "/dev/shm/pig_live")
)
```

**(i)** `HLSManager.ensure_started`（`524-538`）改模式感知 + 新增 `active_out_dir`：

```python
    def ensure_started(self, camera_id: str, stream_type: str) -> Path:
        import storage_monitor
        key: StreamKey = (camera_id, stream_type)
        with self._lock:
            if key not in self._streams:
                mode = storage_monitor.get_target_mode()
                rolling = mode == "ephemeral"
                if rolling:
                    out_dir = Path(_EPHEMERAL_BASE) / camera_id / stream_type / "_live"
                else:
                    out_dir = (Path(settings.hls_base_dir) / camera_id / stream_type
                               / datetime.now().strftime("%Y-%m-%d-%H"))
                out_dir.mkdir(parents=True, exist_ok=True)
                proc = _start_ffmpeg(out_dir, rolling=rolling)
                stream = HLSStream(camera_id, stream_type, proc, out_dir)
                stream.mode = mode if mode != "drop" else "record"
                stream.rolling = rolling
                self._streams[key] = stream
                logger.info(f"Started HLS stream {camera_id}/{stream_type} → {out_dir} (mode={mode})")
            return self._streams[key].out_dir

    def active_out_dir(self, camera_id: str, stream_type: str,
                       date_hour: str) -> "Optional[Path]":
        """若有 active stream 且其 out_dir.name 命中 date_hour（含 ephemeral '_live'）
        → 回該 out_dir（serve_hls 據此撈 .ts，不限定 hls_base）；否則 None。"""
        with self._lock:
            stream = self._streams.get((camera_id, stream_type))
        if stream is not None and stream.out_dir.name == date_hour:
            return stream.out_dir
        return None
```

> 注意：`ensure_started` 的 `import storage_monitor` 放在函式內以避免模組載入順序問題（`hls_manager` 頂部已 `import storage_monitor as _sm`，函式內再 `import storage_monitor` 取同一模組亦可；二者皆指同一單例）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_manager.py -q`
Expected: PASS（既有 + 新增；既有 record-mode 測試因預設 target_mode="record" 不受影響）

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): 模式感知輸出（record/ephemeral/drop）+ active_out_dir + ephemeral sidecar skip"
```

---

## Task 7: `serve_hls` 從 active stream out_dir 撈 segment

**Files:**
- Modify: `routers/stream.py:15-32`
- Test: `tests/test_stream_router.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_stream_router.py 追加（若檔不存在則建，頂部 import 見下）
from pathlib import Path
from unittest.mock import MagicMock
from fastapi.testclient import TestClient


def test_serve_hls_serves_ts_from_active_ephemeral_dir(tmp_path, monkeypatch):
    """ephemeral 模式 .ts 在 /dev/shm（非 hls_base）；serve_hls 須從 active
    stream 的 out_dir 撈得到，而非只看 hls_base。"""
    import hls_manager
    from main import app

    eph_dir = tmp_path / "eph" / "cam_01" / "rgb" / "_live"
    eph_dir.mkdir(parents=True)
    (eph_dir / "seg_005.ts").write_bytes(b"TSDATA")

    monkeypatch.setattr(hls_manager.hls_manager, "active_out_dir",
                        lambda cam, st, dh: eph_dir if dh == "_live" else None)
    monkeypatch.setattr(hls_manager.hls_manager, "corrected_m3u8",
                        lambda cam, st, dh: None)

    client = TestClient(app)
    resp = client.get("/stream/hls/cam_01/rgb/_live/seg_005.ts")
    assert resp.status_code == 200
    assert resp.content == b"TSDATA"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_stream_router.py::test_serve_hls_serves_ts_from_active_ephemeral_dir -q`
Expected: FAIL（404，因舊邏輯只看 `hls_base_dir`）

- [ ] **Step 3: 寫實作**

把 `routers/stream.py` 的 `serve_hls`（`15-32`）整段換成：

```python
@router.get("/hls/{camera_id}/{stream_type}/{date_hour}/{filename}")
async def serve_hls(
    camera_id: str, stream_type: str, date_hour: str, filename: str
):
    # live playlist：回傳後端自管 PDT 版本（非當前小時/無 stream → None → fallback）。
    if filename == "index.m3u8":
        corrected = hls_manager.corrected_m3u8(camera_id, stream_type, date_hour)
        if corrected is not None:
            return PlainTextResponse(
                corrected, media_type="application/vnd.apple.mpegurl"
            )
    # 先試 active stream 的當前 out_dir（含 ephemeral /dev/shm）；命中 date_hour 才用。
    active_dir = hls_manager.active_out_dir(camera_id, stream_type, date_hour)
    if active_dir is not None:
        ad = active_dir.resolve()
        fp = (ad / filename).resolve()
        if fp.is_relative_to(ad) and fp.exists():
            return FileResponse(fp)
    # fallback：歷史錄影一律在 hls_base_dir。
    base = Path(settings.hls_base_dir).resolve()
    file_path = (base / camera_id / stream_type / date_hour / filename).resolve()
    if not file_path.is_relative_to(base) or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_stream_router.py::test_serve_hls_serves_ts_from_active_ephemeral_dir -q`
Expected: PASS

> 註：若 `from main import app` 因待辦 #12（`.env` 缺 `ZMQ_SOURCES`）在此環境 collection error，於測試頂部加 `monkeypatch`/env 設定，或用既有 `tests/conftest.py` 的 fixture（與既有 `test_stream_router` 同樣處理方式）。此為既有環境問題，非本任務回歸。

- [ ] **Step 5: Commit**

```bash
git add routers/stream.py tests/test_stream_router.py
git commit -m "feat(stream): serve_hls 從 active stream out_dir 撈 segment（支援 ephemeral）"
```

---

## Task 8: `GET /storage/health` endpoint

**Files:**
- Modify: `routers/storage.py`
- Test: `tests/test_storage_router.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_storage_router.py 追加
from fastapi.testclient import TestClient


def test_storage_health_returns_snapshot(monkeypatch):
    import storage_monitor
    from main import app
    monkeypatch.setattr(storage_monitor.monitor, "get_snapshot",
                        lambda: {"target_mode": "record", "recording_state": "ok"})
    client = TestClient(app)
    resp = client.get("/storage/health")
    assert resp.status_code == 200
    assert resp.json()["target_mode"] == "record"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_router.py::test_storage_health_returns_snapshot -q`
Expected: FAIL（404 Not Found）

- [ ] **Step 3: 寫實作**

在 `routers/storage.py` 頂部 import 區加：

```python
from storage_monitor import monitor as storage_health_monitor
```

並在檔案結尾（最後一個 route 之後）加：

```python
@router.get("/health")
async def get_storage_health():
    """儲存健康快照（前端 header 狀態小燈輪詢）。不需 DB。"""
    return storage_health_monitor.get_snapshot()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_router.py::test_storage_health_returns_snapshot -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add routers/storage.py tests/test_storage_router.py
git commit -m "feat(storage): GET /storage/health 回傳健康快照"
```

---

## Task 9: `routers/settings.py` 開放新鍵

**Files:**
- Modify: `routers/settings.py:9-40`
- Test: `tests/test_settings_router.py`

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_settings_router.py 追加
def test_storage_keys_in_allowed():
    from routers.settings import ALLOWED_KEYS
    for k in ("storage_min_free_gb", "storage_check_interval_seconds",
              "storage_min_free_inodes_ratio", "storage_debounce_count",
              "storage_volume_marker", "recording_schedule_enabled",
              "recording_off_start", "recording_off_end"):
        assert k in ALLOWED_KEYS
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings_router.py::test_storage_keys_in_allowed -q`
Expected: FAIL（KeyError/assert）

- [ ] **Step 3: 寫實作**

把 `routers/settings.py` 的 `ALLOWED_KEYS`（`9-15`）改為：

```python
ALLOWED_KEYS = frozenset({
    "analysis_interval_minutes",
    "analysis_window_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
    "temp_anomaly_enabled",
    # 儲存健康監控（storage_monitor loop 每輪讀 DB → 即時生效、不需 reload）
    "storage_check_interval_seconds",
    "storage_min_free_gb",
    "storage_min_free_inodes_ratio",
    "storage_debounce_count",
    "storage_volume_marker",
    # 夜間 no-record 排程
    "recording_schedule_enabled",
    "recording_off_start",
    "recording_off_end",
})
```

並在 GET fallback dict（`33-39`，`pool is None` 分支）加上對應預設（其餘鍵不變）：

```python
            "storage_check_interval_seconds": str(app_settings.storage_check_interval_seconds),
            "storage_min_free_gb":            str(app_settings.storage_min_free_gb),
            "storage_min_free_inodes_ratio":  str(app_settings.storage_min_free_inodes_ratio),
            "storage_debounce_count":         str(app_settings.storage_debounce_count),
            "storage_volume_marker":          app_settings.storage_volume_marker,
            "recording_schedule_enabled":     str(app_settings.recording_schedule_enabled).lower(),
            "recording_off_start":            app_settings.recording_off_start,
            "recording_off_end":              app_settings.recording_off_end,
```

> 不加入 `_RELOAD_KEYS`：這些鍵由 `_storage_monitor_loop` 每輪讀 DB 取用（Task 10），≤一輪間隔生效，不需 scheduler reload。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_settings_router.py -q`
Expected: PASS（既有 + 新增）

- [ ] **Step 5: Commit**

```bash
git add routers/settings.py tests/test_settings_router.py
git commit -m "feat(settings): 開放 storage_* / recording_* 鍵（即時生效）"
```

---

## Task 10: `main.py` 起 storage monitor loop

**Files:**
- Modify: `main.py:11-73`
- Test: `tests/test_storage_monitor.py`（loop 的 alert 回呼純函式測試）

- [ ] **Step 1: 寫失敗測試**

```python
# tests/test_storage_monitor.py 追加（驗證 main 的 alert 回呼會呼叫 write_health_alert）
def test_main_storage_alert_writes_health_alert(monkeypatch):
    import asyncio
    import main
    captured = {}

    async def fake_write(pool, *, camera_id, object_id, metric,
                         current_value, mean_value, std_value):
        captured.update(camera_id=camera_id, metric=metric, object_id=object_id)
        return 1

    monkeypatch.setattr(main, "write_health_alert", fake_write, raising=False)
    monkeypatch.setattr(main.database, "get_pool", lambda: object())
    asyncio.get_event_loop().run_until_complete(
        main._storage_alert("storage_unwritable", 3.0, 10.0)
    )
    assert captured["metric"] == "storage_unwritable"
    assert captured["camera_id"] == "_system"
    assert captured["object_id"] == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py::test_main_storage_alert_writes_health_alert -q`
Expected: FAIL（`AttributeError: module 'main' has no attribute '_storage_alert'`）

- [ ] **Step 3: 寫實作**

`main.py` import 區（`15-16` 附近）補：

```python
from db_writer import get_all_settings, get_protected_hours, write_health_alert
import storage_monitor
from datetime import datetime
```

（`get_all_settings`/`get_protected_hours` 已 import，補 `write_health_alert`；`datetime`/`storage_monitor` 新增。）

在 `_retention_loop` 定義之後、`lifespan` 之前加：

```python
async def _storage_alert(metric: str, current_value: float, mean_value: float) -> None:
    """storage_monitor 狀態轉換 → 寫一筆系統級 health_alert（進通知中心，
    不碰 get_anomaly_cache → 不亂亮紅框）。DB 不可用 → 只 log。"""
    pool = database.get_pool()
    if pool is None:
        logger.error(f"storage alert {metric} free={current_value:.1f}GB 但 DB 不可用")
        return
    try:
        await write_health_alert(
            pool, camera_id="_system", object_id=0, metric=metric,
            current_value=float(current_value), mean_value=float(mean_value),
            std_value=0.0,
        )
    except Exception as e:
        logger.error(f"寫 storage alert 失敗：{e}")


async def _storage_monitor_loop() -> None:
    """每 storage_check_interval_seconds 量錄影碟/ephemeral 碟健康 → 更新
    target_mode（hls_manager 讀取）+ 狀態轉換時告警。設定每輪讀 DB（即時生效）。
    首輪立即跑（讓 target_mode 早就緒）。"""
    eph_base = storage_monitor.effective_ephemeral_dir(app_settings.hls_ephemeral_dir)
    while True:
        interval = app_settings.storage_check_interval_seconds
        try:
            pool = database.get_pool()
            db_settings = await get_all_settings(pool) if pool is not None else None
            s = storage_monitor.resolve_settings(db_settings, app_settings)
            interval = max(5, s.check_interval_seconds)
            await storage_monitor.monitor.run_once(
                recording_base=app_settings.hls_base_dir,
                ephemeral_base=eph_base,
                settings=s,
                now=datetime.now(),
                alert_cb=_storage_alert,
            )
        except Exception as e:
            logger.warning(f"storage monitor loop 錯誤：{e}")
        await asyncio.sleep(interval)
```

在 `lifespan` 內（`66` 行 `retention_task = ...` 之後）加：

```python
    storage_task = asyncio.create_task(_storage_monitor_loop())
```

並在 shutdown 段（`68` 行 `retention_task.cancel()` 之後）加：

```python
    storage_task.cancel()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py -q`
Expected: PASS（含新 main alert 測試）

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_storage_monitor.py
git commit -m "feat(main): 起 storage_monitor 背景 loop + 系統級告警回呼"
```

---

## Task 11: 前端 header 狀態小燈 + 設定面板欄位

**Files:**
- Modify: `static/index.html`

> 此檔 ~800+ 行，inline `<script>` 起始行會隨改動位移。用 `grep -n "^  <script>" static/index.html` 找 script 起始行供 `node --check` 抽取；插入點用既有字串錨定（見各步驟）。

- [ ] **Step 1: 加 header 狀態小燈 DOM + 樣式**

用 `grep -n "id=\"bell" static/index.html` 找通知 bell 所在的 header 區塊，在其旁插入：

```html
<span id="storage-pill" class="storage-pill" title="儲存狀態" style="display:none">●</span>
```

並在既有 `<style>` 區塊（`grep -n "</style>"`）前加：

```css
.storage-pill { font-size: 14px; margin-left: 8px; cursor: default; }
.storage-pill.ok { color: #2ecc71; }
.storage-pill.degraded { color: #f39c12; }
.storage-pill.down { color: #e74c3c; }
```

- [ ] **Step 2: 加輪詢函式**

用 `grep -n "function startLiveTimers" static/index.html` 找既有 timer helper 區，在其附近加：

```javascript
async function pollStorageHealth() {
  try {
    const r = await fetch('/storage/health');
    if (!r.ok) return;
    const h = await r.json();
    const pill = document.getElementById('storage-pill');
    if (!pill) return;
    const mode = h.target_mode || 'record';
    const recState = h.recording_state || 'ok';
    let cls = 'ok', label = '錄影中';
    if (mode === 'drop') { cls = 'down'; label = '儲存故障：丟幀'; }
    else if (recState === 'down') { cls = 'down'; label = '錄影碟故障 → ephemeral live'; }
    else if (mode === 'ephemeral') { cls = 'degraded'; label = h.recording_time === false ? '夜間不錄影（live 中）' : 'ephemeral live'; }
    else if (recState === 'degraded') { cls = 'degraded'; label = `空間不足（剩 ${h.recording_free_gb}GB）`; }
    pill.className = 'storage-pill ' + cls;
    pill.title = label;
    pill.style.display = '';
  } catch (e) { /* 靜默：監控小燈非關鍵路徑 */ }
}
setInterval(pollStorageHealth, 20000);
pollStorageHealth();
```

- [ ] **Step 3: 設定面板加欄位**

用 `grep -n "analysis_window_minutes\|temp_anomaly_enabled" static/index.html` 找設定面板既有欄位區，仿其樣式加入（id 對齊 settings key）：

```html
<label>夜間不錄影
  <input type="checkbox" id="set-recording_schedule_enabled">
</label>
<label>不錄影起 <input type="time" id="set-recording_off_start" value="17:00"></label>
<label>不錄影迄 <input type="time" id="set-recording_off_end" value="06:30"></label>
<label>最低可用空間 (GB) <input type="number" id="set-storage_min_free_gb" min="1" step="1"></label>
<label>監控間隔 (秒) <input type="number" id="set-storage_check_interval_seconds" min="5" step="5"></label>
```

- [ ] **Step 4: 設定 load/save 接上新欄位**

用 `grep -n "function loadSettings\|function saveSettings" static/index.html` 找既有設定載入/儲存函式，在其 body 仿既有鍵加入（checkbox 用 `.checked`、其餘 `.value`）：

```javascript
// loadSettings 內，取得 data 後：
const sse = document.getElementById('set-recording_schedule_enabled');
if (sse) sse.checked = String(data.recording_schedule_enabled) === 'true';
const m = {
  'set-recording_off_start': 'recording_off_start',
  'set-recording_off_end': 'recording_off_end',
  'set-storage_min_free_gb': 'storage_min_free_gb',
  'set-storage_check_interval_seconds': 'storage_check_interval_seconds',
};
for (const [id, key] of Object.entries(m)) {
  const el = document.getElementById(id);
  if (el && data[key] != null) el.value = data[key];
}

// saveSettings 內，組 body 時加：
body.recording_schedule_enabled = String(document.getElementById('set-recording_schedule_enabled').checked);
body.recording_off_start = document.getElementById('set-recording_off_start').value;
body.recording_off_end = document.getElementById('set-recording_off_end').value;
body.storage_min_free_gb = document.getElementById('set-storage_min_free_gb').value;
body.storage_check_interval_seconds = document.getElementById('set-storage_check_interval_seconds').value;
```

- [ ] **Step 5: 語法檢查**

```bash
START=$(grep -n "^  <script>" static/index.html | tail -1 | cut -d: -f1)
END=$(grep -n "^  </script>" static/index.html | tail -1 | cut -d: -f1)
sed -n "$((START+1)),$((END-1))p" static/index.html > "$CLAUDE_JOB_DIR/tmp/idx.js" && node --check "$CLAUDE_JOB_DIR/tmp/idx.js" && echo "JS OK"
```

Expected: `JS OK`

- [ ] **Step 6: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 儲存狀態小燈 + 夜間排程/空間門檻設定欄位"
```

---

## Task 12: 全套件回歸 + 收尾

**Files:** 無（驗證）

- [ ] **Step 1: 全套件**

Run: `uv run pytest -q`
Expected: 既有 4 個 ZMQ_SOURCES OS-env gap 失敗（待辦 #12）外，**零本次新回歸**；新增 storage_monitor / hls / stream / storage / settings / config 測試全綠。

- [ ] **Step 2: record-mode 不變量手測（讀碼確認）**

確認預設 `target_mode="record"`（首輪 loop 前）+ `is_recording_time` 在白天回 True → `_make_ffmpeg_cmd` 預設 rolling=False、list_size=0、crf=23、libx264 → **record 行為與現狀逐位元相容**（PDT/VOD/小時 rollover 不動）。

- [ ] **Step 3: Commit（如有收尾調整）**

```bash
git add -A && git commit -m "test: 儲存韌性全套件回歸通過"
```

---

## Self-Review 對照 spec

- **§0 根因 / §11 不變量** → Task 12 Step 2 驗證 record-mode 逐位元相容；告警走 health_alerts（Task 10）不碰 anomaly_cache。
- **§2 純函式 + 探針 + 狀態 + target_mode** → Task 1/2/3。
- **§3 背景 loop（讀 DB、首輪即跑）** → Task 10。
- **§4 hls 整合（目標選擇/_restart/writer drop/ephemeral sidecar skip/serve_hls）** → Task 6/7。
- **§5 告警（storage_low_space/unwritable/recovered，sentinel camera）** → Task 3（轉換）+ Task 10（寫入）。
- **§6 /storage/health + 前端小燈** → Task 8/11。
- **§7 編碼旋鈕（crf/codec，env-only，預設不變）** → Task 4/5。
- **§8 設定（DB-backed via /settings，含排程 + 門檻）** → Task 4/9 + 前端 Task 11。
- **§9 夜間 ephemeral（固定 _live、滾動、無小時 rollover、handoff 自動續播）** → Task 6（`_desired_target` ephemeral 回固定 `_live`、`feed` 只在目標變更時 restart→ephemeral 不會每小時 restart）+ Task 7（serve）。
- **§10 邊界** → drop 雙故障（Task 3 `test_target_mode_drop_when_both_down`）；DB 不可用告警退化（Task 10 `_storage_alert`）；ephemeral fallback（Task 2 `effective_ephemeral_dir`）。

**Placeholder 掃描**：無 TBD/TODO；每個 code step 皆含完整程式碼。
**型別一致**：`get_target_mode()`、`active_out_dir()`、`_desired_target()`、`StorageSettings`、`run_once(...)`、`_storage_alert(metric,current,mean)` 跨任務簽名一致；`metric` 值（`storage_low_space`/`storage_unwritable`/`storage_recovered`）≤32 字元、`camera_id='_system'`≤16。
