# Live bbox frame_id 幀身分對應 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 live bbox 依 `frame_id`（單調遞增、與時鐘無關）而非時鐘相減與畫面同步，消除手動/常數 offset、誤差不隨時間累積。

**Architecture:** 後端 `hls_manager` 在每個 HLS segment 旁，用「餵入幀計數」推算其首幀對應的擷取 `frame_id`，寫進 corrected m3u8 的自訂標籤 `#EXT-X-PIG-FRAMEID`；前端讀 hls.js fragment 標籤建立 媒體位置→frame_id 映射，於 `bboxHistory` 以 `frame_id` 精準配對。任何缺標籤情境一律回退現有 `playingDate`+offset 路徑，保證不比現況差。

**Tech Stack:** Python（FastAPI / threading / ffmpeg HLS）、pytest、原生 JS + hls.js。

**參考文件:** `docs/superpowers/specs/2026-05-18-frameid-bbox-sync-design.md`

**全程約束:** commit 授權、**不 push**（留本機 master）。`CLAUDE.md`、`ref/HybridSORT/` 為 gitignore，永不 commit/force-add。沙箱拒絕讀 `.env` 與 `data/pig_monitoring/hls`，勿嘗試。回應一律繁體中文。

**測試基線（每次全套件比對用）:** 以
`pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
跑。既有 5 個失敗為預存在（待辦 #12 ZMQ_SOURCES OS-env gap）：
`tests/test_config.py::test_default_mot_worker_threads` 1 個 +
`tests/test_stream_router.py` 4 個 404。新增測試不得使這 5 個以外的測試變紅。

---

## File Structure

| 檔案 | 職責 | 本計畫變更 |
|---|---|---|
| `hls_manager.py` | HLS 串流管理：feed→ffmpeg、segment 偵測、corrected m3u8 | 新增 `_fed_log`/`_fed_count`/`_seg_first_fid`、`feed` 帶 `frame_id`、`_scan_new_segments` 幀計數錨點、`corrected_m3u8` 寫 `#EXT-X-PIG-FRAMEID`、`_restart` 清空、`HLSManager.feed` 透傳 |
| `zmq_receiver.py` | ZMQ 收幀 → 解碼 / 餵 HLS / 推論 | rgb `feed(...)` 呼叫補帶 `frame_id` |
| `static/index.html` | 前端播放器 + bbox overlay | `bboxHistory` 存 `fid`、FRAG/LEVEL 事件建 sn→fid 映射、`drawBoxes` live 改 FID 配對 + HUD 欄位 |
| `tests/test_hls_manager.py` | hls_manager 單元測試 | 新增 4 組測試（feed 帶 frame_id、幀計數錨點、m3u8 標籤、_restart 清空、透傳）|
| `CLAUDE.md`（gitignore，不 commit） | 專案備註 | 新增本次修正章節 |

前端無 JS 測試框架（與既有 Phase 4/5/6 前端任務一致）→ Task 5 以精確 edit + 瀏覽器待測清單交付，不寫 JS 自動化測試。

---

### Task 1: `hls_manager.py` — `feed()` 串接 `frame_id` 與餵入幀記錄

