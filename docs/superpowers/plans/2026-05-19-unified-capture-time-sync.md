# 統一 live + VOD 擷取時間同步 實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 live 與 VOD 的 bbox 對齊由「相機真實 capture_ts 構成、每段重錨的時間軸」驅動，根除 ffmpeg `-vf fps` 媒體時鐘脫鉤造成的 FPS 依賴漂移，並刪除 FID/手動 offset/PDT-EMA 整套修正債。

**Architecture:** Writer 改 `time.monotonic()` 截止排程節拍器（消除餵入速率偏差）；`_emit_frame` 記 `(emit_idx, capture_ts)`；`_scan_new_segments` 由此推每段首幀真實 `capture_ts`，寫入記憶體 `_seg_pdt` 與磁碟 append-only sidecar `pdt.jsonl`；`corrected_m3u8`（live）與 `vod_generator`（VOD）皆以真實 capture_ts 重寫每段 PDT/EXTINF、大 gap 補 `#EXT-X-DISCONTINUITY`；前端 live 用 `hls.playingDate`、VOD 用播放器 PDT，移除 FID 配對與 `livePdtOffset`。

**Tech Stack:** Python 3 / ffmpeg(libx264, HLS) / FastAPI / pytest / hls.js / 原生 JS。

**規格來源:** `docs/superpowers/specs/2026-05-19-unified-capture-time-sync-design.md`

**測試基線（每次跑全套件用）:** `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`。預存在失敗固定為 5：`tests/test_config.py::test_default_mot_worker_threads` + 4 個 `tests/test_stream_router.py` 404（待辦 #12 ZMQ_SOURCES OS-env gap），**非本計畫回歸**。每個 Task commit 後須維持「僅這 5 個既有失敗」。

**標準限制（全程適用）:** 可 commit、**不可 push**（停在本機 master，使用者自行 push）；`CLAUDE.md`、`ref/HybridSORT/` 為 gitignore，**不得 commit/force-add**；sandbox 不可讀 `data/pig_monitoring/hls`、`.env`（測試一律用 `tmp_path`，不碰真實目錄）；以繁體中文回應。

---

## File Structure

| 檔案 | 責任 | 本計畫變更 |
|---|---|---|
| `config.py` | 設定 | +`hls_slip_resync_seconds`、`hls_discontinuity_seconds`；−`live_pdt_offset_seconds` |
| `hls_manager.py` | HLS 編碼/切片 + 真實時間軸授權 | writer 節拍器、`_emit_log`、`_scan_new_segments` 真實錨點、sidecar、`corrected_m3u8` 真實 EXTINF/DISCONTINUITY；刪 FID/offset/EMA |
| `vod_generator.py` | VOD m3u8 組裝 | 讀 sidecar→真實 seg_start/EXTINF、逐段 PDT、缺則回退舊邏輯 |
| `zmq_receiver.py` | 來源接收 | rgb feed 不再傳 `frame_id`（仍傳 `capture_ts`） |
| `routers/stream.py` | 串流端點 | `/live` 移除 `pdt_offset` |
| `routers/settings.py` | 設定 API | `ALLOWED_KEYS` / 預設 dict 移除 `live_pdt_offset_seconds` |
| `static/index.html` | 前端 | 刪 FID 配對 / `livePdtOffset` / offset 設定欄；live=`playingDate`、VOD=播放器 PDT；HUD 簡化 |
| `tests/test_hls_manager.py` | 後端測試 | 新增 writer/emit_log/scan/sidecar/corrected_m3u8 測試 |
| `tests/test_vod_generator.py` | VOD 測試 | 新增 sidecar 讀取 / fallback 測試（檔案可能不存在則建立） |
| `tests/test_stream_router.py` | 路由測試 | `/live` 無 `pdt_offset` 斷言 |
| `tests/test_settings_router.py` | 設定測試 | 移除 `live_pdt_offset_seconds` 相關斷言 |

執行順序刻意讓每個 commit 都能通過測試：先做「加法」(T1–T4 新機制與 sidecar，舊 FID/offset 暫留且不被新路徑使用)，再做「減法」(T5 zmq 停傳 frame_id → T6 hls_manager 刪 FID/offset 內部 + 端點/設定 + 前端)。

---

## Task 1: config 同步參數 + hls_manager writer 真實牆鐘節拍器

新增兩個設定參數；`_frame_buffer` 改存 `(jpeg, capture_ts)`；`_writer_loop` 改 `monotonic` 截止排程（落後重同步、空則複製帶上一幀 capture_ts）；`_emit_frame` 新增 `_emit_idx`/`_emit_log` 記 `(emit_idx, capture_ts)`；`_make_ffmpeg_cmd` 移除 `-vf fps` 與輸入 `-framerate`。舊 `_fed_log`/`_fed_count`/`frame_id` 參數此 Task **暫時保留**（不被新邏輯使用），由 Task 6 統一刪除，確保各 commit 綠燈。

**Files:**
- Modify: `config.py:55-60`、`config.py:107-109`
- Modify: `hls_manager.py`（`_make_ffmpeg_cmd`、`HLSStream.__init__`、`feed`、`_emit_frame`、`_writer_loop`）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 加設定參數（config.py）**

`config.py` 於 `hls_base_dir`（第 60 行）之後新增兩行（找到 `hls_base_dir: str = "data/pig_monitoring/hls"` 該行，於其下加）：

```python
    hls_slip_resync_seconds: float = 0.5   # writer 落後超過此值即重置截止時間（不爆衝補償）
    hls_discontinuity_seconds: float = 8.0  # 相鄰段 capture_ts 差超過此值 → #EXT-X-DISCONTINUITY
```

（暫不刪 `live_pdt_offset_seconds`，Task 6 處理。）

- [ ] **Step 2: 寫失敗測試（writer 節拍器 + emit_log + ffmpeg cmd）**

於 `tests/test_hls_manager.py` 末尾新增：

