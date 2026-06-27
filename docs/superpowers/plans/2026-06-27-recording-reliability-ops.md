# 錄影可靠性 + ops 推播 + 夜間省電 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓錄影獨立於觀看者並自我復活（修白天突然停錄）、把儲存/錄影異常用 ntfy 推播到手機、並支援夜間排程停 GPU 省電。

**Architecture:** 三個內聚子系統。(1) `main.py` 新增錄影監督者 loop 每 10s 為每個攝影機確保錄影串流存在，搭配 `zmq_receiver` / `HLSStream._restart` 例外硬化。(2) 新 `ntfy_notifier.py` 純傳輸模組，由 `main._storage_alert` 依 metric 推播。(3) `storage_monitor` 既有每 ~20s DB tick 順便算夜間 GPU 排程並設 `inference_pipeline` 旗標、`_process_batch` 開頭閘門。

**Tech Stack:** Python 3 / FastAPI / asyncio / threading / httpx（已在依賴，0.28.1）/ pytest / loguru。

## Global Constraints

- spec：`docs/superpowers/specs/2026-06-27-recording-reliability-ops-design.md`
- ntfy 預設 endpoint：`https://ntfy.ed716.duckdns.org/pig`（精確字串，勿改）
- 新設定預設值（精確）：`ntfy_enabled=True`、`gpu_off_schedule_enabled=False`、`gpu_off_start="22:00"`、`gpu_off_end="06:00"`
- DB-backed 設定即時生效模式：沿用既有「storage loop 每輪讀 DB」與 settings router `ALLOWED_KEYS`，**不新增 reload 機制**。
- 既有 live DB 無 migration 系統：`sql/init.sql` 只在首次初始化跑，新 seed 用 `ON CONFLICT DO NOTHING`。
- ntfy 推播**不含**豬隻健康告警（避免推播被洗版）。
- 測試命令：`uv run pytest tests/<file> -v`；既有 4 個 ZMQ_SOURCES OS-env gap 失敗為已知非回歸。
- 推論時鐘/PDT 等既有機制零改動；GPU 不卸載模型（僅跳過計算）。

---

### Task 1: config.py 新增所有新設定欄位

**Files:**
- Modify: `config.py:65-83`（HLS / ephemeral / storage / recording 區塊後）
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `settings.ntfy_url: str`、`settings.ntfy_enabled: bool`、`settings.gpu_off_schedule_enabled: bool`、`settings.gpu_off_start: str`、`settings.gpu_off_end: str`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_config.py` 末尾：

```python
def test_new_ops_settings_defaults():
    from config import Settings
    s = Settings(_env_file=None)
    assert s.ntfy_url == "https://ntfy.ed716.duckdns.org/pig"
    assert s.ntfy_enabled is True
    assert s.gpu_off_schedule_enabled is False
    assert s.gpu_off_start == "22:00"
    assert s.gpu_off_end == "06:00"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_config.py::test_new_ops_settings_defaults -v`
Expected: FAIL（`AttributeError` 或預設值不符）

- [ ] **Step 3: 加欄位**

在 `config.py` 的 `recording_off_end: str = "06:30"`（行 82）之後、`# ── Logging` 區塊之前插入：

```python

    # ── ntfy 推播通知（ops/儲存異常 → 手機）────────────────────
    ntfy_url: str = "https://ntfy.ed716.duckdns.org/pig"
    ntfy_enabled: bool = True

    # ── 夜間停 GPU 省電（獨立排程；預設關閉，零行為改變）────────
    gpu_off_schedule_enabled: bool = False
    gpu_off_start: str = "22:00"   # 本地時間 HH:MM
    gpu_off_end: str = "06:00"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_config.py::test_new_ops_settings_defaults -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat(config): 新增 ntfy 與夜間停 GPU 設定欄位"
```

---

### Task 2: zmq_receiver 接收迴圈例外硬化

**Files:**
- Modify: `zmq_receiver.py:83`（`_source_worker` 的 `on_frame(...)` 呼叫）
- Test: `tests/test_zmq_receiver.py`（新增；若既有 collection error，測試獨立可跑）

**Interfaces:**
- Produces: `_source_worker` 對 `on_frame` 例外不再 break 接收迴圈。

- [ ] **Step 1: 寫失敗測試**

新增 `tests/test_zmq_receiver_hardening.py`（獨立檔，避開既有 collection error）：

```python
import threading
import struct
import zmq_receiver as zr


def test_on_frame_exception_does_not_break_loop(monkeypatch):
    """on_frame 拋例外時，_source_worker 應 continue 收下一幀而非結束。"""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("boom")

    # 偽造一個會吐兩幀後停止的 socket。
    hdr = struct.Struct("dQII")
    import time
    payload = b"topic\x00" + hdr.pack(time.time(), 1, 0, 0)

    class FakeSock:
        def __init__(self):
            self.n = 0
        def setsockopt(self, *a, **k): pass
        def setsockopt_string(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def poll(self, ms): return 1
        def recv(self): return payload
        def close(self): pass

    class FakeCtx:
        def socket(self, *a, **k): return FakeSock()
        def term(self): pass

    monkeypatch.setattr(zr.zmq, "Context", lambda: FakeCtx())
    monkeypatch.setattr(zr.settings, "zmq_warmup_secs", 0.0)
    monkeypatch.setattr(zr.settings, "zmq_stale_ms", 10_000.0)

    running = threading.Event()
    running.set()

    cfg = zr.ZmqSource(name="t", src_host="h", src_port=1,
                       src_topic="topic", label="cam")

    def stop_after():
        # 讓迴圈跑幾輪後停止
        import time as _t
        _t.sleep(0.2)
        running.clear()

    th = threading.Thread(target=stop_after)
    th.start()
    zr._source_worker(cfg, running, boom)
    th.join()

    # 若例外有 break loop，calls 會卡在 1；硬化後應 >= 2。
    assert calls["n"] >= 2
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_zmq_receiver_hardening.py -v`
Expected: FAIL（`calls["n"] == 1`，例外 break 了迴圈）