**Files:**
- Modify: `hls_manager.py`（模組常數區 ~第 41 行附近；`HLSStream.__init__` ~149-153；`feed` ~165-176）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_hls_manager.py` 末尾新增：

```python
def test_feed_threads_frame_id_into_fed_log(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream.feed(b"\xff\xd8\xff", capture_ts=1_700_000_001.0, frame_id=10)
    stream.feed(b"\xff\xd8\xff", capture_ts=1_700_000_001.1, frame_id=11)
    assert stream._fed_count == 2
    assert list(stream._fed_log) == [(0, 10), (1, 11)]
    # 無 frame_id（thermal）→ 計數仍增、但不記入 _fed_log
    stream.feed(b"\xff\xd8\xff")
    assert stream._fed_count == 3
    assert list(stream._fed_log) == [(0, 10), (1, 11)]
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_hls_manager.py::test_feed_threads_frame_id_into_fed_log -v`
Expected: FAIL（`AttributeError: '_fed_count'` / `feed() got unexpected keyword 'frame_id'`）

- [ ] **Step 3: 加模組常數**

`hls_manager.py` 找到現有 `TARGET_FPS: int = getattr(settings, "hls_target_fps", 25)`（約 41 行），在其後新增：

```python
# segment 時長（與 _make_ffmpeg_cmd 的 -hls_time 一致）
_HLS_TIME: int = 4
# 餵入幀 (fed_index, frame_id) 記錄上限（約 30 分鐘餘量，遠超單一小時所需）
_FED_LOG_MAX: int = TARGET_FPS * 1800
```

- [ ] **Step 4: `HLSStream.__init__` 加狀態**

`hls_manager.py` 在 `self._last_scan: float = 0.0`（約 153 行）之後、`# 啟動 writer 執行緒` 註解之前，新增：

```python
        # 幀身分對應：餵入幀計數 + (fed_index, frame_id) 環形記錄，
        # 與 segment 首幀 frame_id 對應（_scan_new_segments 用幀計數推算）。
        self._fed_count: int = 0
        self._fed_log: deque[tuple[int, int]] = deque(maxlen=_FED_LOG_MAX)
        self._seg_first_fid: dict[str, int] = {}
```

- [ ] **Step 5: `feed()` 增 `frame_id` 參數**

`hls_manager.py` 將 `HLSStream.feed`（約 165 行）簽名與本體改為：

```python
    def feed(
        self,
        jpeg_bytes: bytes,
        capture_ts: Optional[float] = None,
        frame_id: Optional[int] = None,
    ) -> None:
        """把新幀放入 buffer；若 buffer 滿則自動丟棄最舊幀（deque maxlen 行為）。
        capture_ts 為該幀真實擷取牆鐘（後端自管 PDT，fallback 用）；
        frame_id 為該幀身分（幀計數錨點 → segment↔frame_id 對應）。"""
        with self._lock:
            new_dir = self._hour_dir()
            if new_dir != self.out_dir:
                self._restart(new_dir)
        if capture_ts is not None:
            self._last_capture_ts = capture_ts
        self._fed_count += 1
        if frame_id is not None:
            self._fed_log.append((self._fed_count - 1, int(frame_id)))
        self.last_feed_time = time.time()
```

（其後原本 `self._frame_buffer.append(...)` 等行保持不變。）

- [ ] **Step 6: 跑測試確認通過**

Run: `pytest tests/test_hls_manager.py::test_feed_threads_frame_id_into_fed_log -v`
Expected: PASS

- [ ] **Step 7: 既有 feed 測試不回歸**

Run: `pytest tests/test_hls_manager.py -q`
Expected: 全部 PASS（既有 `test_feed_threads_capture_ts_into_stream` 等仍綠）

- [ ] **Step 8: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): feed() 串接 frame_id 與餵入幀計數記錄"
```

---

### Task 2: `hls_manager.py` — `_scan_new_segments` 幀計數錨點 + `_restart` 清空

**Files:**
- Modify: `hls_manager.py`（imports 第 1-7 行加 `re`；`_scan_new_segments` ~178-195；`_restart` ~287-296）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_hls_manager.py` 末尾新增：