```python
def test_ffmpeg_cmd_drops_fps_filter_and_input_framerate(tmp_path):
    from hls_manager import _make_ffmpeg_cmd
    joined = " ".join(_make_ffmpeg_cmd(tmp_path))
    assert "fps=" not in joined          # 移除 -vf fps（resample 是脫鉤源）
    assert "-framerate" not in joined    # 移除輸入 framerate 提示
    assert "-hls_time" in joined         # 其餘 HLS 設定保留


def test_feed_buffers_jpeg_with_capture_ts(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True  # 停掉 writer thread，純測 buffer
    stream.feed(b"J1", capture_ts=1000.0)
    assert stream._frame_buffer[-1] == (b"J1", 1000.0)


def test_emit_frame_records_emit_log_with_capture_ts(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    assert stream._emit_frame(b"A", 1000.0) is True
    assert stream._emit_frame(b"B", 1000.5) is True
    assert stream._emit_idx == 2
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1000.5)]
    # capture_ts 為 None（thermal/缺）→ 仍計數但不記 log
    assert stream._emit_frame(b"C", None) is True
    assert stream._emit_idx == 3
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1000.5)]


def test_writer_loop_duplicates_with_last_capture_ts(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    written = []
    monkeypatch.setattr(stream, "_emit_frame",
                         lambda f, ts: (written.append((f, ts)) or True))
    stream._frame_buffer.append((b"X", 2000.0))
    # 模擬 writer 單次 tick 的取幀邏輯：先有幀、buffer 空後複製
    stream._writer_tick()   # 取出 (b"X",2000.0)
    stream._writer_tick()   # buffer 空 → 複製上一幀，沿用 2000.0
    assert written == [(b"X", 2000.0), (b"X", 2000.0)]
```

- [ ] **Step 3: 跑測試確認失敗**

Run: `uv run pytest -q tests/test_hls_manager.py -k "drops_fps_filter or buffers_jpeg_with_capture_ts or emit_log_with_capture_ts or duplicates_with_last_capture_ts"`
Expected: FAIL（`fps=` 仍在 / `_emit_log` 不存在 / `_writer_tick` 不存在）。

- [ ] **Step 4: 改 `_make_ffmpeg_cmd`（hls_manager.py:59-87）**

把 `-framerate` 與 `-vf fps` 兩段移除。將該函式 `return [...]` 內容改為（刪掉 `"-framerate", str(TARGET_FPS),` 與 `"-vf", f"fps={TARGET_FPS}",` 兩行；其餘不變）：

```python
def _make_ffmpeg_cmd(out_dir: Path) -> list[str]:
    gop = TARGET_FPS * 2
    return [
        "ffmpeg", "-y",
        "-f", "mjpeg",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-g", str(gop),
        "-hls_time", "4",
        "-hls_list_size", "0",
        "-hls_flags", "append_list+program_date_time",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        "-loglevel", FFMPEG_LOG_LEVEL,
        str(out_dir / "index.m3u8"),
    ]
```

- [ ] **Step 5: 改 `HLSStream.__init__` buffer 型別 + 新增 emit 狀態（hls_manager.py:147-149, 163-165）**

把 `self._frame_buffer` 宣告改型別、`_fed_*` 區塊新增 `_emit_idx`/`_emit_log`（**保留** `_fed_count`/`_fed_log`/`_seg_first_fid`，Task 6 才刪）：

```python
        self._frame_buffer: deque[tuple[bytes, Optional[float]]] = deque(
            maxlen=FRAME_BUFFER_SIZE
        )
```

並在 `self._seg_first_fid: dict[str, int] = {}`（第 165 行）之後新增：

```python
        # 真實時間軸授權：每寫入 ffmpeg 一幀記 (emit_idx, capture_ts)，
        # 與 ffmpeg 輸出幀 1:1（writer 等速 tick）。_scan_new_segments
        # 據此推每段首幀真實擷取牆鐘。
        self._emit_idx: int = 0
        self._emit_log: deque[tuple[int, float]] = deque(maxlen=_FED_LOG_MAX)
        self._writer_last_frame: Optional[tuple[bytes, Optional[float]]] = None
```

- [ ] **Step 6: 改 `feed` 存 capture_ts（hls_manager.py:177-198）**

把 `feed` body 末段（`fid = ...` 與 append）改為（仍接受 `frame_id` 參數但不再用，Task 6 移除參數）：

```python
    def feed(
        self,
        jpeg_bytes: bytes,
        capture_ts: Optional[float] = None,
        frame_id: Optional[int] = None,
    ) -> None:
        with self._lock:
            new_dir = self._hour_dir()
            if new_dir != self.out_dir:
                self._restart(new_dir)
        if capture_ts is not None:
            self._last_capture_ts = capture_ts
        self.last_feed_time = time.time()
        self._frame_buffer.append((jpeg_bytes, capture_ts))
        self._buffer_event.set()
```

- [ ] **Step 7: 改 `_emit_frame` 記 emit_log（hls_manager.py:288-309）**

簽名與內部記錄改為（仍 `return False` on BrokenPipe）：

```python
    def _emit_frame(self, frame: bytes, capture_ts: Optional[float]) -> bool:
        """寫一幀進 ffmpeg stdin，並在寫入那刻記 (emit_idx, capture_ts)。
        writer 等速每 tick 寫一幀（含補幀）→ emit_idx 與 ffmpeg 輸出幀
        1:1；segment NNN 首幀 == emit_idx round(NNN*TARGET_FPS*_HLS_TIME)。
        回傳 False 表示 stdin pipe 已斷（writer 應結束）。"""
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except BrokenPipeError:
            logger.warning(
                f"[{self.camera_id}/{self.stream_type}] ffmpeg stdin pipe broken, "
                "stream may have crashed"
            )
            return False
        except Exception as e:
            logger.warning(f"[{self.camera_id}/{self.stream_type}] stdin write error: {e}")
            return True
        if capture_ts is not None:
            self._emit_log.append((self._emit_idx, capture_ts))
        self._emit_idx += 1
        return True
```

- [ ] **Step 8: 改 `_writer_loop` 為 monotonic 截止排程 + 抽出 `_writer_tick`（hls_manager.py:311-341）**