- [ ] **Step 3: 硬化 on_frame 呼叫**

把 `zmq_receiver.py:83` 的：

```python
            on_frame(cfg.label, ts, frame_id, rgb_bytes, thermal_bytes)
            recv_count += 1
```

改成：

```python
            try:
                on_frame(cfg.label, ts, frame_id, rgb_bytes, thermal_bytes)
            except Exception as e:
                # 單一幀處理失敗（含 HLS feed/_restart 例外）不可殺掉整條
                # 攝影機接收 thread——否則該攝影機永久停錄，需重啟。
                logger.warning(f"{tag} on_frame error (continuing): {e}")
                continue
            recv_count += 1
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_zmq_receiver_hardening.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add zmq_receiver.py tests/test_zmq_receiver_hardening.py
git commit -m "fix(zmq): on_frame 例外不再殺掉攝影機接收 thread"
```

---

### Task 3: HLSStream._restart 例外硬化

**Files:**
- Modify: `hls_manager.py:479-499`（`_restart`）
- Test: `tests/test_hls_manager.py`

**Interfaces:**
- Produces: `_restart` 在 `mkdir`/`_start_ffmpeg` 失敗時 log + return，不向上拋。

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_hls_manager.py`（沿用既有 `_make_stream` fixture）：

```python
def test_restart_swallows_spawn_failure(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    # 讓 _start_ffmpeg 噴錯，模擬整點換目錄時 spawn 失敗
    monkeypatch.setattr("hls_manager._start_ffmpeg",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("spawn fail")))
    new_dir = tmp_path / "newhour"
    # 不應拋例外（過去會冒泡到 feed → zmq thread）
    stream._restart(new_dir, rolling=False, mode="record")
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_manager.py::test_restart_swallows_spawn_failure -v`
Expected: FAIL（`OSError` 拋出）

- [ ] **Step 3: 硬化 _restart**

把 `hls_manager.py` 的 `_restart` 主體（行 482-488 的 `with self._proc_lock:` 區塊）改成包 try/except：

```python
        with self._proc_lock:
            self._close_proc()
            try:
                new_dir.mkdir(parents=True, exist_ok=True)
                self.proc = _start_ffmpeg(new_dir, rolling=rolling)
            except OSError as e:
                # 整點換目錄 / 模式切換失敗：不可向上拋（會冒泡到 feed →
                # zmq thread 死）。保留舊 out_dir，交給 writer poll() 自癒
                # 或錄影監督者下一輪重建。
                logger.warning(
                    f"[{self.camera_id}/{self.stream_type}] _restart 失敗（{e}），"
                    "保留舊狀態待自癒"
                )
                return
            self.out_dir = new_dir
            self.mode = mode
            self.rolling = rolling
```

（其後的 `with self._seg_lock:` 重置區塊維持不變。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_manager.py::test_restart_swallows_spawn_failure -v`
Expected: PASS

- [ ] **Step 5: 跑整檔回歸**

Run: `uv run pytest tests/test_hls_manager.py -v`
Expected: 全 PASS（既有測試不回歸）

- [ ] **Step 6: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "fix(hls): _restart spawn 失敗不向上拋、保留舊狀態待自癒"
```

---

### Task 4: HLSManager 追蹤 last_seen + has_stream + desired_recording_keys

**Files:**
- Modify: `hls_manager.py:551-627`（`HLSManager`：`__init__`、`feed`，新增方法）
- Test: `tests/test_hls_manager.py`

**Interfaces:**
- Consumes: `HLSManager.ensure_started(camera_id, stream_type)`（既有）
- Produces:
  - `HLSManager.has_stream(camera_id, stream_type) -> bool`
  - `HLSManager.desired_recording_keys(cameras: list[str]) -> list[tuple[str, str]]`（rgb 一律含；thermal 僅當 `(cam,"thermal")` 於 `_THERMAL_SEEN_WINDOW` 秒內被 feed 過）
  - `feed()` 對任何 (cam, stream_type)（含不存在的 stream）更新 `self._last_seen`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_hls_manager.py`：

```python
def test_desired_recording_keys_rgb_always_thermal_when_seen(manager, monkeypatch):
    # rgb 一律含
    keys = manager.desired_recording_keys(["cam_01"])
    assert ("cam_01", "rgb") in keys
    assert ("cam_01", "thermal") not in keys

    # feed 一筆 thermal（即使沒有 active stream，也應記 last_seen）
    manager.feed("cam_01", "thermal", b"\xff\xd8", capture_ts=None)
    keys2 = manager.desired_recording_keys(["cam_01"])
    assert ("cam_01", "thermal") in keys2


def test_has_stream_reflects_streams(manager):
    assert manager.has_stream("cam_01", "rgb") is False
    manager.ensure_started("cam_01", "rgb")
    assert manager.has_stream("cam_01", "rgb") is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_manager.py::test_desired_recording_keys_rgb_always_thermal_when_seen tests/test_hls_manager.py::test_has_stream_reflects_streams -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 實作**

在 `hls_manager.py` 的 `_HLS_TIME` 常數附近（檔案頂部常數區）新增：

```python
# 錄影監督者：thermal 串流僅在「近期確實有送 thermal 幀」時才確保（避免對
# 從未裝 thermal 的攝影機平白起一條永遠無資料的 ffmpeg）。
_THERMAL_SEEN_WINDOW: float = 60.0
```

在 `HLSManager.__init__`（行 555-565）的 `self._streams` 之後新增：

```python
        self._last_seen: Dict[StreamKey, float] = {}