```python
def test_scan_records_seg_first_fid_by_frame_count(tmp_path, monkeypatch):
    """segment 首幀 frame_id 用『餵入幀計數』推算（避開管線延遲 L），
    取 fed_index 最接近 round(ordinal*TARGET_FPS*_HLS_TIME) 的 frame_id。"""
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    # 餵入足夠覆蓋到 seg_002 期望位置的記錄：fed_index i → frame_id 1000+i
    expected2 = round(2 * TARGET_FPS * _HLS_TIME)
    for i in range(expected2 + 5):
        stream._fed_log.append((i, 1000 + i))
    (stream.out_dir / "seg_002.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_first_fid["seg_002.ts"] == 1000 + expected2
    # 同名不覆寫
    stream._fed_log.append((expected2, 99999))
    stream._scan_new_segments()
    assert stream._seg_first_fid["seg_002.ts"] == 1000 + expected2
    # _fed_log 為空 → 不記
    stream._fed_log.clear()
    (stream.out_dir / "seg_003.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert "seg_003.ts" not in stream._seg_first_fid


def test_restart_clears_frameid_state(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._fed_count = 5
    stream._fed_log.append((4, 77))
    stream._seg_first_fid["seg_000.ts"] = 77
    new_dir = stream.out_dir.parent / "2099-01-01-00"
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        stream._restart(new_dir)
    assert stream._fed_count == 0
    assert list(stream._fed_log) == []
    assert stream._seg_first_fid == {}
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_hls_manager.py::test_scan_records_seg_first_fid_by_frame_count tests/test_hls_manager.py::test_restart_clears_frameid_state -v`
Expected: FAIL（`_seg_first_fid` 未被寫入 / `_restart` 未清空）

- [ ] **Step 3: 加 `re` import**

`hls_manager.py` 第 1-3 行為 `import subprocess` / `import threading` / `import time`。在 `import time` 之後新增一行：

```python
import re
```

- [ ] **Step 4: `_scan_new_segments` 加幀計數錨點**

`hls_manager.py` 的 `_scan_new_segments`（約 178-195）目前 `with self._seg_lock:` 迴圈內為：

```python
        with self._seg_lock:
            for name in names:
                if name in self._seen_segs:
                    continue
                self._seen_segs.add(name)
                if cap is not None:
                    self._seg_pdt[name] = cap
            if len(self._seg_pdt) > 2000:  # ffmpeg 每小時 restart 會清，這只是保險
                for k in sorted(self._seg_pdt)[:-2000]:
                    self._seg_pdt.pop(k, None)
```

將其整段替換為（在記錄 `_seg_pdt` 旁，平行用幀計數推算 `_seg_first_fid`）：

```python
        with self._seg_lock:
            fed_log = list(self._fed_log)
            for name in names:
                if name in self._seen_segs:
                    continue
                self._seen_segs.add(name)
                if cap is not None:
                    self._seg_pdt[name] = cap
                m = re.match(r"seg_(\d+)\.ts$", name)
                if m and fed_log:
                    expected = round(int(m.group(1)) * TARGET_FPS * _HLS_TIME)
                    best_fid = min(
                        fed_log, key=lambda p: abs(p[0] - expected)
                    )[1]
                    self._seg_first_fid[name] = best_fid
            if len(self._seg_pdt) > 2000:  # ffmpeg 每小時 restart 會清，這只是保險
                for k in sorted(self._seg_pdt)[:-2000]:
                    self._seg_pdt.pop(k, None)
            if len(self._seg_first_fid) > 2000:
                for k in sorted(self._seg_first_fid)[:-2000]:
                    self._seg_first_fid.pop(k, None)
```

- [ ] **Step 5: `_restart` 清空幀身分狀態**

`hls_manager.py` 的 `_restart`（約 287-296）內 `with self._seg_lock:` 區塊目前為：

```python
        with self._seg_lock:  # 新小時、新 ffmpeg：舊 segment 對應已無意義
            self._seg_pdt.clear()
            self._seen_segs.clear()
        self._last_scan = 0.0
```

替換為：

```python
        with self._seg_lock:  # 新小時、新 ffmpeg：舊 segment 對應已無意義
            self._seg_pdt.clear()
            self._seen_segs.clear()
            self._seg_first_fid.clear()
        self._fed_log.clear()
        self._fed_count = 0
        self._last_scan = 0.0
```

- [ ] **Step 6: 跑測試確認通過**

Run: `pytest tests/test_hls_manager.py::test_scan_records_seg_first_fid_by_frame_count tests/test_hls_manager.py::test_restart_clears_frameid_state -v`
Expected: PASS

- [ ] **Step 7: 既有 scan/restart 測試不回歸**