```python
    def _writer_tick(self) -> None:
        """單次：取一幀（空則複製上一幀沿用其 capture_ts）寫入 ffmpeg。
        回傳由 _emit_frame 決定是否續跑（False→pipe 斷）。"""
        try:
            frame = self._frame_buffer.popleft()
            self._writer_last_frame = frame
        except IndexError:
            frame = self._writer_last_frame
        if frame is not None:
            jpeg_bytes, cap = frame
            if not self._emit_frame(jpeg_bytes, cap):
                self._stopped = True

    def _writer_loop(self) -> None:
        """真實牆鐘節拍器：每 1/TARGET_FPS 真實秒寫一幀，落後過多即重置
        截止時間（不爆衝補償，避免時間軸扭曲）。長期餵入速率因此嚴格
        鎖在 TARGET_FPS×真實秒，消除造成漂移斜線的持續性速率偏差。"""
        interval = 1.0 / TARGET_FPS
        slip = getattr(settings, "hls_slip_resync_seconds", 0.5)
        deadline = time.monotonic()
        while not self._stopped:
            self._writer_tick()
            now_m = time.monotonic()
            if now_m - self._last_scan >= 0.5:
                self._last_scan = now_m
                self._scan_new_segments()
            deadline += interval
            now_m = time.monotonic()
            if now_m - deadline > slip:
                deadline = now_m       # 落後過多 → 重同步，不爆衝
            sleep_time = deadline - now_m
            if sleep_time > 0:
                time.sleep(sleep_time)
```

- [ ] **Step 9: 跑測試確認通過**

Run: `uv run pytest -q tests/test_hls_manager.py`
Expected: 全綠（含新 4 測試 + 既有；既有 `test_ffmpeg_cmd_has_correct_hls_settings` 仍過，因只斷言 `-hls_time`/`append_list`/`seg_%03d`，不涉 `-vf`）。

- [ ] **Step 10: 跑全套件確認零回歸**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗，其餘全綠。

- [ ] **Step 11: Commit**

```bash
git add config.py hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): writer 真實牆鐘節拍器 + emit_log(capture_ts) + 移除 -vf fps"
```

---

## Task 2: `_scan_new_segments` 真實錨點 + sidecar 落磁碟

`_scan_new_segments` 改用 `_emit_log` 推每段首幀真實 `capture_ts`（取代「掃描當下最新 `_last_capture_ts`」），含非單調 clamp；同時 append 到每小時目錄 sidecar `pdt.jsonl`。`_restart` 清記憶體但**不刪 sidecar 檔**。舊 `_seg_first_fid`/`_fed_log` 記錄此 Task 保留不動（Task 6 刪）。

**Files:**
- Modify: `hls_manager.py`（`HLSStream.__init__` 加 `_PDT_EPS`、`_scan_new_segments`、`_restart`）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

於 `tests/test_hls_manager.py` 末尾新增：

```python
import json


def test_scan_records_seg_pdt_from_emit_log(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    from hls_manager import TARGET_FPS, _HLS_TIME
    # seg_001 首幀 emit_idx = 1*TARGET_FPS*_HLS_TIME
    idx1 = round(1 * TARGET_FPS * _HLS_TIME)
    stream._emit_log.append((0, 5000.0))
    stream._emit_log.append((idx1, 5004.0))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 5000.0
    assert stream._seg_pdt["seg_001.ts"] == 5004.0
    # sidecar 落磁碟、內容正確
    lines = (stream.out_dir / "pdt.jsonl").read_text().splitlines()
    rows = {json.loads(x)["seg"]: json.loads(x)["pdt"] for x in lines}
    assert rows == {"seg_000.ts": 5000.0, "seg_001.ts": 5004.0}


def test_scan_clamps_non_monotonic_pdt(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    from hls_manager import TARGET_FPS, _HLS_TIME
    idx1 = round(1 * TARGET_FPS * _HLS_TIME)
    stream._emit_log.append((0, 5000.0))
    stream._emit_log.append((idx1, 4990.0))   # 倒退
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 5000.0
    assert stream._seg_pdt["seg_001.ts"] == pytest.approx(5000.0 + 1e-3)


def test_restart_clears_memory_keeps_sidecar(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._emit_log.append((0, 7000.0))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    stream._scan_new_segments()
    sidecar = stream.out_dir / "pdt.jsonl"
    assert sidecar.exists()
    new_dir = tmp_path / "cam_01" / "rgb" / "2099-01-01-05"
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        stream._restart(new_dir)
    assert stream._seg_pdt == {}
    assert list(stream._emit_log) == []
    assert stream._emit_idx == 0
    assert sidecar.exists()   # 舊小時 sidecar 不刪
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest -q tests/test_hls_manager.py -k "seg_pdt_from_emit_log or clamps_non_monotonic or clears_memory_keeps_sidecar"`
Expected: FAIL。

- [ ] **Step 3: `__init__` 加常數**

於 Task 1 新增的 `self._writer_last_frame = None` 之後加：

```python
        self._PDT_EPS: float = 1e-3   # 非單調 capture_ts clamp 用
```

- [ ] **Step 4: 改 `_scan_new_segments`（hls_manager.py:200-231）**

替換整個方法為：

```python
    def _scan_new_segments(self) -> None:
        """偵測 out_dir 新出現的 seg_*.ts，用 _emit_log 推該段首幀真實
        擷取牆鐘（emit_idx ≈ NNN*TARGET_FPS*_HLS_TIME），存 _seg_pdt 並
        append 到 sidecar pdt.jsonl（VOD 跨小時讀得到）。非單調則 clamp。"""
        try:
            names = sorted(p.name for p in self.out_dir.glob("seg_*.ts"))
        except OSError:
            return
        with self._seg_lock:
            emit_log = None
            new_rows: list[tuple[str, float]] = []
            for name in names:
                if name in self._seen_segs:
                    continue
                self._seen_segs.add(name)
                m = re.match(r"seg_(\d+)\.ts$", name)
                if not m:
                    continue
                if emit_log is None:
                    emit_log = list(self._emit_log)
                if not emit_log:
                    continue
                expected = round(int(m.group(1)) * TARGET_FPS * _HLS_TIME)
                cap = min(emit_log, key=lambda p: abs(p[0] - expected))[1]
                if self._seg_pdt:
                    prev = max(self._seg_pdt.values())
                    if cap <= prev:
                        cap = prev + self._PDT_EPS
                self._seg_pdt[name] = cap
                new_rows.append((name, cap))
            if len(self._seg_pdt) > 2000:
                for k in sorted(self._seg_pdt)[:-2000]:
                    self._seg_pdt.pop(k, None)
        for seg_name, cap in new_rows:
            try:
                with (self.out_dir / "pdt.jsonl").open("a") as fh:
                    fh.write(json.dumps({"seg": seg_name, "pdt": cap}) + "\n")
            except OSError as e:
                logger.warning(f"[{self.camera_id}/{self.stream_type}] sidecar write failed: {e}")
```