```

在 `HLSManager.feed`（行 611-626）最前面（`key = ...` 之後、`with self._lock:` 之前）插入：

```python
        self._last_seen[key] = time.time()
```

在 `HLSManager` 末尾（`_watchdog_loop` 之後）新增方法：

```python
    def has_stream(self, camera_id: str, stream_type: str) -> bool:
        with self._lock:
            return (camera_id, stream_type) in self._streams

    def desired_recording_keys(self, cameras: list[str]) -> list[StreamKey]:
        """錄影監督者要確保的串流：每攝影機 rgb 一律含；thermal 僅當近期
        （_THERMAL_SEEN_WINDOW 秒內）確實送過 thermal 幀。"""
        now = time.time()
        keys: list[StreamKey] = []
        for cam in cameras:
            keys.append((cam, "rgb"))
            seen = self._last_seen.get((cam, "thermal"))
            if seen is not None and now - seen <= _THERMAL_SEEN_WINDOW:
                keys.append((cam, "thermal"))
        return keys
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_manager.py::test_desired_recording_keys_rgb_always_thermal_when_seen tests/test_hls_manager.py::test_has_stream_reflects_streams -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): last_seen 追蹤 + has_stream/desired_recording_keys（監督者用）"
```

---

### Task 5: 錄影監督者 loop + lifespan 接線

**Files:**
- Modify: `main.py`（新增 `_recording_supervisor_loop` / `_run_recording_supervisor_once`、lifespan 起 task）
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `hls_manager.desired_recording_keys`、`hls_manager.has_stream`、`hls_manager.ensure_started`、`storage_monitor.get_target_mode`、`main._storage_alert`
- Produces: `main._run_recording_supervisor_once() -> None`（async）；module global `main._supervised_prev: set`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_main.py`：

```python
def test_supervisor_ensures_rgb_and_skips_on_drop(monkeypatch):
    import main
    ensured = []

    class FakeHls:
        def desired_recording_keys(self, cams):
            return [(c, "rgb") for c in cams]
        def has_stream(self, c, t):
            return False
        def ensure_started(self, c, t):
            ensured.append((c, t))

    monkeypatch.setattr(main, "hls_manager", FakeHls())
    monkeypatch.setattr(main.app_settings, "zmq_sources",
                        [type("S", (), {"label": "cam_01"})()])
    main._supervised_prev = set()

    # drop → 不 ensure
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "drop")
    asyncio.run(main._run_recording_supervisor_once())
    assert ensured == []

    # record → ensure rgb
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "record")
    asyncio.run(main._run_recording_supervisor_once())
    assert ("cam_01", "rgb") in ensured


def test_supervisor_fires_revive_alert_when_stream_went_missing(monkeypatch):
    import main
    alerts = []

    async def fake_alert(metric, cur, mean):
        alerts.append(metric)

    class FakeHls:
        def desired_recording_keys(self, cams):
            return [("cam_01", "rgb")]
        def has_stream(self, c, t):
            return False   # 一直不存在 → 需重建
        def ensure_started(self, c, t):
            pass

    monkeypatch.setattr(main, "hls_manager", FakeHls())
    monkeypatch.setattr(main, "_storage_alert", fake_alert)
    monkeypatch.setattr(main.storage_monitor, "get_target_mode", lambda: "record")
    monkeypatch.setattr(main.app_settings, "zmq_sources",
                        [type("S", (), {"label": "cam_01"})()])

    # 第一輪：首次建立（_supervised_prev 空）→ 不算 revive、不告警
    main._supervised_prev = set()
    asyncio.run(main._run_recording_supervisor_once())
    assert alerts == []
    # 第二輪：上一輪已列入 _supervised_prev、這輪仍 missing → revive 告警
    asyncio.run(main._run_recording_supervisor_once())
    assert "recording_supervisor_revive" in alerts
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_main.py::test_supervisor_ensures_rgb_and_skips_on_drop tests/test_main.py::test_supervisor_fires_revive_alert_when_stream_went_missing -v`
Expected: FAIL（`_run_recording_supervisor_once` 不存在）

- [ ] **Step 3: 實作**

在 `main.py` 的 `_RETENTION_INTERVAL_SECONDS` 常數附近新增：

```python
# 錄影監督者巡檢間隔：每 10s 確保每攝影機錄影串流存在（不依賴有人開直播頁）。
_SUPERVISOR_INTERVAL_SECONDS = 10

# 上一輪監督者確認過「應存在」的串流 keys；用來區分「首次建立」（不告警）與
# 「之前在、這輪不見了的重建」（告警 recording_supervisor_revive）。
_supervised_prev: set = set()
```

在 `_storage_monitor_loop` 之後新增：

```python
async def _run_recording_supervisor_once() -> None:
    """確保每個攝影機的錄影串流存在；被逐出/死掉的下一輪重建。drop（雙碟全死）
    時不重建（無處可寫）。某串流之前在、這輪卻不見 → 視為重建並告警。"""
    global _supervised_prev
    if storage_monitor.get_target_mode() == "drop":
        return
    cameras = [s.label for s in app_settings.zmq_sources]
    desired = hls_manager.desired_recording_keys(cameras)
    revived: list = []
    for cam, stype in desired:
        present_before = hls_manager.has_stream(cam, stype)
        try:
            hls_manager.ensure_started(cam, stype)
        except Exception as e:
            logger.warning(f"[{cam}/{stype}] 錄影監督者 ensure_started 失敗：{e}")
            continue
        if not present_before and (cam, stype) in _supervised_prev:
            revived.append((cam, stype))
    _supervised_prev = set(desired)
    for cam, stype in revived:
        logger.warning(f"[{cam}/{stype}] 錄影監督者重建已消失的串流")
        await _storage_alert("recording_supervisor_revive", 0.0, 0.0)


async def _recording_supervisor_loop() -> None:
    """週期性確保錄影串流存活。例外只 log，絕不拖垮服務。"""
    while True:
        await asyncio.sleep(_SUPERVISOR_INTERVAL_SECONDS)
        try:
            await _run_recording_supervisor_once()
        except Exception as e:
            logger.warning(f"錄影監督者巡檢失敗：{e}")
```

