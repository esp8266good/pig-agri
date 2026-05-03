# Phase 3 — MOT 推論 Implementation Design

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 將 HybridSORT (YOLOX + ReID + HybridSORT) 整合進 FastAPI，以 latest-frame 批次模式執行推論，透過 WebSocket 推送即時 MOT 結果，並在前端 HLS 播放器上疊加 bounding box canvas overlay。

**Architecture:** `InferencePipeline` 作為協調者，持有 latest-frame dict、`BatchDetector`、`ReIDExtractor`、`TrackerPool`。ZMQ receiver 解碼 JPEG 後呼叫 `pipeline.update_frame()`；inference daemon thread 每 100ms 取出所有 camera 最新 frame 組成 GPU batch，結果透過 `asyncio.run_coroutine_threadsafe` 橋接回 FastAPI event loop 廣播 WebSocket。

**Tech Stack:** PyTorch (YOLOX + ReID on GPU)、HybridSORT (CPU ThreadPoolExecutor)、FastAPI WebSocket、`sys.path` 整合 `ref/HybridSORT`（不安裝、不複製）

---

## 1. 檔案結構

**新增：**
```
inference/
  pipeline.py       # InferencePipeline 協調者
  batch_detector.py # YOLOX model wrapper
  reid_extractor.py # FastReID wrapper
  tracker_pool.py   # per-camera Hybrid_Sort_ReID dict + ThreadPoolExecutor
```

**修改：**
```
routers/tracking.py # 改寫：WebSocket endpoint + ConnectionManager
zmq_receiver.py     # 加 JPEG→numpy decode + pipeline.update_frame()
main.py             # lifespan 加 pipeline.start() / pipeline.stop()
static/index.html   # 加 <canvas> overlay + WebSocket JS
```

---

## 2. ref/HybridSORT 整合方式

`inference/batch_detector.py` 模組頂端：

```python
import sys
from pathlib import Path

_REF_DIR = Path(__file__).parent.parent / "ref" / "HybridSORT"
if str(_REF_DIR) not in sys.path:
    sys.path.insert(0, str(_REF_DIR))
```

所有 `ref/HybridSORT` 內的相對路徑（exp 檔、ckpt、fast_reid config、fast_reid weights）皆轉為基於 `_REF_DIR` 的絕對路徑，不使用 `os.chdir`（多執行緒不安全）。

`settings` 中的 `model_config` 值為相對於 `ref/HybridSORT` 的路徑，例如 `"exps/example/mot/yolox_oink_test_hybrid_sort_reid.py"`。

---

## 3. 模組介面

### 3.1 `inference/batch_detector.py`

```python
class BatchDetector:
    def __init__(self, ckpt_path: str, exp_file: str) -> None:
        """
        ckpt_path: 絕對路徑或相對路徑的 .pth.tar
        exp_file:  相對於 ref/HybridSORT 的 exp py 檔路徑
        """

    def infer(self, images: list[np.ndarray]) -> list[np.ndarray | None]:
        """
        輸入: list of BGR numpy (H, W, 3)
        回傳: list of [n, 6] float32 (x1,y1,x2,y2,obj_score,cls_score)
              若某 camera 無偵測結果回傳 None
        實作: 各圖 preproc() → stack batch tensor → 單次 forward → postprocess()
        """
```

初始化流程：
1. `get_exp(abs_exp_file, None)` 取得 exp 物件
2. `exp.get_model().to(device)` + `model.eval()`
3. `torch.load(ckpt_path, map_location="cpu")` → `model.load_state_dict(ckpt["model"])`
4. 啟動時印出 VRAM 用量：`torch.cuda.memory_allocated() / 1e9`

### 3.2 `inference/reid_extractor.py`

```python
class ReIDExtractor:
    def __init__(self, config_file: str, weights_path: str) -> None:
        """
        config_file:  相對於 ref/HybridSORT 的 yml 路徑
        weights_path: 相對於 ref/HybridSORT 的 pth 路徑
        """

    def extract(self, image: np.ndarray, dets: np.ndarray) -> np.ndarray:
        """
        輸入: BGR image (H, W, 3)；dets [n, 4+] bbox_xyxy（原圖座標）
        回傳: [n, 2048] float32 features；dets 為空時回傳 np.zeros((0, 2048))
        """
```

`FastReIDInterface` 初始化需傳入 `config_file` 和 `weights_path` 的絕對路徑。

### 3.3 `inference/tracker_pool.py`