在檔案頂部 import 區（`import re` 附近）新增 `import json`（若尚無）。

- [ ] **Step 5: 改 `_restart` 清 emit 狀態（hls_manager.py:343-360）**

在 `_restart` 的 `with self._seg_lock:` 區塊後、`self._fed_log.clear()` 那段，新增清 `_emit_log`/`_emit_idx`（保留既有 `_fed_log`/`_fed_count` 行不動）：

```python
        with self._seg_lock:
            self._seg_pdt.clear()
            self._seen_segs.clear()
            self._seg_first_fid.clear()
        self._fed_log.clear()
        self._fed_count = 0
        self._emit_log.clear()
        self._emit_idx = 0
        self._writer_last_frame = None
        self._last_scan = 0.0
```

（sidecar 為 `out_dir` 內檔案，`_restart` 切到 new_dir，舊檔自然保留、不主動刪。）

- [ ] **Step 6: 跑測試確認通過**

Run: `uv run pytest -q tests/test_hls_manager.py`
Expected: 全綠。

- [ ] **Step 7: 全套件零回歸**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗。

- [ ] **Step 8: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): _scan 用 emit_log 推每段真實 capture_ts + sidecar pdt.jsonl"
```

---

## Task 3: `corrected_m3u8` 真實 EXTINF + DISCONTINUITY

`corrected_m3u8`（live current-hour playlist）每段 PDT 改用新 `_seg_pdt`（已是真實 capture_ts），`#EXTINF` 改為相鄰段 PDT 差，差超過 `hls_discontinuity_seconds` 時於該段前插 `#EXT-X-DISCONTINUITY` 並用 nominal `_HLS_TIME`。`#EXT-X-PIG-FRAMEID` 寫入此 Task 先保留（Task 6 刪 `_seg_first_fid` 時一併移除），確保 commit 綠燈。

**Files:**
- Modify: `hls_manager.py`（`corrected_m3u8`）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

於 `tests/test_hls_manager.py` 末尾新增：

```python
def test_corrected_m3u8_real_pdt_and_extinf(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:4.000000,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\n"
        "seg_000.ts\n"
        "#EXTINF:4.000000,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.000+08:00\n"
        "seg_001.ts\n"
    )
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0, "seg_001.ts": 5004.5}
    from hls_manager import _iso_local
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(5000.0)}" in out
    assert f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(5004.5)}" in out
    # seg_000 EXTINF = 5004.5 - 5000.0 = 4.5（真實，非 ffmpeg 的 4.0）
    assert "#EXTINF:4.500000," in out
    assert "#EXT-X-DISCONTINUITY" not in out


def test_corrected_m3u8_inserts_discontinuity_on_big_gap(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\nseg_000.ts\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.000+08:00\nseg_001.ts\n"
    )
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0, "seg_001.ts": 5050.0}  # 50s gap
    out = stream.corrected_m3u8(stream.out_dir.name)
    lines = out.splitlines()
    i = lines.index("seg_001.ts")
    assert "#EXT-X-DISCONTINUITY" in lines[:i]
    from hls_manager import _HLS_TIME
    assert f"#EXTINF:{float(_HLS_TIME):.6f}," in out  # gap 段用 nominal
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest -q tests/test_hls_manager.py -k "real_pdt_and_extinf or discontinuity_on_big_gap"`
Expected: FAIL（目前 EXTINF 沿用 ffmpeg 原值、無 DISCONTINUITY 邏輯）。

- [ ] **Step 3: 改 `corrected_m3u8`（hls_manager.py:233-270）**

替換整個方法為（保留 PIG-FRAMEID 寫入；新增真實 EXTINF + DISCONTINUITY）：

```python
    def corrected_m3u8(self, date_hour: str) -> Optional[str]:
        """live index.m3u8：每段 #EXT-X-PROGRAM-DATE-TIME 改寫成 _seg_pdt
        真實擷取時間，#EXTINF 改為相鄰段 PDT 差；差 > 不連續門檻則於該段
        前插 #EXT-X-DISCONTINUITY 並用 nominal _HLS_TIME。非當前小時→None。"""
        if date_hour != self.out_dir.name:
            return None
        m3u8_path = self.out_dir / "index.m3u8"
        try:
            text = m3u8_path.read_text()
        except OSError:
            return None
        with self._seg_lock:
            seg_pdt = dict(self._seg_pdt)
            seg_fid = dict(self._seg_first_fid)
        disc = getattr(settings, "hls_discontinuity_seconds", 8.0)
        # 依 m3u8 出現順序蒐集 segment 名，算每段真實時長
        seg_order = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        ]
        def _dur(name: str, nxt: Optional[str]) -> tuple[float, bool]:
            a = seg_pdt.get(name)
            b = seg_pdt.get(nxt) if nxt else None
            if a is None or b is None:
                return float(_HLS_TIME), False
            gap = b - a
            if gap <= 0 or gap > disc:
                return float(_HLS_TIME), True   # 不連續
            return gap, False
        out: list[str] = []
        last_pdt_idx: Optional[int] = None
        pending_extinf_idx: Optional[int] = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                last_pdt_idx = len(out)
                out.append(line)
                continue
            if line.startswith("#EXTINF:"):
                pending_extinf_idx = len(out)
                out.append(line)
                continue
            if line and not line.startswith("#"):
                seg_name = line.strip()
                idx = seg_order.index(seg_name) if seg_name in seg_order else -1
                nxt = seg_order[idx + 1] if 0 <= idx < len(seg_order) - 1 else None
                cap = seg_pdt.get(seg_name)
                if cap is not None:
                    corrected = f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap)}"
                    if last_pdt_idx is not None:
                        out[last_pdt_idx] = corrected
                    else:
                        out.append(corrected)
                    dur, is_disc = _dur(seg_name, nxt)
                    if pending_extinf_idx is not None:
                        out[pending_extinf_idx] = f"#EXTINF:{dur:.6f},"
                    if is_disc:
                        ins = pending_extinf_idx if pending_extinf_idx is not None else len(out)
                        out.insert(ins, "#EXT-X-DISCONTINUITY")
                fid = seg_fid.get(seg_name)
                if fid is not None:
                    out.append(f"#EXT-X-PIG-FRAMEID:{fid}")
                last_pdt_idx = None
                pending_extinf_idx = None
            out.append(line)
        return "\n".join(out) + "\n"
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest -q tests/test_hls_manager.py`
Expected: 全綠（既有 `corrected_m3u8` 測試若斷言舊 EXTINF 行為需同步調整——若有 `test_corrected_m3u8_*` 既有測試 fail，檢視其斷言：舊測試多半只斷言 PDT 被改寫與 PIG-FRAMEID 存在，仍會過；若斷言「EXTINF 原樣保留」則該斷言與新規格衝突，更新為新行為）。