Run: `pytest tests/test_hls_manager.py -q`
Expected: 全 PASS（既有 `test_scan_new_segments_records_last_capture_ts` 仍綠）

- [ ] **Step 8: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): _scan_new_segments 幀計數推算 segment 首幀 frame_id"
```

---

### Task 3: `hls_manager.py` — `corrected_m3u8` 寫 `#EXT-X-PIG-FRAMEID` + `HLSManager.feed` 透傳

**Files:**
- Modify: `hls_manager.py`（`HLSStream.corrected_m3u8` ~197-229；`HLSManager.feed` ~361-378）
- Test: `tests/test_hls_manager.py`

- [ ] **Step 1: 寫失敗測試**

在 `tests/test_hls_manager.py` 末尾新增：

```python
def test_corrected_m3u8_inserts_pig_frameid_tag(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    (stream.out_dir / "index.m3u8").write_text(_FFMPEG_M3U8)
    stream._seg_pdt = {"seg_000.ts": 1_900_000_000.0}
    stream._seg_first_fid = {"seg_000.ts": 4242}  # seg_001 故意未知
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert out is not None
    lines = out.splitlines()
    i0 = lines.index("seg_000.ts")
    i1 = lines.index("seg_001.ts")
    # seg_000 緊鄰前一行（URI 行前）含 frameid 標籤
    assert "#EXT-X-PIG-FRAMEID:4242" in lines[i0 - 3:i0]
    # 未知段不插入標籤
    assert not any("PIG-FRAMEID" in ln for ln in lines[i1 - 3:i1])
    assert out.count("seg_000.ts") == 1 and out.count("#EXTINF:") == 2


def test_manager_feed_threads_frame_id(tmp_path, monkeypatch):
    from hls_manager import HLSManager
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
        captured = {}
        real = m._streams[("cam_01", "rgb")].feed

        def spy(jpeg, capture_ts=None, frame_id=None):
            captured["frame_id"] = frame_id
            return real(jpeg, capture_ts, frame_id)

        m._streams[("cam_01", "rgb")].feed = spy
        m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=1_700_000_000.0, frame_id=55)
    assert captured["frame_id"] == 55
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `pytest tests/test_hls_manager.py::test_corrected_m3u8_inserts_pig_frameid_tag tests/test_hls_manager.py::test_manager_feed_threads_frame_id -v`
Expected: FAIL（無 PIG-FRAMEID 標籤 / `HLSManager.feed` 不接受 `frame_id`）

- [ ] **Step 3: `corrected_m3u8` 插入標籤**

`hls_manager.py` 的 `corrected_m3u8`（約 197-229），目前 `with self._seg_lock:` 之後的迴圈為：

```python
        with self._seg_lock:
            seg_pdt = dict(self._seg_pdt)
        out: list[str] = []
        last_pdt_idx: Optional[int] = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                last_pdt_idx = len(out)
                out.append(line)
                continue
            if line and not line.startswith("#"):
                cap = seg_pdt.get(line.strip())
                if cap is not None:
                    corrected = f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap)}"
                    if last_pdt_idx is not None:
                        out[last_pdt_idx] = corrected
                    else:
                        out.append(corrected)
                last_pdt_idx = None
            out.append(line)
        return "\n".join(out) + "\n"
```

替換為（取 `_seg_first_fid` 快照，於 segment URI 行前插入標籤）：

```python
        with self._seg_lock:
            seg_pdt = dict(self._seg_pdt)
            seg_fid = dict(self._seg_first_fid)
        out: list[str] = []
        last_pdt_idx: Optional[int] = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                last_pdt_idx = len(out)
                out.append(line)
                continue
            if line and not line.startswith("#"):
                seg_name = line.strip()
                cap = seg_pdt.get(seg_name)
                if cap is not None:
                    corrected = f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap)}"
                    if last_pdt_idx is not None:
                        out[last_pdt_idx] = corrected
                    else:
                        out.append(corrected)
                fid = seg_fid.get(seg_name)
                if fid is not None:
                    out.append(f"#EXT-X-PIG-FRAMEID:{fid}")
                last_pdt_idx = None
            out.append(line)
        return "\n".join(out) + "\n"