```python
class TrackerPool:
    def __init__(self) -> None:
        # 只持有 per-camera tracker dict，無 executor（executor 由 InferencePipeline 持有）
        self._trackers: dict[str, Hybrid_Sort_ReID] = {}
        self._lock = threading.Lock()

    def update(
        self,
        camera_id: str,
        dets: np.ndarray | None,
        img_info: tuple[int, int],   # (height, width) 原圖尺寸
        id_feature: np.ndarray,
    ) -> list:
        """
        若 camera_id 不存在，lazy-create 新的 Hybrid_Sort_ReID instance。
        dets 為 None 時傳入 np.empty((0, 6))。
        回傳 online_targets: list of [x1, y1, x2, y2, track_id, score]
        此方法為 thread-safe（同一 camera 只有一個 tracker instance，不同 camera 互不干擾）。
        """
```

`_build_tracker_args()` 模組私有函式，回傳 `argparse.Namespace`，從 exp 物件讀取所有追蹤參數：

| 參數 | 來源 | 值 |
|------|------|----|
| `track_thresh` | 預設 | 0.6 |
| `iou_thresh` | exp | 0.15 |
| `use_byte` | exp | True |
| `inertia` | exp | 0.05 |
| `asso` | exp | `"Height_Modulated_IoU"` |
| `deltat` | 預設 | 3 |
| `TCM_first_step` | exp | True |
| `TCM_byte_step` | exp | True |
| `TCM_first_step_weight` | exp | 1.5 |
| `TCM_byte_step_weight` | exp | 1.0 |
| `EG_weight_high_score` | exp | 2.8 |
| `EG_weight_low_score` | exp | 1.4 |
| `low_thresh` | 預設 | 0.1 |
| `high_score_matching_thresh` | 預設 | 0.8 |
| `low_score_matching_thresh` | 預設 | 0.5 |
| `alpha` | 預設 | 0.8 |
| `with_fastreid` | exp | True |
| `fast_reid_config` | exp → 轉絕對路徑 | `<REF_DIR>/fast_reid/configs/...` |
| `fast_reid_weights` | exp → 轉絕對路徑 | `<REF_DIR>/pretrained/model_0054.pth` |
| `with_longterm_reid` | 預設 | False |
| `longterm_reid_weight` | 預設 | 0.0 |
| `longterm_reid_weight_low` | 預設 | 0.0 |
| `with_longterm_reid_correction` | exp | True |
| `longterm_reid_correction_thresh` | exp | 0.20 |
| `longterm_reid_correction_thresh_low` | exp | 1.0 |
| `longterm_bank_length` | 預設 | 30 |
| `adapfs` | 預設 | False |
| `ECC` | 預設 | False |
| `max_id_num` | 預設 | 40 |
| `dataset` | exp | `"OinkTrack"` |
| `hybrid_sort_with_reid` | exp | True |
| `min_box_area` | 預設 | 100 |
| `min_hits` | 預設 | 3 |
| `track_buffer` | 預設 | 30 |

### 3.4 `inference/pipeline.py`

```python
class InferencePipeline:
    LOOP_INTERVAL: float = 0.1  # 10fps target

    def __init__(self) -> None:
        self._latest: dict[str, FrameData] = {}   # camera_id → FrameData
        self._lock = threading.Lock()
        self._detector: BatchDetector | None = None
        self._reid: ReIDExtractor | None = None
        self._tracker_pool: TrackerPool | None = None
        self._executor: ThreadPoolExecutor | None = None   # 並行 tracker updates
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._event_loop: asyncio.AbstractEventLoop | None = None

    def start(self, event_loop: asyncio.AbstractEventLoop) -> None:
        """lifespan 呼叫，傳入 FastAPI 的 event loop 供 WS broadcast 用。
        初始化 BatchDetector、ReIDExtractor、TrackerPool、ThreadPoolExecutor，
        啟動 daemon thread。executor max_workers = settings.mot_worker_threads。"""

    def stop(self) -> None:
        """lifespan 呼叫"""

    def update_frame(
        self, camera_id: str, rgb_np: np.ndarray,
        thermal_np: np.ndarray | None, ts: float
    ) -> None:
        """ZMQReceiver 呼叫（在 ZMQ thread 中執行）"""

    def _loop(self) -> None:
        """daemon thread 主迴圈"""

    def _process_batch(self, snapshot: dict[str, FrameData]) -> None:
        """執行一次完整推論並廣播結果"""
```