- [ ] **Step 5: 全套件零回歸**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗。

- [ ] **Step 6: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): corrected_m3u8 真實 EXTINF + 大 gap 補 DISCONTINUITY"
```

---

## Task 4: `vod_generator` 讀 sidecar（真實 PDT/EXTINF + 逐段 PDT + fallback）

`_parse_hour_m3u8` 讀同目錄 `pdt.jsonl`：有則 `seg_start`=真實 pdt、`#EXTINF`=相鄰 pdt 差（>門檻 → 標記不連續、用 nominal）；缺則回退現行 `hour_unix+ΣEXTINF`（舊錄影/thermal 路徑不變）。`build_vod_m3u8` 改逐段輸出 `#EXT-X-PROGRAM-DATE-TIME`，並在不連續處輸出 `#EXT-X-DISCONTINUITY`。

**Files:**
- Modify: `vod_generator.py`（`build_vod_m3u8`、`_parse_hour_m3u8`）
- Test: `tests/test_vod_generator.py`（新建，若不存在）

- [ ] **Step 1: 寫失敗測試（新建 `tests/test_vod_generator.py`）**

```python
import json
from pathlib import Path

from vod_generator import build_vod_m3u8


def _write_hour(base: Path, cam: str, st: str, hour_name: str,
                segs: list[str], extinfs: list[float], sidecar: dict | None):
    d = base / cam / st / hour_name
    d.mkdir(parents=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:5"]
    for seg, e in zip(segs, extinfs):
        lines += [f"#EXTINF:{e:.6f},", seg]
    (d / "index.m3u8").write_text("\n".join(lines) + "\n")
    for seg in segs:
        (d / seg).write_bytes(b"x")
    if sidecar is not None:
        with (d / "pdt.jsonl").open("a") as fh:
            for seg, pdt in sidecar.items():
                fh.write(json.dumps({"seg": seg, "pdt": pdt}) + "\n")


def test_vod_uses_sidecar_real_pdt(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    # 2099-01-01 00:00 本地 → 用該小時的 unix 當基準
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts"], [4.0, 4.0],
                {"seg_000.ts": hour_unix + 1.0, "seg_001.ts": hour_unix + 5.5})
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    assert m3u8 is not None
    # 逐段 PDT 出現、seg_000 EXTINF = 5.5-1.0 = 4.5（真實）
    assert m3u8.count("#EXT-X-PROGRAM-DATE-TIME:") >= 2
    assert "#EXTINF:4.500000," in m3u8


def test_vod_falls_back_without_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts"], [4.0, 4.0], None)
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    assert m3u8 is not None
    assert "#EXTINF:4.000000," in m3u8   # 回退舊 ΣEXTINF 行為


def test_vod_discontinuity_on_big_gap(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts"], [4.0, 4.0],
                {"seg_000.ts": hour_unix + 1.0, "seg_001.ts": hour_unix + 60.0})
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    assert "#EXT-X-DISCONTINUITY" in m3u8
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest -q tests/test_vod_generator.py`
Expected: FAIL（目前不讀 sidecar、無逐段 PDT、無 DISCONTINUITY）。

- [ ] **Step 3: 改 `vod_generator.py`**

替換 `build_vod_m3u8` 與 `_parse_hour_m3u8`（保留 import；`build_vod_m3u8` 簽名不變）：

```python
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings

_DISC = getattr(settings, "hls_discontinuity_seconds", 8.0)


def _iso_local(ts: float) -> str:
    dt = datetime.fromtimestamp(ts).astimezone()
    base = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    off = dt.strftime("%z")
    return f"{base}{off[:3]}:{off[3:]}"


def build_vod_m3u8(
    camera_id: str,
    stream_type: str,
    start_ts: float,
    end_ts: float,
) -> Optional[str]:
    base = Path(settings.hls_base_dir)
    start_hour = int(start_ts // 3600) * 3600
    end_hour = int(end_ts // 3600) * 3600

    # (seg_start, dur, url, is_discontinuity)
    all_segments: list[tuple[float, float, str, bool]] = []
    max_target_duration = 4

    current_hour = start_hour
    while current_hour <= end_hour:
        dt = datetime.fromtimestamp(current_hour)
        dir_name = dt.strftime("%Y-%m-%d-%H")
        m3u8_path = base / camera_id / stream_type / dir_name / "index.m3u8"
        if m3u8_path.exists():
            segs, td = _parse_hour_m3u8(
                m3u8_path, current_hour, camera_id, stream_type, dir_name
            )
            all_segments.extend(segs)
            max_target_duration = max(max_target_duration, td)
        current_hour += 3600

    in_range = [
        (ts, dur, url, disc) for ts, dur, url, disc in all_segments
        if ts >= start_ts and ts < end_ts
    ]
    if not in_range:
        return None

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max_target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for ts, dur, url, disc in in_range:
        if disc:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(ts)}")
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _load_sidecar(hour_dir: Path) -> dict[str, float]:
    path = hour_dir / "pdt.jsonl"
    out: dict[str, float] = {}
    try:
        for raw in path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                out[rec["seg"]] = float(rec["pdt"])
            except (ValueError, KeyError, TypeError):
                continue   # 容錯：跳過半行/壞行
    except OSError:
        pass
    return out


def _parse_hour_m3u8(
    m3u8_path: Path,
    hour_unix: int,
    camera_id: str,
    stream_type: str,
    dir_name: str,
) -> tuple[list[tuple[float, float, str, bool]], int]:
    text = m3u8_path.read_text()
    td_match = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
    target_duration = int(td_match.group(1)) if td_match else 4

    # 逐行：segment URI = #EXTINF 後第一行非 # 開頭非空行
    seg_names: list[str] = []
    seg_extinf: list[float] = []
    pending: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+),", line)
            if m:
                pending = float(m.group(1))
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        seg_names.append(line)
        seg_extinf.append(pending)
        pending = None

    sidecar = _load_sidecar(m3u8_path.parent)
    segments: list[tuple[float, float, str, bool]] = []

    if sidecar and all(s in sidecar for s in seg_names) and seg_names:
        for i, name in enumerate(seg_names):
            start = sidecar[name]
            if i + 1 < len(seg_names) and seg_names[i + 1] in sidecar:
                gap = sidecar[seg_names[i + 1]] - start
                if gap <= 0 or gap > _DISC:
                    dur, disc = float(target_duration), True
                else:
                    dur, disc = gap, False
            else:
                dur, disc = float(target_duration), False
            url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{name}"
            segments.append((start, dur, url, disc))
        return segments, target_duration

    # 缺 sidecar（舊錄影 / thermal）→ 回退舊 hour_unix+ΣEXTINF
    accumulated = 0.0
    for name, e in zip(seg_names, seg_extinf):
        seg_start = float(hour_unix) + accumulated
        url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{name}"
        segments.append((seg_start, e, url, False))
        accumulated += e
    return segments, target_duration
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest -q tests/test_vod_generator.py`
Expected: 全綠。若另有舊 VOD 測試（grep `build_vod_m3u8` 於 `tests/`）斷言舊回傳結構，更新為新四元組 / 逐段 PDT 行為。