在 `lifespan` 的 `storage_task = asyncio.create_task(_storage_monitor_loop())`（行 114）之後新增：

```python
    supervisor_task = asyncio.create_task(_recording_supervisor_loop())
```

並在 `yield` 後的 cancel 區塊（行 116-117）加：

```python
    supervisor_task.cancel()
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_main.py::test_supervisor_ensures_rgb_and_skips_on_drop tests/test_main.py::test_supervisor_fires_revive_alert_when_stream_went_missing -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): 錄影監督者 loop（錄影獨立於觀看者、自我復活）"
```

---

### Task 6: ntfy_notifier 純傳輸模組

**Files:**
- Create: `ntfy_notifier.py`
- Test: `tests/test_ntfy_notifier.py`

**Interfaces:**
- Produces: `async ntfy_notifier.notify(url: str, title: str, message: str, *, priority: str = "default", tags: str = "") -> bool`（url 空 → return False no-op；POST 成功 → True；任何例外 → 吞掉回 False）

- [ ] **Step 1: 寫失敗測試**

新增 `tests/test_ntfy_notifier.py`：

```python
import asyncio

import ntfy_notifier


def _run(coro):
    return asyncio.run(coro)


def test_notify_noop_when_url_empty():
    assert _run(ntfy_notifier.notify("", "t", "m")) is False


def test_notify_swallows_network_error(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("net down")
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", BoomClient)
    # 不可拋例外
    assert _run(ntfy_notifier.notify("http://x/pig", "t", "m")) is False


def test_notify_posts_with_headers(monkeypatch):
    captured = {}

    class OkClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            class R: status_code = 200
            return R()
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", OkClient)
    ok = _run(ntfy_notifier.notify("http://x/pig", "標題", "訊息",
                                   priority="high", tags="warning"))
    assert ok is True
    assert captured["url"] == "http://x/pig"
    assert captured["content"] == "訊息".encode("utf-8")
    assert captured["headers"]["Title"] == "標題"
    assert captured["headers"]["Priority"] == "high"
    assert captured["headers"]["Tags"] == "warning"
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_ntfy_notifier.py -v`
Expected: FAIL（`ModuleNotFoundError: ntfy_notifier`）

- [ ] **Step 3: 實作**

新增 `ntfy_notifier.py`：

```python
"""ntfy 推播純傳輸模組。

不讀 config、不決定 policy（要推哪些事件由 main 決定）——只負責「把一則訊息
POST 到給定的 ntfy url」，並保證絕不拋例外、絕不阻塞事件迴圈（timeout）。
"""
import httpx
from loguru import logger


async def notify(url: str, title: str, message: str, *,
                 priority: str = "default", tags: str = "") -> bool:
    """POST 一則 ntfy 通知。url 空 → no-op 回 False。成功回 True，
    任何網路/逾時錯誤只 log warning 並回 False（呼叫端不需處理例外）。"""
    if not url:
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url, content=message.encode("utf-8"), headers=headers
            )
        if resp.status_code >= 400:
            logger.warning(f"ntfy notify 回 {resp.status_code}")
            return False
        return True
    except Exception as e:
        logger.warning(f"ntfy notify 失敗：{e}")
        return False
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_ntfy_notifier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add ntfy_notifier.py tests/test_ntfy_notifier.py
git commit -m "feat(ntfy): ntfy 推播純傳輸模組"
```

---

### Task 7: storage_monitor target_mode 轉換偵測（recording_paused / resumed）

**Files:**
- Modify: `storage_monitor.py:177-262`（`StorageMonitor.__init__` 加 `_prev_target_mode`、`run_once` 末尾加轉換偵測）
- Test: `tests/test_storage_monitor.py`

**Interfaces:**
- Consumes: `run_once(..., alert_cb)`（既有；`alert_cb(metric, cur, mean)`）
- Produces: target_mode 轉換時，經 `alert_cb` 多發 `recording_paused`（record→ephemeral 且 record 狀態 ok）或 `recording_resumed`（ephemeral→record）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_storage_monitor.py`：

```python
def test_recording_paused_alert_on_schedule_ephemeral(tmp_path):
    """碟健康但進入夜間 no-record 窗（record→ephemeral）→ recording_paused。"""
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1, min_free_bytes=0,
                           off_start_min=17 * 60, off_end_min=6 * 60 + 30)
    fired = []

    async def cb(metric, cur, mean):
        fired.append(metric)

    # 先在錄影時段（record）建立基準
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=datetime(2026, 6, 13, 12, 0), alert_cb=cb))
    fired.clear()
    # 進入 no-record 窗 → ephemeral
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=datetime(2026, 6, 13, 18, 0), alert_cb=cb))
    assert mon.get_target_mode() == "ephemeral"
    assert "recording_paused" in fired