`FrameData` 為 `dataclasses.dataclass`：
```python
@dataclass
class FrameData:
    rgb_np: np.ndarray
    thermal_np: np.ndarray | None
    ts: float
    frame_id: int
```

`frame_id` 由 pipeline 自行對每個 camera 計數（`dict[str, int]`）。

`_process_batch` 流程：
1. `batch_imgs = [f.rgb_np for f in snapshot.values()]`
2. `all_dets = detector.infer(batch_imgs)`
3. 對每個 `(camera_id, frame_data, dets)` 並行（`ThreadPoolExecutor.submit`）：
   - `id_feats = reid.extract(frame_data.rgb_np, dets[:, :4])` if dets not None
   - `online_targets = tracker_pool.update(camera_id, dets, img_info, id_feats)`
   - 建立 WebSocket payload
   - `asyncio.run_coroutine_threadsafe(ws_manager.broadcast(camera_id, payload), event_loop)`

ReID 是 GPU 操作，不能多 thread 同時呼叫，必須在主迴圈循序完成。Tracker CPU 工作可並行。正確流程：

```
1. all_dets = detector.infer(batch_imgs)           # GPU, sequential
2. all_id_feats = [reid.extract(img, dets)          # GPU, sequential per camera
                   for img, dets in zip(imgs, all_dets)]
3. futures = [self._executor.submit(               # CPU, parallel (self._executor 屬於 InferencePipeline)
                  tracker_pool.update, cam, dets, info, feats)
              for cam, dets, info, feats in ...]
4. for cam, future in zip(cameras, futures):
       online_targets = future.result()
       asyncio.run_coroutine_threadsafe(broadcast(cam, payload), loop)
```

---

## 4. WebSocket

### 4.1 `routers/tracking.py`

```python
class ConnectionManager:
    def __init__(self) -> None:
        self._connections: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, camera_id: str, ws: WebSocket) -> None:
        await ws.accept()
        async with self._lock:
            self._connections.setdefault(camera_id, set()).add(ws)

    async def disconnect(self, camera_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._connections.get(camera_id, set()).discard(ws)

    async def broadcast(self, camera_id: str, msg: dict) -> None:
        connections = set(self._connections.get(camera_id, set()))
        if not connections:
            return
        data = json.dumps(msg)
        dead: set[WebSocket] = set()
        for ws in connections:
            try:
                await ws.send_text(data)
            except Exception:
                dead.add(ws)
        if dead:
            async with self._lock:
                self._connections.get(camera_id, set()).difference_update(dead)

ws_manager = ConnectionManager()

@router.websocket("/ws/tracking/{camera_id}")
async def ws_tracking(ws: WebSocket, camera_id: str):
    await ws_manager.connect(camera_id, ws)
    try:
        while True:
            await ws.receive_text()   # 保持連線，忽略 client 訊息
    except WebSocketDisconnect:
        await ws_manager.disconnect(camera_id, ws)

@router.get("/tracking/{camera_id}")
async def get_tracking(camera_id: str, ...): ...
```

**路由設定：** `routers/tracking.py` 的 `router = APIRouter()`（**無 prefix**）。`main.py` 以 `app.include_router(tracking_router)` 掛載，完整路徑為 `/ws/tracking/{camera_id}` 和 `/tracking/{camera_id}`。

### 4.2 訊息格式

```json
{
  "frame_id": 42,
  "timestamp": 1714800000.123,
  "objects": [
    { "object_id": 1, "bbox": [120, 80, 60, 90], "confidence": 0.87 }
  ]
}
```

`bbox` 為原始 RGB 圖像座標系（640×480）的 `[x, y, w, h]`（左上角 + 寬高）。

---

## 5. ZMQReceiver 修改

`_process_frame` 增加 decode + pipeline 呼叫：

```python
def _process_frame(self, parts: list) -> None:
    if len(parts) < 4:
        return
    topic = parts[0].decode()
    ts, frame_id = struct.unpack("dQ", parts[1])
    rgb_bytes: bytes = parts[2]
    thermal_bytes: bytes = parts[3]

    rgb_np: np.ndarray | None = None
    thermal_np: np.ndarray | None = None

    if rgb_bytes:
        arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
        rgb_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        hls_mod.hls_manager.feed(topic, "rgb", rgb_bytes)

    if thermal_bytes:
        arr = np.frombuffer(thermal_bytes, dtype=np.uint8)
        thermal_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        hls_mod.hls_manager.feed(topic, "thermal", thermal_bytes)

    if rgb_np is not None:
        inference_pipeline.update_frame(topic, rgb_np, thermal_np, ts)
```