- [ ] **Step 5: 全套件零回歸**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗。

- [ ] **Step 6: Commit**

```bash
git add vod_generator.py tests/test_vod_generator.py
git commit -m "feat(vod): 讀 sidecar 真實 PDT/EXTINF + 逐段 PDT + 缺則回退"
```

---

## Task 5: `zmq_receiver` rgb feed 停傳 frame_id

rgb feed 呼叫只傳 `capture_ts`，不再傳 `frame_id`（為 Task 6 移除 `feed` 的 `frame_id` 參數鋪路）。`pipeline.update_frame(... frame_id)` **不動**（frame_id 端到端資料流保留）。

**Files:**
- Modify: `zmq_receiver.py:152`
- Test: `tests/test_zmq_receiver.py`（屬基線忽略集，仍就地更新斷言；不納入綠燈門檻）

- [ ] **Step 1: 改呼叫（zmq_receiver.py:152）**

```python
            hls_mod.hls_manager.feed(label, "rgb", rgb_bytes, capture_ts=ts)
```

（第 157 行 thermal feed 不變；第 160-162 `pipeline_mod.inference_pipeline.update_frame(label, rgb_np, thermal_np, ts, frame_id)` 不變。）

- [ ] **Step 2: Commit**

```bash
git add zmq_receiver.py
git commit -m "refactor(zmq): rgb feed 停傳 frame_id（HLS 同步不再用 FID）"
```

---

## Task 6: 刪除 FID / offset / EMA 技術債（hls_manager + 端點 + 設定 + config）

移除 `_fed_log`/`_fed_count`/`_seg_first_fid`/`#EXT-X-PIG-FRAMEID`、`_update_pdt_offset`/`get_pdt_offset`/`_pdt_offset`、`feed`/`HLSManager.feed` 的 `frame_id` 參數；`/live` 移除 `pdt_offset`；`config.py`/`routers/settings.py` 移除 `live_pdt_offset_seconds`。

**Files:**
- Modify: `hls_manager.py`、`routers/stream.py:64-88`、`config.py:107-109`、`routers/settings.py:9-24,35-43`
- Test: `tests/test_hls_manager.py`、`tests/test_stream_router.py`、`tests/test_settings_router.py`

- [ ] **Step 1: 寫失敗測試**

`tests/test_hls_manager.py` 末尾新增：

```python
def test_corrected_m3u8_no_pig_frameid_tag(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
            "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\nseg_000.ts\n")
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0}
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert "#EXT-X-PIG-FRAMEID" not in out


def test_hls_manager_has_no_pdt_offset_api():
    from hls_manager import HLSManager
    assert not hasattr(HLSManager, "get_pdt_offset")
    assert not hasattr(HLSManager, "_update_pdt_offset")


def test_feed_signature_has_no_frame_id():
    import inspect
    from hls_manager import HLSStream, HLSManager
    assert "frame_id" not in inspect.signature(HLSStream.feed).parameters
    assert "frame_id" not in inspect.signature(HLSManager.feed).parameters
```

`tests/test_stream_router.py`：找現有 `test_live_includes_pdt_offset`，改名/改斷言為「無 pdt_offset」：

```python
def test_live_excludes_pdt_offset(client):
    # （沿用該檔既有 client/啟動 fixture；此測試本就因待辦 #12 ZMQ gap 可能 404，
    #   仍保留斷言內容，待 #12 修復後生效；不納入綠燈門檻）
    resp = client.get("/stream/cam_01/live")
    if resp.status_code == 200:
        assert "pdt_offset" not in resp.json()
```

`tests/test_settings_router.py`：移除/改寫斷言 `live_pdt_offset_seconds` 在 `ALLOWED_KEYS` 的測試 → 斷言其**不**在：

```python
def test_live_pdt_offset_removed_from_allowed_keys():
    from routers.settings import ALLOWED_KEYS
    assert "live_pdt_offset_seconds" not in ALLOWED_KEYS
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest -q tests/test_hls_manager.py -k "no_pig_frameid or no_pdt_offset_api or signature_has_no_frame_id" tests/test_settings_router.py -k "live_pdt_offset_removed"`
Expected: FAIL。

- [ ] **Step 3: hls_manager 刪 FID/offset 內部**