```

- [ ] **Step 4: `HLSManager.feed` 透傳 `frame_id`**

`hls_manager.py` 的 `HLSManager.feed`（約 361-378）替換為：

```python
    def feed(
        self,
        camera_id: str,
        stream_type: str,
        jpeg_bytes: bytes,
        capture_ts: float | None = None,
        frame_id: int | None = None,
    ) -> None:
        key: StreamKey = (camera_id, stream_type)
        if capture_ts is not None and stream_type == "rgb":
            self._update_pdt_offset(camera_id, capture_ts)
        with self._lock:
            stream = self._streams.get(key)
        if stream is not None:
            stream.feed(jpeg_bytes, capture_ts, frame_id)
        else:
            logger.debug(
                f"[{camera_id}/{stream_type}] feed() called but stream not started, dropping frame"
            )
```

- [ ] **Step 5: 跑測試確認通過**

Run: `pytest tests/test_hls_manager.py::test_corrected_m3u8_inserts_pig_frameid_tag tests/test_hls_manager.py::test_manager_feed_threads_frame_id -v`
Expected: PASS

- [ ] **Step 6: hls_manager 全測試不回歸**

Run: `pytest tests/test_hls_manager.py -q`
Expected: 全 PASS（既有 `test_corrected_m3u8_*`、`test_feed_*` 仍綠）

- [ ] **Step 7: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat(hls): corrected_m3u8 寫 #EXT-X-PIG-FRAMEID + HLSManager.feed 透傳 frame_id"
```

---

### Task 4: `zmq_receiver.py` — rgb feed 透傳 `frame_id`

**Files:**
- Modify: `zmq_receiver.py:152`
- Test: 無新增（`tests/test_zmq_receiver.py` 屬既有 baseline collection-error，待辦 #12；透傳已於 Task 3 `test_manager_feed_threads_frame_id` 於 manager 層覆蓋）

- [ ] **Step 1: 改 feed 呼叫**

`zmq_receiver.py` 第 152 行目前為：

```python
            hls_mod.hls_manager.feed(label, "rgb", rgb_bytes, capture_ts=ts)
```

改為（`frame_id` 為 `_on_frame` 既有參數，見第 137 行）：

```python
            hls_mod.hls_manager.feed(label, "rgb", rgb_bytes, capture_ts=ts, frame_id=frame_id)
```

thermal 那行（157 行 `feed(label, "thermal", thermal_bytes)`）**不改**——thermal 無 frame_id，全程降級，符合設計。

- [ ] **Step 2: 語法檢查**

Run: `python -m py_compile zmq_receiver.py`
Expected: 無輸出（compile 成功）

- [ ] **Step 3: Commit**

```bash
git add zmq_receiver.py
git commit -m "feat(zmq): rgb feed 透傳 frame_id 給 hls_manager"
```

---

### Task 5: `static/index.html` — 前端依 frame_id 配對 + HUD

**Files:**
- Modify: `static/index.html`（`bboxHistory` 註解/狀態 ~843；`ws.onmessage` push ~1379；`loadStream` live hls 事件 ~1632；live `drawBoxes` 區塊 ~1431-1470；HUD ~1525-1541）
- Test: 無 JS 自動化框架（既有狀況）→ 瀏覽器待測清單見 Task 6

> 註：hls.js 自訂標籤格式跨版本不一，採容錯解析（掃 `frag.tagList` 字串化後正則抓數字），缺標籤一律回退現有 PDT 路徑。

- [ ] **Step 1: `bboxHistory` 存 `fid`**

`static/index.html:1379` 目前：

```javascript
            bboxHistory.push({ ts: data.timestamp, boxes: latestBoxes });
```

改為：

```javascript
            bboxHistory.push({ ts: data.timestamp, fid: data.frame_id, boxes: latestBoxes });
```

- [ ] **Step 2: 加 segment→fid 映射狀態**