`import cv2` 加到 `zmq_receiver.py` 頂端（`opencv-python-headless` 已在 dependencies）。

---

## 6. `main.py` lifespan 修改

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    loop = asyncio.get_event_loop()
    inference_pipeline.start(loop)
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    inference_pipeline.stop()
    hls_manager.stop_all()
    await database.disconnect()
```

---

## 7. `config.py` 新增欄位

```python
fast_reid_config: str = "fast_reid/configs/CUHKSYSU_DanceTrack/sbs_S50.yml"
fast_reid_weights: str = "pretrained/model_0054.pth"
# 以上兩個路徑皆相對於 ref/HybridSORT
```

`model_config` 已存在（`"exps/example/mot/yolox_oink_test_hybrid_sort_reid.py"`），相對於 `ref/HybridSORT`。

---

## 8. 前端 canvas overlay

`static/index.html` 修改：

**HTML：**
```html
<div id="video-wrap">
  <video id="video" autoplay muted playsinline controls></video>
  <canvas id="overlay"></canvas>
</div>
```

**CSS（新增 canvas 樣式）：**
```css
#video-wrap { position: relative; /* 其餘不變 */ }
#overlay {
  position: absolute;
  top: 0; left: 0;
  width: 100%; height: 100%;
  pointer-events: none;
}
```

**JS：**
```javascript
let ws = null;
let latestBoxes = [];
const ORIG_W = 640, ORIG_H = 480;

function connectWS(cameraId) {
  if (ws) { ws.close(); ws = null; }
  ws = new WebSocket(`ws://${location.host}/ws/tracking/${cameraId}`);
  ws.onmessage = (e) => { latestBoxes = JSON.parse(e.data).objects || []; };
  ws.onclose = () => { latestBoxes = []; };
}

function drawBoxes() {
  const canvas = document.getElementById('overlay');
  const video  = document.getElementById('video');
  canvas.width  = video.offsetWidth;
  canvas.height = video.offsetHeight;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const sx = canvas.width / ORIG_W, sy = canvas.height / ORIG_H;
  ctx.strokeStyle = '#0f0';
  ctx.fillStyle   = '#0f0';
  ctx.lineWidth   = 2;
  ctx.font        = '13px monospace';
  for (const o of latestBoxes) {
    const [x, y, w, h] = o.bbox;
    ctx.strokeRect(x * sx, y * sy, w * sx, h * sy);
    ctx.fillText(`#${o.object_id}`, x * sx + 2, y * sy - 4);
  }
  requestAnimationFrame(drawBoxes);
}
requestAnimationFrame(drawBoxes);
```

`loadStream()` 和 `setType()` 在切換 camera/type 時同步呼叫 `connectWS(currentCamera)`。

---

## 9. 錯誤處理

- **Inference loop 例外：** `try/except Exception` 包住整個 `_process_batch`，`logger.exception` 記錄後繼續下一輪（不停止服務）。
- **單一 camera 失敗：** 若某 camera 的 tracker.update 拋例外，`logger.warning` 後跳過該 camera，不影響其他 camera。
- **WebSocket 死連線：** `broadcast` 時捕捉所有例外，自動從 `_connections` 移除。
- **JPEG decode 失敗：** `cv2.imdecode` 回傳 None，`zmq_receiver._process_frame` 檢查並 skip。

---

## 10. 測試策略

| 測試檔案 | 覆蓋範圍 | GPU mock |
|---------|---------|---------|
| `tests/test_batch_detector.py` | `_make_batch_tensor` shape/dtype；`infer` 呼叫 `postprocess` | mock `model.__call__` |
| `tests/test_reid_extractor.py` | `extract` 空 dets 回傳 zeros；非空 dets 呼叫 `FastReIDInterface.inference` | mock `FastReIDInterface` |
| `tests/test_tracker_pool.py` | lazy-create tracker；`update` 呼叫 `tracker.update`；`stop` 關閉 executor | mock `Hybrid_Sort_ReID` |
| `tests/test_inference_pipeline.py` | `update_frame` 寫入 latest_frames；`_process_batch` 觸發 broadcast | mock detector/reid/tracker_pool/ws_manager |
| `tests/test_ws_tracking.py` | WS connect/disconnect；`broadcast` 發送訊息；死連線自動移除 | `TestClient` WebSocket |

所有測試在無 GPU 環境下執行。HybridSORT 模組（`yolox`、`trackers`）的 import 透過 `sys.path` patch 或直接 mock 相關類別。