def test_recording_resumed_alert_back_to_record(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1, min_free_bytes=0,
                           off_start_min=17 * 60, off_end_min=6 * 60 + 30)
    fired = []

    async def cb(metric, cur, mean):
        fired.append(metric)

    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=datetime(2026, 6, 13, 18, 0), alert_cb=cb))
    fired.clear()
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=datetime(2026, 6, 13, 12, 0), alert_cb=cb))
    assert mon.get_target_mode() == "record"
    assert "recording_resumed" in fired
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py::test_recording_paused_alert_on_schedule_ephemeral tests/test_storage_monitor.py::test_recording_resumed_alert_back_to_record -v`
Expected: FAIL（無 recording_paused/resumed）

- [ ] **Step 3: 實作**

`storage_monitor.py` 的 `StorageMonitor.__init__`（行 187 `self._target_mode = "record"` 之後）新增：

```python
        self._prev_target_mode = "record"
```

在 `run_once` 末尾（既有 `if transitioned and alert_cb is not None:` 告警區塊**之後**、方法結束前）新增 target_mode 轉換告警：

```python
        # target_mode 轉換告警（與 record 狀態告警獨立）：排程型暫停/恢復錄影。
        # 故障型 ephemeral（record 狀態 down）已由 storage_unwritable 涵蓋，
        # 故 recording_paused 僅在 record 狀態仍 ok 時發（＝純排程造成）。
        prev_mode = self._prev_target_mode
        self._prev_target_mode = mode
        if alert_cb is not None and mode != prev_mode:
            if mode == "ephemeral" and prev_mode == "record" and new_record == "ok":
                await alert_cb("recording_paused", rec_free / 1024**3, 0.0)
            elif mode == "record" and prev_mode == "ephemeral":
                await alert_cb("recording_resumed", rec_free / 1024**3, 0.0)
```

注意：`mode`、`new_record`、`rec_free` 都在 `run_once` 前段已定義（`mode` 於 `with self._lock` 區塊內賦值，出區塊仍可讀）。`_prev_target_mode` 的讀寫在鎖外無妨（單一 storage loop 串行呼叫 run_once）。

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add storage_monitor.py tests/test_storage_monitor.py
git commit -m "feat(storage): target_mode 轉換偵測（recording_paused/resumed）"
```

---

### Task 8: main 推播分派（_push_ntfy）+ 接入 _storage_alert

**Files:**
- Modify: `main.py`（`_storage_alert` 加 ntfy 推播、新增 `_push_ntfy` + metric 對照表、import）
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `ntfy_notifier.notify`、`get_all_settings`、`app_settings.ntfy_url/ntfy_enabled`
- Produces: `main._push_ntfy(metric: str, free_gb: float) -> None`（async）；`_storage_alert` 末尾呼叫之

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_main.py`：

```python
def test_push_ntfy_maps_metric_and_calls_notify(monkeypatch):
    import main
    sent = {}

    async def fake_notify(url, title, message, *, priority="default", tags=""):
        sent.update(url=url, title=title, priority=priority, tags=tags)
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)  # 用 app_settings
    monkeypatch.setattr(main.app_settings, "ntfy_url", "http://x/pig")
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", True)

    asyncio.run(main._push_ntfy("storage_unwritable", 3.0))
    assert sent["url"] == "http://x/pig"
    assert sent["priority"] == "urgent"


def test_push_ntfy_noop_when_disabled(monkeypatch):
    import main
    called = {"n": 0}

    async def fake_notify(*a, **k):
        called["n"] += 1
        return True

    monkeypatch.setattr(main.ntfy_notifier, "notify", fake_notify)
    monkeypatch.setattr(main.database, "get_pool", lambda: None)
    monkeypatch.setattr(main.app_settings, "ntfy_enabled", False)
    asyncio.run(main._push_ntfy("storage_unwritable", 3.0))
    assert called["n"] == 0
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_main.py::test_push_ntfy_maps_metric_and_calls_notify tests/test_main.py::test_push_ntfy_noop_when_disabled -v`
Expected: FAIL（`_push_ntfy` 不存在）

- [ ] **Step 3: 實作**

`main.py` 頂部 import 區（`import storage_monitor` 之後）加：

```python
import ntfy_notifier
```

在 `_storage_alert` **之前**新增對照表與分派：

```python
# metric → (ntfy 標題, priority, tags)。未列入的 metric 不推播。
_NTFY_MAP: dict[str, tuple[str, str, str]] = {
    "storage_unwritable":          ("🚨 錄影碟不可寫", "urgent", "rotating_light"),
    "storage_low_space":           ("⚠️ 儲存空間偏低", "high", "warning"),
    "storage_recovered":           ("✅ 儲存已恢復", "default", "white_check_mark"),
    "recording_paused":            ("🌙 夜間暫停錄影", "low", "moon"),
    "recording_resumed":           ("✅ 已恢復錄影", "default", "white_check_mark"),
    "recording_supervisor_revive": ("⚠️ 錄影串流已自動重建", "high", "warning"),
}


async def _push_ntfy(metric: str, free_gb: float) -> None:
    """依 metric 推播 ntfy。停用 / URL 空 / metric 未列入 → no-op。
    URL/開關優先讀 DB（即時生效），失敗回退 app_settings。"""
    spec = _NTFY_MAP.get(metric)
    if spec is None:
        return
    url = app_settings.ntfy_url
    enabled = app_settings.ntfy_enabled
    pool = database.get_pool()
    if pool is not None:
        try:
            db = await get_all_settings(pool)
            if db.get("ntfy_url") is not None:
                url = db["ntfy_url"]
            if db.get("ntfy_enabled") is not None:
                enabled = str(db["ntfy_enabled"]).strip().lower() == "true"
        except Exception:
            pass
    if not enabled:
        return
    title, priority, tags = spec
    msg = f"{metric} | 錄影碟可用 {free_gb:.1f} GB"
    await ntfy_notifier.notify(url, title, msg, priority=priority, tags=tags)