`static/index.html` 約 843-845 行（`let bboxHistory = [];` 與 `let _dbg = null;` 附近）之後新增：

```javascript
    // frame_id 幀身分對應：由 hls.js fragment 自訂標籤 #EXT-X-PIG-FRAMEID 推得
    let fidBySn = {};       // 媒體 segment sequence-number → 首幀 frame_id
    let liveFragFid = null;     // 當前播放 segment 首幀 frame_id（null=該段無標籤→降級）
    let liveFragNextFid = null; // 下一 segment 首幀 frame_id（段內插值用）
    let liveFragStart = 0;      // 當前 segment 媒體起點（秒）
    let liveFragDur = 0;        // 當前 segment 媒體時長（秒）
    const FPS_HINT = 25;        // 僅在無下一段標籤時的 fallback 段內插值用，非精確
    function parseFragFid(frag) {
      if (!frag || !frag.tagList) return null;
      for (const t of frag.tagList) {
        const s = Array.isArray(t) ? t.join(':') : String(t);
        const m = s.match(/PIG-FRAMEID[:,]?\s*(\d+)/);
        if (m) return parseInt(m[1], 10);
      }
      return null;
    }
```

- [ ] **Step 3: live hls 綁定 LEVEL_LOADED / FRAG_CHANGED**

`static/index.html` 的 `loadStream()` 內 live hls 事件區，於 `hls.on(Hls.Events.LEVEL_LOADED, () => {` 區塊（約 1638-1643）**之前**新增兩個監聽：

```javascript
          hls.on(Hls.Events.LEVEL_LOADED, (_, data) => {
            const frags = (data && data.details && data.details.fragments) || [];
            const map = {};
            for (const f of frags) {
              const fid = parseFragFid(f);
              if (fid != null) map[f.sn] = fid;
            }
            fidBySn = map;
          });

          hls.on(Hls.Events.FRAG_CHANGED, (_, data) => {
            const f = data && data.frag;
            if (!f) return;
            liveFragStart = f.start || 0;
            liveFragDur = f.duration || 0;
            const cur = parseFragFid(f);
            liveFragFid = (cur != null) ? cur
                          : (fidBySn[f.sn] != null ? fidBySn[f.sn] : null);
            const nxt = fidBySn[f.sn + 1];
            liveFragNextFid = (nxt != null) ? nxt : null;
          });
```

（原本既有的 `hls.on(Hls.Events.LEVEL_LOADED, () => { ... setStatus ... })` 區塊保留不動——兩個 LEVEL_LOADED 監聽可並存，hls.js 支援多監聽。）

- [ ] **Step 4: live `drawBoxes` 改 FID 配對（保留 PDT fallback）**

`static/index.html` 的 `drawBoxes` 中 `if (isLive && bboxHistory.length) {` 區塊（約 1431-1455）目前為：

```javascript
      if (isLive && bboxHistory.length) {
        let targetTs = null;
        let dbgSrc = 'latest';
        const pd = hls && hls.playingDate;
        if (pd && !isNaN(pd.getTime())) {
          targetTs = pd.getTime() / 1000 - livePdtOffset;
          dbgSrc = 'PDT';
        } else {
          const latency = (hls && hls.latency != null) ? hls.latency : 0;
          if (latency > 1) { targetTs = Date.now() / 1000 - latency; dbgSrc = 'latency'; }
        }
        let chosenTs = bboxHistory[bboxHistory.length - 1].ts;
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
```

在 `if (isLive && bboxHistory.length) {` 之後、`let targetTs = null;` 之前插入 FID 優先分支；FID 不可用時 `continue` 沿用原 PDT 邏輯。整段替換為：

