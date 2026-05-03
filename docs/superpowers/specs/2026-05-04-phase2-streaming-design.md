# Phase 2 — 串流設計規格

**日期：** 2026-05-04  
**範圍：** FFmpeg HLS pipeline（RGB + Thermal）+ 最小化 Live 播放器前端

---

## 目標

從 ZMQ receiver 收到的 JPEG frame，透過 FFmpeg subprocess 轉成 HLS `.ts` 片段存到磁碟，前端透過 hls.js 播放 live stream。Phase 2 只做 live 播放，VOD / bbox overlay 留給 Phase 4 / Phase 3。

---

## 決策記錄

| 項目 | 決定 |
|------|------|
| 串流路數 | 每個 camera 各一路 RGB HLS + 一路 Thermal HLS |
| 前端範圍 | 最小化播放器（camera 下拉 + RGB/Thermal 切換 + `<video>`） |
| HLS 設定 | segment 4s，playlist 保留 3 段（穩定優先） |
| FFmpeg 啟動 | On-demand（`GET /stream/{camera_id}/live` 才啟動） |
| 斷線偵測 | Watchdog daemon thread，30s 無 frame 自動終止 FFmpeg |
| FFmpeg 管理 | Sync `subprocess.Popen` + `threading.Lock`（匹配 ZMQ daemon thread 模型） |
| Thermal 解析度 | 保持原始 160×120，不 upscale |

---

## 架構總覽

```
ZMQ daemon thread
  ├─→ hls_manager.feed("cam_01", "rgb",     rgb_bytes)   # if rgb_bytes non-empty
  └─→ hls_manager.feed("cam_01", "thermal", thermal_bytes)  # if thermal_bytes non-empty

HLSManager
  ├── _streams: dict[(camera_id, stream_type) → HLSStream]
  ├── _lock: threading.Lock
  └── _watchdog: daemon thread（每 10s 掃描）

HLSStream（每個 camera × stream_type 一個）
  ├── proc: subprocess.Popen（ffmpeg stdin=PIPE）
  ├── last_feed_time: float
  └── out_dir: Path（YYYY-MM-DD-HH 目錄，整點自動切換）

FastAPI（on-demand 觸發）
  GET /stream/{camera_id}/live?type=rgb|thermal
      → ensure_started → 回傳 m3u8 URL
  GET /stream/hls/{camera_id}/{stream_type}/{date_hour}/{filename}
      → FileResponse
```

---

## HLS 目錄結構

```
{HLS_BASE_DIR}/
└── cam_01/
    ├── rgb/
    │   └── 2026-05-04-14/
    │       ├── index.m3u8
    │       ├── seg_000.ts
    │       └── seg_001.ts
    └── thermal/
        └── 2026-05-04-14/
            ├── index.m3u8
            └── ...
```

整點（`YYYY-MM-DD-HH`）切換時，`HLSStream.feed()` 偵測到小時改變，重啟 FFmpeg 到新目錄。

---

## `hls_manager.py` 設計

### `HLSStream`

```python
@dataclass
class HLSStream:
    camera_id: str
    stream_type: str          # "rgb" | "thermal"
    proc: subprocess.Popen
    out_dir: Path
    last_feed_time: float
    _lock: threading.Lock

    def feed(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            # 若整點已切換 → stop + 重啟到新目錄
            self.last_feed_time = time.time()
            self.proc.stdin.write(jpeg_bytes)
            self.proc.stdin.flush()

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        self.proc.wait(timeout=3)
```

### `HLSManager`

```python
class HLSManager:
    NO_FRAME_TIMEOUT = 30   # seconds
    WATCHDOG_INTERVAL = 10  # seconds

    def ensure_started(self, camera_id: str, stream_type: str) -> Path:
        """On-demand：若尚未啟動則建立 HLSStream + 啟動 FFmpeg，回傳當前 out_dir。"""

    def feed(self, camera_id: str, stream_type: str, jpeg_bytes: bytes) -> None:
        """ZMQ thread 呼叫；若 stream 未啟動（尚未有前端請求）則 no-op。"""

    def stop_all(self) -> None:
        """lifespan shutdown 呼叫，終止所有 FFmpeg process。"""

    def _watchdog_loop(self) -> None:
        """Daemon thread：每 WATCHDOG_INTERVAL 秒掃描，逾時者 stop + 移除。"""
```

### FFmpeg 指令