```

在 `_storage_alert` 末尾（既有 `write_health_alert` 之後、`except` 之外）追加推播。把現有 `_storage_alert` 主體改為：

```python
async def _storage_alert(metric: str, current_value: float, mean_value: float) -> None:
    pool = database.get_pool()
    if pool is None:
        logger.error(f"storage alert {metric} free={current_value:.1f}GB 但 DB 不可用")
    else:
        try:
            await write_health_alert(
                pool, camera_id="_system", object_id=0, metric=metric,
                current_value=float(current_value), mean_value=float(mean_value),
                std_value=0.0,
            )
        except Exception as e:
            logger.error(f"寫 storage alert 失敗：{e}")
    # 不論 DB 是否可用都嘗試推播（_push_ntfy 自行讀 DB/回退 app_settings）。
    try:
        await _push_ntfy(metric, current_value)
    except Exception as e:
        logger.warning(f"ntfy 推播失敗：{e}")
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_main.py -v`
Expected: 全 PASS（含既有 `test_storage_alert_*`）

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): _storage_alert 接 ntfy 推播（依 metric 對照優先級）"
```

---

### Task 9: storage_monitor is_inference_active + resolve_gpu_active

**Files:**
- Modify: `storage_monitor.py`（新增兩個純函式）
- Test: `tests/test_storage_monitor.py`

**Interfaces:**
- Produces:
  - `is_inference_active(now, off_start_min, off_end_min, enabled) -> bool`（語意：是否在 GPU 開啟時段；停用/無效/空窗 → True）
  - `resolve_gpu_active(db: dict | None, app_settings, now: datetime) -> bool`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_storage_monitor.py`：

```python
def test_is_inference_active_window():
    # gpu_off 22:00–06:00：窗內 inactive、窗外 active
    assert sm.is_inference_active(datetime(2026, 6, 13, 23, 0),
                                  22 * 60, 6 * 60, True) is False
    assert sm.is_inference_active(datetime(2026, 6, 13, 12, 0),
                                  22 * 60, 6 * 60, True) is True
    # 停用 → 永遠 active
    assert sm.is_inference_active(datetime(2026, 6, 13, 23, 0),
                                  22 * 60, 6 * 60, False) is True


def test_resolve_gpu_active_uses_db_then_fallback():
    class App:
        gpu_off_schedule_enabled = False
        gpu_off_start = "22:00"
        gpu_off_end = "06:00"
    now = datetime(2026, 6, 13, 23, 0)
    # DB 啟用排程 + 窗內 → inactive
    db = {"gpu_off_schedule_enabled": "true",
          "gpu_off_start": "22:00", "gpu_off_end": "06:00"}
    assert sm.resolve_gpu_active(db, App(), now) is False
    # DB 缺鍵 → 回退 app_settings（停用）→ active
    assert sm.resolve_gpu_active(None, App(), now) is True
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_storage_monitor.py::test_is_inference_active_window tests/test_storage_monitor.py::test_resolve_gpu_active_uses_db_then_fallback -v`
Expected: FAIL（函式不存在）

- [ ] **Step 3: 實作**

在 `storage_monitor.py` 的 `is_recording_time` 之後新增：

```python
def is_inference_active(now: datetime, off_start_min: int, off_end_min: int,
                        enabled: bool) -> bool:
    """now 是否在「GPU 推論開啟時段」（gpu_off 窗之外）。停用/無效/空窗 →
    永遠 active。語意與 is_recording_time 相同（皆判斷『是否在 off 窗外』）。"""
    return is_recording_time(now, off_start_min, off_end_min, enabled)
```

在 `resolve_settings` 之後新增：

```python
def resolve_gpu_active(db: "dict | None", app_settings, now: datetime) -> bool:
    """合併 DB（前端可調）與 app_settings → 算當下 GPU 推論是否該開啟。"""
    def g(key, default):
        if db and key in db and db[key] is not None:
            return db[key]
        return default

    enabled = _coerce_bool(
        g("gpu_off_schedule_enabled", app_settings.gpu_off_schedule_enabled),
        app_settings.gpu_off_schedule_enabled)
    start = parse_hhmm(str(g("gpu_off_start", app_settings.gpu_off_start)))
    end = parse_hhmm(str(g("gpu_off_end", app_settings.gpu_off_end)))
    return is_inference_active(now, start, end, enabled)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_storage_monitor.py::test_is_inference_active_window tests/test_storage_monitor.py::test_resolve_gpu_active_uses_db_then_fallback -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add storage_monitor.py tests/test_storage_monitor.py
git commit -m "feat(storage): is_inference_active + resolve_gpu_active 純函式"
```

---

### Task 10: InferencePipeline set_active + _process_batch 閘門

**Files:**
- Modify: `inference/pipeline.py:46-56`（`__init__`）、`95-118`（新增 `set_active`、`_process_batch` 閘門）
- Test: `tests/test_inference_pipeline.py`

**Interfaces:**
- Produces: `InferencePipeline.set_active(active: bool) -> None`；`_process_batch` 在 `not self._active` 時直接 return（不呼叫 detector）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_inference_pipeline.py`：

```python
def test_set_active_false_skips_detector():
    from inference.pipeline import FrameData, InferencePipeline
    import numpy as np
    p = InferencePipeline()
    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [np.ones((1, 7), dtype=np.float32)]
    p._detector = mock_detector
    p._reid = MagicMock()
    p._tracker_pool = MagicMock()

    p.set_active(False)
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    mock_detector.infer.assert_not_called()

    # 恢復 active 後會呼叫 detector（驗證 gate 不是永久關）
    p.set_active(True)
    # detector 真的被叫到即可（後續 reid/tracker 為 MagicMock，不深究結果）
    try:
        p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                              ts=1.0, frame_id=1)})
    except Exception:
        pass
    mock_detector.infer.assert_called()
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_inference_pipeline.py::test_set_active_false_skips_detector -v`
Expected: FAIL（`set_active` 不存在 / detector 仍被呼叫）