- `HLSStream.__init__`：刪 `self._fed_count`/`self._fed_log`/`self._seg_first_fid` 三行。
- `feed`：移除 `frame_id` 參數（簽名變 `def feed(self, jpeg_bytes, capture_ts=None):`）。
- `_emit_frame`：已是 `(frame, capture_ts)`（Task 1），不變。
- `_scan_new_segments`：刪 `self._seg_first_fid` 相關（Task 2 已不寫，確認無殘留引用）。
- `corrected_m3u8`：刪 `seg_fid = dict(self._seg_first_fid)` 與 `fid = seg_fid.get(...)` / `#EXT-X-PIG-FRAMEID` 三處。
- `_restart`：刪 `self._seg_first_fid.clear()`、`self._fed_log.clear()`、`self._fed_count = 0`。
- `HLSManager.__init__`：刪 `self._pdt_offset = {}`。
- `HLSManager.feed`：移除 `frame_id` 參數與 `if capture_ts is not None and stream_type == "rgb": self._update_pdt_offset(...)`；呼叫改 `stream.feed(jpeg_bytes, capture_ts)`。
- 刪整個 `_update_pdt_offset`、`get_pdt_offset` 方法與 `_PDT_OFFSET_*` 三個類別常數。
- 刪 `_FED_LOG_MAX` 若不再被引用？仍被 `_emit_log` 用（`maxlen=_FED_LOG_MAX`）→ **保留** `_FED_LOG_MAX`。

- [ ] **Step 4: routers/stream.py `/live` 移除 pdt_offset（64-88）**

```python
@router.get("/{camera_id}/live")
async def get_live_stream(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in [s.label for s in settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")
    out_dir = hls_manager.ensure_started(camera_id, stream_type)
    return {
        "url": f"/stream/hls/{camera_id}/{stream_type}/{out_dir.name}/index.m3u8",
    }
```

並移除該檔頂部不再使用的 import：`import database`、`from db_writer import get_all_settings`（確認檔內無其他使用後再刪；`get_timeline`/`serve_hls`/`get_vod_stream` 不需它們）。

- [ ] **Step 5: config.py 刪 live_pdt_offset_seconds（107-109）**

刪除註解區塊與 `live_pdt_offset_seconds: float = 2.0` 該行。

- [ ] **Step 6: routers/settings.py 移除 live_pdt_offset_seconds**

- `ALLOWED_KEYS`：刪 `"live_pdt_offset_seconds",`。
- `get_settings` 無 pool 的 fallback dict（第 35-43 行）：刪 `"live_pdt_offset_seconds": str(app_settings.live_pdt_offset_seconds),` 該行。
- `_RELOAD_KEYS` 未含它，不動。

- [ ] **Step 7: 跑測試確認通過**

Run: `uv run pytest -q tests/test_hls_manager.py tests/test_settings_router.py`
Expected: 全綠（新斷言通過；既有不涉刪除項者不受影響）。

- [ ] **Step 8: 全套件零回歸**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗（`test_stream_router` 那 4 個 404 仍因待辦 #12 而失敗，屬既有）。

- [ ] **Step 9: Commit**

```bash
git add hls_manager.py routers/stream.py config.py routers/settings.py tests/
git commit -m "refactor: 刪除 FID/手動 offset/PDT-EMA 整套修正債"
```

---

## Task 7: 前端統一（刪 FID/livePdtOffset、live=playingDate、VOD=播放器 PDT、HUD 簡化）

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 刪 FID 狀態與 parseFragFid（index.html:867-883）**

把 867–883 行（`// frame_id 幀身分對應` 至 `let livePdtOffset = 0; ...` 之前）整段刪除，僅保留：

```javascript
    let _dbg = null;        // live-sync diagnostics snapshot (press 'd' to toggle HUD)
    let currentCamera = null;
```

即移除：`fidBySn`、`liveFragFid`、`liveFragNextFid`、`liveFragStart`、`liveFragDur`、`FPS_HINT`、`parseFragFid`、`livePdtOffset`。

- [ ] **Step 2: bboxHistory.push 去掉 fid（index.html:1417）**

```javascript
            bboxHistory.push({ ts: data.timestamp, boxes: latestBoxes });
```

- [ ] **Step 3: drawBoxes live 改純 playingDate（index.html:1469-1533）**

把 `if (isLive && bboxHistory.length) { ... } else { _dbg = null; }` 整段（1469–1533）替換為：

```javascript
      if (isLive && bboxHistory.length) {
        let targetTs = null;
        let dbgSrc = 'latest';
        let chosenTs = bboxHistory[bboxHistory.length - 1].ts;
        const pd = hls && hls.playingDate;
        if (pd && !isNaN(pd.getTime())) {
          targetTs = pd.getTime() / 1000;      // PDT≡真實擷取時間，不再減 offset
          dbgSrc = 'PDT';
        } else {
          const latency = (hls && hls.latency != null) ? hls.latency : 0;
          if (latency > 1) { targetTs = Date.now() / 1000 - latency; dbgSrc = 'latency'; }
        }
        if (targetTs != null) {
          let best = bboxHistory[bboxHistory.length - 1];
          let bestDist = Infinity;
          for (const entry of bboxHistory) {
            const d = Math.abs(entry.ts - targetTs);
            if (d < bestDist) { bestDist = d; best = entry; }
          }
          displayBoxes = best.boxes;
          chosenTs = best.ts;
        }
        if (window.__bboxDebug) {
          const now = Date.now() / 1000;
          _dbg = {
            src: dbgSrc, now,
            latency: (hls && hls.latency != null) ? hls.latency : null,
            playingDate: (pd && !isNaN(pd.getTime())) ? pd.getTime() / 1000 : null,
            targetTs, chosenTs,
            newestTs: bboxHistory[bboxHistory.length - 1].ts,
            histLen: bboxHistory.length,
          };
        } else { _dbg = null; }
      } else { _dbg = null; }
```

- [ ] **Step 4: HUD 簡化（index.html:1599-1611）**

把 `_hudEl.textContent = [ ... ].join('\n');` 的陣列改為（移除 `fid`、`pdtOffset` 行）：

```javascript
      _hudEl.textContent = [
        `src=${d.src}  hist=${d.histLen}`,
        `hls.playingDate=${fmt(d.playingDate)}`,
        `hls.latency=${fmt(d.latency)}`,
        `now=${fmt(d.now)}`,
        `targetTs=${fmt(d.targetTs)}`,
        `chosenBbox.ts=${fmt(d.chosenTs)}`,
        `newestBbox.ts=${fmt(d.newestTs)}`,
        `chosen-target=${fmt(leadTarget)}s`,
        `newest-target=${fmt(leadNewest)}s`,
      ].join('\n');
```