```javascript
      if (isLive && bboxHistory.length) {
        let targetTs = null;
        let dbgSrc = 'latest';
        let chosenTs = bboxHistory[bboxHistory.length - 1].ts;
        let targetFid = null, chosenFid = null;
        const haveFid = liveFragFid != null &&
          bboxHistory.some(e => typeof e.fid === 'number');
        if (haveFid) {
          const frac = liveFragDur > 0
            ? Math.min(1, Math.max(0, (video.currentTime - liveFragStart) / liveFragDur))
            : 0;
          const span = (liveFragNextFid != null)
            ? (liveFragNextFid - liveFragFid)
            : (liveFragDur * FPS_HINT);
          targetFid = liveFragFid + frac * span;
          let best = bboxHistory[bboxHistory.length - 1];
          let bestDist = Infinity;
          for (const entry of bboxHistory) {
            if (typeof entry.fid !== 'number') continue;
            const d = Math.abs(entry.fid - targetFid);
            if (d < bestDist) { bestDist = d; best = entry; }
          }
          displayBoxes = best.boxes;
          chosenTs = best.ts;
          chosenFid = best.fid;
          dbgSrc = 'FID';
        } else {
          const pd = hls && hls.playingDate;
          if (pd && !isNaN(pd.getTime())) {
            targetTs = pd.getTime() / 1000 - livePdtOffset;
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
        }
```

（緊接其後原本的 `if (window.__bboxDebug) {` 區塊在 Step 5 調整；該區塊與後續 `else { _dbg = null; }`、`}` 結尾保持原有縮排與結構。）

- [ ] **Step 5: HUD `_dbg` 加 FID 欄位**

`static/index.html` 的 `if (window.__bboxDebug) {` 內 `_dbg = { ... }`（約 1458-1468）目前末尾欄位為：

```javascript
            newestTs: bboxHistory[bboxHistory.length - 1].ts,
            histLen: bboxHistory.length,
          };
```

改為：

```javascript
            newestTs: bboxHistory[bboxHistory.length - 1].ts,
            histLen: bboxHistory.length,
            targetFid,
            chosenFid,
            fragFid: liveFragFid,
            fragNextFid: liveFragNextFid,
          };
```

並在 `drawDbgHud` 的 `lines` 陣列（約 1530-1541）`` `src=${d.src}  hist=${d.histLen}`, `` 之後新增一行：

```javascript
        `fid t=${fmt(d.targetFid)} c=${fmt(d.chosenFid)} f0=${fmt(d.fragFid)} f1=${fmt(d.fragNextFid)}`,
```

- [ ] **Step 6: 存在性快檢**

Run:
```bash
grep -c "parseFragFid\|liveFragFid\|fid: data.frame_id\|dbgSrc = 'FID'\|targetFid" static/index.html
```
Expected: 計數 ≥ 5（5 處關鍵改動皆就位）。正式驗證走瀏覽器待測清單。

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): live bbox 改 frame_id 幀身分配對（缺標籤回退 PDT）+ HUD FID 欄位"
```

---

### Task 6: 全套件驗證 + 文件

**Files:**
- 驗證：全測試套件
- Modify: `CLAUDE.md`（gitignore，**不 commit**）

- [ ] **Step 1: 全套件對基線**

Run:
```bash
pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py
```
Expected: 僅既有 5 失敗（`test_config.py::test_default_mot_worker_threads` +
4 `test_stream_router.py` 404），其餘全綠；新增 hls_manager 測試（Task1-3 共
~5 個）皆 PASS；零新增回歸。

- [ ] **Step 2: 基線交叉驗證（如有疑慮）**

若 Step 1 出現上述 5 個以外的紅燈，先確認是否預存在：

```bash
git stash push -- hls_manager.py zmq_receiver.py static/index.html tests/test_hls_manager.py
pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py
git stash pop
```
比對 HEAD 基線失敗集合；僅當「本次新增的紅燈」存在才需修。

- [ ] **Step 3: 更新 `CLAUDE.md`（不 commit）**

在 `CLAUDE.md` 既有「VOD 回放 404 + live bbox 漸進落後」章節之後，新增一節，內容須涵蓋：
- 根因：ffmpeg 媒體時鐘 = 幀數÷TARGET_FPS 與牆鐘脫鉤 → 常數 offset（手動/EMA）只能抵 L、抵不掉 `r·Δt` 斜線（Phase 4.5 架構否決）。
- 解法：`frame_id` 幀身分對應——`hls_manager` 幀計數推算 segment 首幀 frame_id → `#EXT-X-PIG-FRAMEID` 寫進 corrected m3u8；前端 `bboxHistory` 存 fid、`FRAG_CHANGED`/`LEVEL_LOADED` 建 sn→fid、`drawBoxes` 按 fid 配對；缺標籤回退 PDT。
- 範圍：VOD 不動、ffmpeg 不動、無新 endpoint；`live_pdt_offset_seconds` 降為純 fallback（保留不刪）。
- 已知限制：幀計數錨點假設餵入↔輸出近 1:1，速率偏離時有「數幀級、有界、每段自修正」殘差（已非無界斜線）。
- 待測（瀏覽器）：HUD 按 `d`，`src=FID`；live 框貼齊豬隻；長時間（過 ffmpeg 整點 `_restart`）不再漸進落後；thermal / 舊錄影 / 切換 RGB↔Thermal↔回放 降級不崩；commit 清單。