**RGB（640×480，已是 4:3）：**
```
ffmpeg -y -f mjpeg -i pipe:0
  -c:v libx264 -preset ultrafast -tune zerolatency
  -hls_time 4 -hls_list_size 3 -hls_flags delete_segments+append_list
  -hls_segment_filename "{out_dir}/seg_%03d.ts"
  "{out_dir}/index.m3u8"
```

**Thermal（160×120，原始尺寸）：**
同上，無額外 `-vf` 參數。

---

## 修改現有檔案

### `zmq_receiver.py`

在 `logger.info(...)` 之後加入：

```python
from hls_manager import hls_manager

if rgb_bytes:
    hls_manager.feed(topic, "rgb", rgb_bytes)
if thermal_bytes:
    hls_manager.feed(topic, "thermal", thermal_bytes)
```

### `routers/stream.py`

```
GET /cameras
    → {"cameras": ["cam_01", "cam_02", ...]}  # 來自 settings.camera_topics

GET /stream/{camera_id}/live?type=rgb|thermal
    # FastAPI 參數：stream_type: str = Query("rgb", alias="type")
    # （type 是 Python builtin，不能直接用作參數名）
    1. hls_manager.ensure_started(camera_id, stream_type)
    2. 回傳 {"url": "/stream/hls/{camera_id}/{stream_type}/{YYYY-MM-DD-HH}/index.m3u8"}

GET /stream/hls/{camera_id}/{stream_type}/{date_hour}/{filename}
    → FileResponse( HLS_BASE_DIR / camera_id / stream_type / date_hour / filename )
    → 若路徑不存在 → 404

GET /stream/{camera_id}/vod?start=&end=
    → {"status": "not implemented"}  # Phase 4
```

### `main.py`

```python
from hls_manager import hls_manager

# lifespan shutdown：
hls_manager.stop_all()
```

---

## 前端：`static/index.html`

最小化播放器，FastAPI 掛載 `StaticFiles("/static")` 並將 `/` redirect 至 `/static/index.html`。

```
┌─────────────────────────────────────┐
│  豬隻監測 Live                       │
│  Camera: [cam_01 ▼]  [RGB] [Thermal]│
│  ┌───────────────────────────────┐  │
│  │         <video>               │  │
│  │         hls.js 播放           │  │
│  └───────────────────────────────┘  │
│  狀態：● 連線中 / ○ 等待 stream     │
└─────────────────────────────────────┘
```

- Camera 清單：呼叫 `GET /cameras` 取得（`settings.camera_topics`，非 DB 設定）
- 切換 camera 或 RGB/Thermal → 呼叫 `GET /stream/{camera_id}/live?type=...` → 更新 hls.js source
- hls.js 從 CDN 載入（`https://cdn.jsdelivr.net/npm/hls.js@latest`）

---

## 測試計畫

### `tests/test_hls_manager.py`（新增）

| 測試 | 做法 |
|------|------|
| `ensure_started` 建立 stream + 啟動 FFmpeg | mock `subprocess.Popen`，檢查 cmd 參數含 `-hls_time 4` |
| `ensure_started` 重複呼叫不重複啟動 | 驗證 Popen 只呼叫一次 |
| `feed` 已啟動時寫入 stdin | mock proc，驗證 `stdin.write` 被呼叫 |
| `feed` 未啟動時 no-op | 確認無 Popen 呼叫 |
| watchdog 逾時終止 | 設 `last_feed_time` 為過去 60s，觸發 `_watchdog_loop`，驗證 `proc.terminate` |
| `stop_all` 清理全部 | 驗證所有 proc 都被 `terminate` |

### `tests/test_stream_router.py`（新增）

| 測試 | 做法 |
|------|------|
| `GET /cameras` 回傳 camera 清單 | 驗證回傳 `{"cameras": [...]}` 且含所有 topics |
| `GET /stream/cam_01/live` 回傳正確 URL | mock `hls_manager.ensure_started`，驗證 URL 格式 |
| `GET /stream/hls/...` 檔案存在 → 200 | `tmp_path` 建立假 .ts 檔 |
| `GET /stream/hls/...` 檔案不存在 → 404 | 不建立檔案 |

### 更新 `tests/test_main.py`

- `/stream/cam_01/live` stub 測試改為驗證真實回傳格式（Phase 2 實作後 stub 移除）

---

## 新增 Python 依賴

Phase 2 不需要新增 PyPI 套件（FFmpeg 是系統指令，`subprocess` 是標準庫）。

---

## 優雅關閉順序（lifespan shutdown）

```
zmq_receiver.stop()      # 停止新 frame 進入
hls_manager.stop_all()   # 終止所有 FFmpeg processes
database.disconnect()    # 關閉 DB pool
```