- [ ] **Step 5: 移除 livePdtOffset 30s 重抓（index.html:1638-1651）**

刪除整段 `// Refresh the server-measured PDT offset ...` 的 `setInterval(async () => { ... }, 30000);`（1638–1651）。

- [ ] **Step 6: loadStream 移除 livePdtOffset 與 FID 事件（index.html:1669-1718）**

- 刪 `livePdtOffset = (typeof live.pdt_offset === 'number' && isFinite(live.pdt_offset)) ? live.pdt_offset : 0;`（1669-1670）。
- 刪整個 `hls.on(Hls.Events.LEVEL_LOADED, (_, data) => { ... fidBySn = map; });`（1698-1706）。
- 刪整個 `hls.on(Hls.Events.FRAG_CHANGED, (_, data) => { ... });`（1708-1718）。
- 保留其後 `hls.on(Hls.Events.LEVEL_LOADED, () => { ...狀態... })`（1720+，與 FID 無關）。

- [ ] **Step 7: VOD scheduleTrackingFetch 改用播放器 PDT（index.html:1138-1142）**

把 `run` 內 `const ts = vodStartTs + (video.currentTime || 0);` 改為（優先播放器 PDT，不可用回退舊式）：

```javascript
        let ts;
        const pd = hls && hls.playingDate;
        if (pd && !isNaN(pd.getTime())) ts = pd.getTime() / 1000;          // 每段重錨、不累積
        else ts = vodStartTs + (video.currentTime || 0);                   // 舊錄影/無PDT回退
        if (!ts) return;
```

- [ ] **Step 8: 移除設定面板手動 offset 欄位**

- HTML：刪 `static/index.html:840-843` 的 `<div class="settings-field"> ... set-live-pdt-offset ... </div>` 整塊。
- loadSettings：刪 `const po = ...`（1329）與 `if (po && data.live_pdt_offset_seconds !== undefined) po.value = ...`（1330）。
- saveSettings：刪 `live_pdt_offset_seconds: document.getElementById('set-live-pdt-offset').value,`（1349）。

- [ ] **Step 9: 結構檢查（自動化 gate，取代無 JS 測試框架）**

Run:
```bash
grep -nE "parseFragFid|fidBySn|liveFragFid|livePdtOffset|PIG-FRAMEID|pdt_offset|set-live-pdt-offset" static/index.html
```
Expected: 無輸出（全數移除）。

Run:
```bash
python -c "import re,sys;s=open('static/index.html').read();print('OK' if s.count('{')>0 else 'EMPTY')"
```
Expected: `OK`（基本未截斷；實際行為靠 Step 10 瀏覽器驗收）。

- [ ] **Step 10: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): live/VOD 統一用播放器 PDT，刪除 FID 配對/手動 offset/HUD 簡化"
```

---

## Task 8: 全套件驗證 + 整合 smoke 指引 + CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`（gitignore，**不 commit**，僅就地更新）

- [ ] **Step 1: 全套件最終驗證**

Run: `uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
Expected: 僅既有 5 失敗（`test_config::test_default_mot_worker_threads` + 4 `test_stream_router` 404，皆待辦 #12），其餘全綠。記錄 `N passed`。

- [ ] **Step 2: 整合 smoke 指引（手動，使用者執行；寫入 CLAUDE.md 待辦）**

文件化（非自動跑）：真 ffmpeg + 合成幀以 13fps 餵、`hls_target_fps=20`，跑數段後比對 `pdt.jsonl` 各段 pdt 差與已知輸入時間軸（應 ≈ 真實段長、不隨段序累積）。

- [ ] **Step 3: 更新 CLAUDE.md（不 commit）**

在 CLAUDE.md「## live FID 同步：FPS 依賴殘差」段之後新增一節，記錄：本架構替換已落地（writer 節拍器 + emit_log 真實錨點 + sidecar + corrected/vod 真實 EXTINF/DISCONTINUITY + 前端統一 playingDate），FID/offset/EMA 已刪；spec/plan 路徑；待瀏覽器驗收項（live 跨 restart 數小時不漂、VOD 拖曳貼齊、**FPS 10/15/20 全綠**、thermal/舊錄影 fallback 不 crash、HUD 簡化）；Phase 4.5 註記：若驗收 FPS15 仍漂則根因模型錯誤需再退一步（非本架構內再補）。

- [ ] **Step 4: 最終 commit（程式碼若有殘留；CLAUDE.md 不納入）**

```bash
git status
# 若有未提交的程式碼變更才 commit；CLAUDE.md 為 gitignore 不處理
```

---

## Self-Review

**1. Spec coverage：** §3 原則→T1/T2/T3/T4；§4.1 writer→T1；§4.2 emit_log 錨點→T1/T2；§4.3 sidecar→T2；§4.4 EXTINF/DISCONTINUITY→T3/T4；§4.5 thermal fallback→T4(缺 sidecar 回退);§5.1 vod_generator→T4；§5.2 前端→T7；§6 刪除清單→T5/T6/T7；§7 config 參數→T1(加)/T6(刪 offset)；§8 測試→各 Task TDD + T8；§9 Phase 4.5→T8 CLAUDE.md；§10 不處理項→未排任務（正確）。無缺漏。

**2. Placeholder scan：** 無 TBD/「類似 Task N」；每個程式步驟均含完整程式碼或精確錨點刪改；指令與預期輸出明確。前端無 JS 測試框架 → 以 grep 結構檢查 + 瀏覽器驗收替代（已於 T7 Step 9/10 明示，非 placeholder）。

**3. Type consistency：** `_frame_buffer: deque[tuple[bytes, Optional[float]]]`（T1）與 `_writer_tick`/`_emit_frame(frame, capture_ts)`/`_emit_log: deque[tuple[int,float]]`/`_emit_idx` 命名跨 T1–T6 一致；`feed(self, jpeg_bytes, capture_ts=None[, frame_id=None→T6 移除])` 演進路徑明確且每 commit 綠燈；`_seg_pdt` 既有名沿用；vod_generator segment 四元組 `(seg_start, dur, url, is_disc)` T4 內一致；`_FED_LOG_MAX` 保留供 `_emit_log` maxlen。一致。