- [ ] **Step 4: 最終 commit（程式碼，不含 CLAUDE.md）**

```bash
git status --porcelain   # 確認無遺漏的 tracked 變更；CLAUDE.md 應為 untracked/ignored
git log --oneline -6
```
Expected: Task1-5 共 5 個 feat commit 在本機 master，無 push。

**瀏覽器待測清單（交付使用者，非本計畫自動化）:**
1. 開 HUD（按 `d`）→ live 應顯示 `src=FID`、`fid t≈c`、`f0/f1` 有值。
2. live 框貼齊豬隻；長時間運行（跨 ffmpeg 整點 `_restart`）不再漸進落後。
3. thermal 串流、舊錄影回放、RGB↔Thermal↔回放↔Live 來回切 → 自動降級，無殘留/錯位/crash。
4. VOD 回放（含今日當前小時）行為與現況一致（本計畫未動 VOD）。

---

## Self-Review

**1. Spec coverage:**
- 單元 1（hls_manager feed/_fed_log/_seg_first_fid/corrected_m3u8/_restart/Manager.feed）→ Task 1-3 ✓
- 單元 2（zmq_receiver 透傳）→ Task 4 ✓
- 單元 3（前端 bboxHistory.fid / FRAG_CHANGED / drawBoxes FID / HUD）→ Task 5 ✓
- 降級矩陣（剛出現/thermal/舊錄影/缺 fid/無標籤）→ Task 5 Step 4 `haveFid` 為偽即走 PDT 分支；後端未知段不寫標籤 → 前端 `liveFragFid=null` 降級 ✓
- 測試策略（feed 帶 frame_id、幀計數錨點、m3u8 標籤、_restart 清空、Manager 透傳、全套件基線）→ Task 1-3、Task 6 ✓
- 範圍邊界（VOD/ffmpeg/endpoint/HybridSORT/per-camera 不做）→ 各 Task 僅改列出檔案，Task 4 明示 thermal 不改、VOD 未列入 ✓
- 提交約束（commit 不 push、CLAUDE.md 不 commit）→ header + Task 6 ✓

**2. Placeholder scan:** 無 TBD/TODO；所有 code step 均含完整程式碼與確切路徑/行號區間；測試步驟含確切指令與預期。

**3. Type/名稱一致性:** `_fed_count: int`、`_fed_log: deque[tuple[int,int]]`、`_seg_first_fid: dict[str,int]`、`_HLS_TIME`、`_FED_LOG_MAX`、`feed(..., frame_id)`、前端 `fidBySn`/`liveFragFid`/`liveFragNextFid`/`liveFragStart`/`liveFragDur`/`parseFragFid`/`FPS_HINT`、HUD `targetFid`/`chosenFid`/`fragFid`/`fragNextFid` —— Task 1-6 全程一致；標籤名 `#EXT-X-PIG-FRAMEID` 後端寫出與前端 `/PIG-FRAMEID[:,]?\s*(\d+)/` 解析一致。

無缺口。