- [ ] **Step 3: 實作**

`inference/pipeline.py` 的 `__init__`（行 54 `self._running = False` 之後）新增：

```python
        self._active = True
```

在 `update_frame` 之後（`_loop` 之前）新增方法：

```python
    def set_active(self, active: bool) -> None:
        """夜間省電閘門：False → _process_batch 跳過 GPU 計算（detector/ReID/
        tracker 皆不呼叫，GPU 閒置）。執行緒間僅單一 bool 寫入，無需鎖。"""
        self._active = active
```

在 `_process_batch`（行 119）的 `try:` **之後第一行**插入閘門：

```python
        if not self._active:
            return
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_inference_pipeline.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add inference/pipeline.py tests/test_inference_pipeline.py
git commit -m "feat(inference): set_active 閘門（夜間停 GPU 省電）"
```

---

### Task 11: storage loop 每輪設定推論 active 狀態

**Files:**
- Modify: `main.py:76-101`（`_storage_monitor_loop`）
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `storage_monitor.resolve_gpu_active`、`inference_pipeline.set_active`

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_main.py`：

```python
def test_storage_loop_helper_sets_inference_active(monkeypatch):
    """抽出的 _apply_gpu_schedule 應依 resolve_gpu_active 設 inference 旗標。"""
    import main
    states = []
    monkeypatch.setattr(main.inference_pipeline, "set_active",
                        lambda v: states.append(v))
    monkeypatch.setattr(main.storage_monitor, "resolve_gpu_active",
                        lambda db, app, now: False)
    main._apply_gpu_schedule(db_settings=None)
    assert states == [False]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_main.py::test_storage_loop_helper_sets_inference_active -v`
Expected: FAIL（`_apply_gpu_schedule` 不存在）

- [ ] **Step 3: 實作**

`main.py` 頂部 import 區確認已有 `from inference.pipeline import inference_pipeline`（既有，行 19）。

在 `_storage_monitor_loop` **之前**新增 helper：

```python
def _apply_gpu_schedule(db_settings: "dict | None") -> None:
    """依 DB/app_settings 的 gpu_off 排程算當下推論是否該開，設 inference 旗標。"""
    active = storage_monitor.resolve_gpu_active(
        db_settings, app_settings, datetime.now())
    inference_pipeline.set_active(active)
```

在 `_storage_monitor_loop` 內，`await storage_monitor.monitor.run_once(...)`（行 92-98）之後、`except` 之前插入：

```python
            _apply_gpu_schedule(db_settings)
```

（`db_settings` 在同一 try 區塊上方已取得：`pool` 有值時為 `await get_all_settings(pool)`，否則 `None`。）

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_main.py::test_storage_loop_helper_sets_inference_active -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat(main): storage loop 每輪套用夜間 GPU 排程"
```

---

### Task 12: settings router + sql seed 接線

**Files:**
- Modify: `routers/settings.py:9-25`（`ALLOWED_KEYS`）、`42-57`（GET 回退 dict）
- Modify: `sql/init.sql:53-57`（seed）
- Test: `tests/test_settings_router.py`

**Interfaces:**
- Consumes: 新 config 欄位（Task 1）
- Produces: 5 個新 key 可經 `/settings` 讀寫

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_settings_router.py`（沿用既有測試風格，斷言新 key 在 `ALLOWED_KEYS`）：

```python
def test_new_ops_keys_allowed():
    from routers.settings import ALLOWED_KEYS
    for k in ("ntfy_url", "ntfy_enabled", "gpu_off_schedule_enabled",
              "gpu_off_start", "gpu_off_end"):
        assert k in ALLOWED_KEYS
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings_router.py::test_new_ops_keys_allowed -v`
Expected: FAIL（key 不在 ALLOWED_KEYS）

- [ ] **Step 3: 實作**

`routers/settings.py` 的 `ALLOWED_KEYS`（行 24 `"recording_off_end",` 之後、`})` 之前）加：

```python
    # ntfy 推播
    "ntfy_url",
    "ntfy_enabled",
    # 夜間停 GPU 排程
    "gpu_off_schedule_enabled",
    "gpu_off_start",
    "gpu_off_end",
```

GET 回退 dict（行 56 `"recording_off_end": app_settings.recording_off_end,` 之後、`}` 之前）加：

```python
            "ntfy_url":                       app_settings.ntfy_url,
            "ntfy_enabled":                   str(app_settings.ntfy_enabled).lower(),
            "gpu_off_schedule_enabled":       str(app_settings.gpu_off_schedule_enabled).lower(),
            "gpu_off_start":                  app_settings.gpu_off_start,
            "gpu_off_end":                    app_settings.gpu_off_end,
```

`sql/init.sql` 的 seed（行 53-57）擴充為：

```sql
INSERT INTO user_settings (key, value, updated_at) VALUES
    ('analysis_interval_minutes', '30', NOW()),
    ('anomaly_std_threshold', '3.0', NOW()),
    ('hls_retention_days', '90', NOW()),
    ('ntfy_url', 'https://ntfy.ed716.duckdns.org/pig', NOW()),
    ('ntfy_enabled', 'true', NOW()),
    ('gpu_off_schedule_enabled', 'false', NOW()),
    ('gpu_off_start', '22:00', NOW()),
    ('gpu_off_end', '06:00', NOW())
ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_settings_router.py -v`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add routers/settings.py sql/init.sql tests/test_settings_router.py
git commit -m "feat(settings): 開放 ntfy_* / gpu_off_* 鍵 + sql seed"
```

---

### Task 13: 前端設定面板欄位

**Files:**
- Modify: `static/index.html:957-961`（設定欄位 HTML）、`1856-1865`（loadSettings 對照）、`1869-1881`（saveSettings body）
- Test: `node --check`（HTML 內 JS 語法檢查）

**Interfaces:**
- Consumes: `/settings` GET/PUT 的新 key（Task 12）

- [ ] **Step 1: 加 HTML 欄位**

在 `static/index.html` 的「監控間隔 (秒)」欄位（行 962-965）**之後**、`settings-save-row`（行 966）**之前**插入：

```html
        <div class="settings-field">
          <label for="set-ntfy_enabled">ntfy 推播</label>
          <input type="checkbox" id="set-ntfy_enabled">
        </div>
        <div class="settings-field">
          <label for="set-ntfy_url">ntfy URL</label>
          <input type="text" id="set-ntfy_url" placeholder="https://ntfy.../pig">
        </div>
        <div class="settings-field">
          <label for="set-gpu_off_schedule_enabled">夜間停 GPU</label>
          <input type="checkbox" id="set-gpu_off_schedule_enabled">
        </div>
        <div class="settings-field">
          <label for="set-gpu_off_start">停 GPU 起</label>
          <input type="time" id="set-gpu_off_start" value="22:00">
        </div>
        <div class="settings-field">
          <label for="set-gpu_off_end">停 GPU 迄</label>
          <input type="time" id="set-gpu_off_end" value="06:00">
        </div>
```

- [ ] **Step 2: loadSettings 套用**

在 `loadSettings` 的 `_smap` 物件（行 1856-1861）內，`'set-storage_check_interval_seconds': 'storage_check_interval_seconds',` 之後加：

```javascript
          'set-ntfy_url': 'ntfy_url',
          'set-gpu_off_start': 'gpu_off_start',
          'set-gpu_off_end': 'gpu_off_end',
```

並在 `_smap` 迴圈（行 1862-1865）**之後**，加上兩個 checkbox 的套用（沿用既有 `recording_schedule_enabled` 寫法）：

```javascript
        const _ne = document.getElementById('set-ntfy_enabled');
        if (_ne) _ne.checked = String(data.ntfy_enabled) === 'true';
        const _ge = document.getElementById('set-gpu_off_schedule_enabled');
        if (_ge) _ge.checked = String(data.gpu_off_schedule_enabled) === 'true';
```

- [ ] **Step 3: saveSettings 送出**

在 `saveSettings` 的 `body` 物件（行 1870-1881）內，`storage_check_interval_seconds:` 那行之後加：

```javascript
        ntfy_enabled:               String(document.getElementById('set-ntfy_enabled').checked),
        ntfy_url:                   document.getElementById('set-ntfy_url').value,
        gpu_off_schedule_enabled:   String(document.getElementById('set-gpu_off_schedule_enabled').checked),
        gpu_off_start:              document.getElementById('set-gpu_off_start').value,
        gpu_off_end:                document.getElementById('set-gpu_off_end').value,
```

- [ ] **Step 4: JS 語法檢查**

抽出 inline script 檢查（行號會位移，用 grep 找起始）：

```bash
START=$(grep -n "^    <script>" static/index.html | head -1 | cut -d: -f1)
echo "script starts at $START"
sed -n "$((START+1)),\$p" static/index.html | sed '/^  <\/script>/,$d' > "$CLAUDE_JOB_DIR/tmp/index_check.js"
node --check "$CLAUDE_JOB_DIR/tmp/index_check.js" && echo "JS OK"
```

Expected: `JS OK`

- [ ] **Step 5: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 設定面板加 ntfy 推播與夜間停 GPU 欄位"
```

---

## Self-Review

**Spec coverage：**
- §3 #1 監督者：Task 4（HLSManager 支援）+ Task 5（supervisor loop）；硬化：Task 2（zmq）+ Task 3（_restart）；可觀測性：Task 2/3/5 的 WARNING log。✓
- §4 #2 確認：spec 已記錄，無 code 改動。✓（驗收清單 §10.2）
- §5 #3 ntfy：Task 6（傳輸）+ Task 7（paused/resumed 事件源）+ Task 8（分派/對照/接 _storage_alert）。事件表全覆蓋：unwritable/low_space/recovered（既有 _storage_alert 來源）+ paused/resumed（Task 7）+ supervisor_revive（Task 5）。✓
- §6 #4 夜間停 GPU：Task 9（純函式）+ Task 10（閘門）+ Task 11（每輪套用）。✓
- §7 設定接線：Task 1（config）+ Task 12（router/sql）+ Task 13（前端）。✓
- §8 測試策略：各 task 皆 TDD。✓

**Placeholder scan：** 無 TBD/TODO；所有 step 含實際程式碼與精確路徑/行號。✓

**Type consistency：**
- `_storage_alert(metric, current_value, mean_value)` 簽名 Task 5/8 一致（Task 5 呼叫 `_storage_alert("recording_supervisor_revive", 0.0, 0.0)`）。✓
- `ntfy_notifier.notify(url, title, message, *, priority, tags)` Task 6 定義、Task 8 呼叫一致。✓
- `desired_recording_keys` / `has_stream` Task 4 定義、Task 5 消費一致。✓
- `resolve_gpu_active(db, app_settings, now)` Task 9 定義、Task 11 呼叫一致。✓
- `set_active(active)` Task 10 定義、Task 11 呼叫一致。✓
- `is_inference_active(now, off_start_min, off_end_min, enabled)` Task 9 定義、內部一致。✓

無未定義引用。計畫完成。
