# Phase 3 — MOT 推論 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 將 YOLOX + FastReID + HybridSORT 整合進 FastAPI，以 latest-frame 批次模式推論，WebSocket 推送結果，前端 HLS 播放器疊加 bounding box canvas overlay。

**Architecture:** `InferencePipeline`（`inference/pipeline.py`）作為協調者，持有 latest-frame dict、`BatchDetector`、`ReIDExtractor`、`TrackerPool`、`ThreadPoolExecutor`。ZMQ receiver decode JPEG 後呼叫 `pipeline.update_frame()`；inference daemon thread 每 100ms 取出所有 camera 最新 frame → GPU batch YOLOX → GPU ReID → CPU TrackerPool → `asyncio.run_coroutine_threadsafe` 橋接 WebSocket broadcast。`ref/HybridSORT` 目錄透過 `inference/__init__.py` 的 `sys.path.insert` 整合，不安裝、不複製程式碼。

**Tech Stack:** PyTorch（YOLOX + FastReID on GPU）、HybridSORT（CPU ThreadPoolExecutor）、FastAPI WebSocket、hls.js canvas overlay

---

## 檔案一覽

| 動作 | 路徑 | 負責 |
|------|------|------|
| 修改 | `config.py` | 新增 `fast_reid_config`、`fast_reid_weights` 欄位 |
| 修改 | `inference/__init__.py` | `sys.path` 加入 `ref/HybridSORT` |
| 新增 | `inference/batch_detector.py` | YOLOX 推論 wrapper |
| 新增 | `inference/reid_extractor.py` | FastReID wrapper |
| 新增 | `inference/tracker_pool.py` | per-camera HybridSORT dict |
| 新增 | `inference/pipeline.py` | `InferencePipeline` 協調者 |
| 改寫 | `routers/tracking.py` | `ConnectionManager` + WebSocket endpoint |
| 修改 | `zmq_receiver.py` | JPEG decode + `pipeline.update_frame()` |
| 修改 | `main.py` | lifespan 加 pipeline start/stop |
| 修改 | `static/index.html` | canvas overlay + WebSocket JS |
| 新增 | `tests/test_batch_detector.py` | BatchDetector 單元測試 |
| 新增 | `tests/test_reid_extractor.py` | ReIDExtractor 單元測試 |
| 新增 | `tests/test_tracker_pool.py` | TrackerPool 單元測試 |
| 新增 | `tests/test_inference_pipeline.py` | InferencePipeline 單元測試 |
| 新增 | `tests/test_ws_tracking.py` | WebSocket endpoint 測試 |
| 修改 | `tests/test_zmq_receiver.py` | 加 pipeline 呼叫的測試 |
| 修改 | `tests/test_main.py` | fixture 加 pipeline mock |

---

## 背景知識

執行這份計畫前需要了解的事：

**HybridSORT 不是 pip 套件。** 它在 `ref/HybridSORT/` 目錄，以 `sys.path` 整合。`yolox`、`trackers`、`fast_reid` 等 import 都來自那裡。

**YOLOX 推論流程（來自 `ref/HybridSORT/run_realtime_track.py`）：**
1. `preproc(img, test_size, rgb_means, std)` → `(tensor_np, ratio, raw_img)`
2. `model(batch_tensor)` → raw outputs
3. `postprocess(outputs, num_classes, conf_thre, nms_thre)` → `list[Tensor([n, 7]) | None]`
   - 每個 tensor 格式：`[x1, y1, x2, y2, obj_score, cls_score, cls_pred]`，座標在 **model space（test_size=736×1280）**
4. tracker.update 接收 model space dets，自己做 `bboxes /= scale` 換回原圖座標

**ReID 需要原圖座標：** `bbox_orig = dets[:, :4] / scale`，其中 `scale = min(test_size[0]/img_h, test_size[1]/img_w)`

**`Hybrid_Sort_ReID.update` 完整簽名：**
```python
tracker.update(output_results, img_info, img_size, id_feature=None)
# output_results: [n, 6+] numpy，model space
# img_info:       [height, width]（原圖）
# img_size:       exp.test_size = (736, 1280)
# id_feature:     [n, 2048] numpy
```

**路徑慣例：** `config.py` 中的 `model_weights`、`model_config_path` 是相對專案根目錄的路徑（以 `./` 開頭）。新增的 `fast_reid_config`、`fast_reid_weights` 也採相同慣例。

---

## Task 1: config.py — 新增 fast_reid 設定欄位

**Files:**
- Modify: `config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: 在 `tests/test_config.py` 新增測試**

開啟 `tests/test_config.py`（已存在），在最後加上：

```python
def test_settings_has_fast_reid_config():
    from config import settings
    assert hasattr(settings, "fast_reid_config")
    assert "fast_reid" in settings.fast_reid_config

def test_settings_has_fast_reid_weights():
    from config import settings
    assert hasattr(settings, "fast_reid_weights")
    assert ".pth" in settings.fast_reid_weights
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_config.py::test_settings_has_fast_reid_config tests/test_config.py::test_settings_has_fast_reid_weights -v
```

Expected: FAIL with `AttributeError`

- [ ] **Step 3: 在 `config.py` 的 `Settings` class 新增兩個欄位**

在 `model_config_path` 欄位後方加入（`Settings` class 內）：

```python
    fast_reid_config: str = (
        "ref/HybridSORT/fast_reid/configs/CUHKSYSU_DanceTrack/sbs_S50.yml"
    )
    fast_reid_weights: str = "ref/HybridSORT/pretrained/model_0054.pth"
```

- [ ] **Step 4: 確認測試通過**

```bash
pytest tests/test_config.py -v
```

Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add fast_reid_config and fast_reid_weights to settings"
```

---

## Task 2: inference/__init__.py — sys.path 整合

**Files:**
- Modify: `inference/__init__.py`

- [ ] **Step 1: 取代 `inference/__init__.py` 全部內容**

```python
import sys
from pathlib import Path

_REF_DIR = Path(__file__).parent.parent / "ref" / "HybridSORT"
if str(_REF_DIR) not in sys.path:
    sys.path.insert(0, str(_REF_DIR))
```

- [ ] **Step 2: 確認現有測試不受影響**

```bash
pytest tests/ -v
```

Expected: 所有既有測試仍 PASS（此步驟不加新測試，sys.path 效果由後續模組 import 驗證）

- [ ] **Step 3: Commit**

```bash
git add inference/__init__.py
git commit -m "feat: add ref/HybridSORT to sys.path via inference/__init__"
```

---

## Task 3: inference/batch_detector.py — YOLOX wrapper

**Files:**
- Create: `inference/batch_detector.py`
- Create: `tests/test_batch_detector.py`

- [ ] **Step 1: 建立 `tests/test_batch_detector.py` 並寫第一個測試**

```python
import sys
import numpy as np
from unittest.mock import MagicMock, patch

# Mock HybridSORT modules（無法在無 GPU / 未編譯環境中 import）
for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_detector():
    """建立 BatchDetector instance，跳過 __init__（不需要真實模型）"""
    from inference.batch_detector import BatchDetector
    d = BatchDetector.__new__(BatchDetector)
    d._model = MagicMock()
    d._test_size = (736, 1280)
    d._num_classes = 1
    d._confthre = 0.1
    d._nmsthre = 0.7
    d._device = "cpu"
    return d


def test_infer_empty_list_returns_empty():
    d = _make_detector()
    assert d.infer([]) == []


def test_test_size_property():
    d = _make_detector()
    assert d.test_size == (736, 1280)
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_batch_detector.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'inference.batch_detector'`

- [ ] **Step 3: 建立 `inference/batch_detector.py`**

```python
import numpy as np
import torch

import inference  # triggers sys.path setup
from yolox.data.data_augment import preproc
from yolox.exp import get_exp
from yolox.utils import postprocess

from config import settings
from pathlib import Path
from loguru import logger

_PROJECT_ROOT = Path(__file__).parent.parent
_RGB_MEANS = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


class BatchDetector:
    def __init__(self, ckpt_path: str, exp_file: str) -> None:
        abs_exp = str((_PROJECT_ROOT / exp_file).resolve())
        exp = get_exp(abs_exp, None)
        self._test_size: tuple[int, int] = exp.test_size
        self._num_classes: int = exp.num_classes
        self._confthre: float = exp.test_conf
        self._nmsthre: float = exp.nmsthre
        self._device = torch.device(settings.device if torch.cuda.is_available() else "cpu")

        model = exp.get_model().to(self._device)
        model.eval()
        abs_ckpt = str((_PROJECT_ROOT / ckpt_path).resolve())
        ckpt = torch.load(abs_ckpt, map_location="cpu")
        model.load_state_dict(ckpt["model"])
        self._model = model

        vram_gb = torch.cuda.memory_allocated() / 1e9 if torch.cuda.is_available() else 0.0
        logger.info(f"BatchDetector ready on {self._device}, VRAM used: {vram_gb:.2f} GB")

    @property
    def test_size(self) -> tuple[int, int]:
        return self._test_size

    def infer(self, images: list[np.ndarray]) -> list[np.ndarray | None]:
        if not images:
            return []
        batch_tensors = []
        for img in images:
            t, _ratio, _raw = preproc(img, self._test_size, _RGB_MEANS, _STD)
            batch_tensors.append(t)
        batch = torch.from_numpy(np.stack(batch_tensors)).float().to(self._device)
        with torch.no_grad():
            raw = self._model(batch)
        outputs = postprocess(raw, self._num_classes, self._confthre, self._nmsthre)
        results: list[np.ndarray | None] = []
        for out in outputs:
            results.append(None if out is None else out.cpu().numpy())
        return results
```

- [ ] **Step 4: 補上更多測試**

在 `tests/test_batch_detector.py` 末尾加入：

```python
def test_infer_returns_none_when_no_detections():
    import inference.batch_detector as bd_mod
    d = _make_detector()
    fake_img = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch.object(bd_mod, "preproc", return_value=(np.zeros((3, 736, 1280)), 0.5, None)), \
         patch.object(bd_mod, "postprocess", return_value=[None]), \
         patch("torch.no_grad"):
        d._model.return_value = MagicMock()
        result = d.infer([fake_img])
    assert result == [None]


def test_infer_returns_ndarray_when_detections_exist():
    import inference.batch_detector as bd_mod
    d = _make_detector()
    fake_img = np.zeros((480, 640, 3), dtype=np.uint8)
    fake_dets = MagicMock()
    fake_dets.cpu.return_value.numpy.return_value = np.ones((3, 7), dtype=np.float32)
    with patch.object(bd_mod, "preproc", return_value=(np.zeros((3, 736, 1280)), 0.5, None)), \
         patch.object(bd_mod, "postprocess", return_value=[fake_dets]), \
         patch("torch.no_grad"):
        d._model.return_value = MagicMock()
        result = d.infer([fake_img])
    assert result[0].shape == (3, 7)
```

- [ ] **Step 5: 確認所有測試通過**

```bash
pytest tests/test_batch_detector.py -v
```

Expected: 4 tests PASS

- [ ] **Step 6: Commit**

```bash
git add inference/batch_detector.py tests/test_batch_detector.py
git commit -m "feat: add BatchDetector YOLOX wrapper"
```

---

## Task 4: inference/reid_extractor.py — FastReID wrapper

**Files:**
- Create: `inference/reid_extractor.py`
- Create: `tests/test_reid_extractor.py`

- [ ] **Step 1: 建立 `tests/test_reid_extractor.py`**

```python
import sys
import numpy as np
from unittest.mock import MagicMock, patch

for _mod in [
    "fast_reid", "fast_reid.fast_reid_interfece",
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_extractor(mock_encoder=None):
    from inference.reid_extractor import ReIDExtractor
    e = ReIDExtractor.__new__(ReIDExtractor)
    e._encoder = mock_encoder or MagicMock()
    return e


def test_extract_empty_dets_returns_zeros():
    e = _make_extractor()
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = e.extract(img, np.zeros((0, 4), dtype=np.float32))
    assert result.shape == (0, 2048)
    e._encoder.inference.assert_not_called()


def test_extract_calls_encoder_with_dets():
    mock_enc = MagicMock()
    mock_enc.inference.return_value = np.ones((2, 2048), dtype=np.float32)
    e = _make_extractor(mock_enc)
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    dets = np.array([[10, 20, 50, 80], [100, 150, 200, 250]], dtype=np.float32)
    result = e.extract(img, dets)
    mock_enc.inference.assert_called_once()
    assert result.shape == (2, 2048)


def test_extract_none_dets_returns_zeros():
    e = _make_extractor()
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = e.extract(img, None)
    assert result.shape == (0, 2048)
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_reid_extractor.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 建立 `inference/reid_extractor.py`**

```python
import numpy as np
from pathlib import Path
from loguru import logger

import inference  # triggers sys.path setup
from fast_reid.fast_reid_interfece import FastReIDInterface

from config import settings

_PROJECT_ROOT = Path(__file__).parent.parent


class ReIDExtractor:
    def __init__(self, config_file: str, weights_path: str) -> None:
        abs_cfg = str((_PROJECT_ROOT / config_file).resolve())
        abs_wts = str((_PROJECT_ROOT / weights_path).resolve())
        self._encoder = FastReIDInterface(abs_cfg, abs_wts, "cuda")
        logger.info("ReIDExtractor ready")

    def extract(self, image: np.ndarray, dets: np.ndarray | None) -> np.ndarray:
        if dets is None or len(dets) == 0:
            return np.zeros((0, 2048), dtype=np.float32)
        bbox_xyxy = dets[:, :4].astype(np.float32)
        return self._encoder.inference(image, bbox_xyxy)
```

- [ ] **Step 4: 確認所有測試通過**

```bash
pytest tests/test_reid_extractor.py -v
```

Expected: 3 tests PASS

- [ ] **Step 5: Commit**

```bash
git add inference/reid_extractor.py tests/test_reid_extractor.py
git commit -m "feat: add ReIDExtractor FastReID wrapper"
```

---

## Task 5: inference/tracker_pool.py — TrackerPool

**Files:**
- Create: `inference/tracker_pool.py`
- Create: `tests/test_tracker_pool.py`

- [ ] **Step 1: 建立 `tests/test_tracker_pool.py`**

```python
import sys
import threading
import argparse
import numpy as np
from unittest.mock import MagicMock, patch

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_pool():
    from inference.tracker_pool import TrackerPool
    pool = TrackerPool.__new__(TrackerPool)
    pool._trackers = {}
    pool._lock = threading.Lock()
    args = argparse.Namespace()
    args.track_thresh = 0.6
    args.iou_thresh = 0.15
    args.asso = "iou"
    args.deltat = 3
    args.inertia = 0.2
    args.use_byte = False
    args.ECC = False
    args.low_thresh = 0.1
    args.high_score_matching_thresh = 0.8
    args.low_score_matching_thresh = 0.5
    args.alpha = 0.8
    args.with_fastreid = False
    args.fast_reid_config = ""
    args.fast_reid_weights = ""
    args.with_longterm_reid = False
    args.longterm_reid_weight = 0.0
    args.longterm_reid_weight_low = 0.0
    args.with_longterm_reid_correction = False
    args.longterm_reid_correction_thresh = 1.0
    args.longterm_reid_correction_thresh_low = 1.0
    args.longterm_bank_length = 30
    args.adapfs = False
    args.max_id_num = 40
    args.dataset = "test"
    args.hybrid_sort_with_reid = False
    args.min_box_area = 100
    args.min_hits = 3
    args.track_buffer = 30
    args.EG_weight_high_score = 0.0
    args.EG_weight_low_score = 0.0
    args.TCM_first_step = False
    args.TCM_byte_step = False
    args.TCM_first_step_weight = 1.0
    args.TCM_byte_step_weight = 1.0
    pool._args = args
    return pool


def test_update_lazy_creates_tracker_on_first_call():
    from inference.tracker_pool import TrackerPool
    pool = _make_pool()
    mock_tracker = MagicMock()
    mock_tracker.update.return_value = []
    dets = np.empty((0, 6), dtype=np.float32)
    id_feat = np.zeros((0, 2048), dtype=np.float32)

    with patch("inference.tracker_pool.Hybrid_Sort_ReID", return_value=mock_tracker):
        pool.update("cam_01", dets, (480, 640), (736, 1280), id_feat)

    assert "cam_01" in pool._trackers
    mock_tracker.update.assert_called_once()


def test_update_reuses_existing_tracker():
    from inference.tracker_pool import TrackerPool
    pool = _make_pool()
    mock_tracker = MagicMock()
    mock_tracker.update.return_value = []
    pool._trackers["cam_01"] = mock_tracker
    dets = np.empty((0, 6), dtype=np.float32)
    id_feat = np.zeros((0, 2048), dtype=np.float32)

    pool.update("cam_01", dets, (480, 640), (736, 1280), id_feat)

    assert mock_tracker.update.call_count == 1


def test_update_passes_empty_dets_when_none():
    from inference.tracker_pool import TrackerPool
    pool = _make_pool()
    mock_tracker = MagicMock()
    mock_tracker.update.return_value = []
    pool._trackers["cam_01"] = mock_tracker

    pool.update("cam_01", None, (480, 640), (736, 1280), np.zeros((0, 2048)))

    call_args = mock_tracker.update.call_args
    dets_passed = call_args[0][0]
    assert dets_passed.shape == (0, 6)


def test_update_returns_online_targets():
    pool = _make_pool()
    mock_tracker = MagicMock()
    mock_tracker.update.return_value = [[10, 20, 50, 80, 1, 0.9]]
    pool._trackers["cam_01"] = mock_tracker
    dets = np.ones((1, 6), dtype=np.float32)
    id_feat = np.ones((1, 2048), dtype=np.float32)

    result = pool.update("cam_01", dets, (480, 640), (736, 1280), id_feat)
    assert result == [[10, 20, 50, 80, 1, 0.9]]
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_tracker_pool.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 建立 `inference/tracker_pool.py`**

```python
import argparse
import threading
import numpy as np
from pathlib import Path
from loguru import logger

import inference  # triggers sys.path setup
from trackers.hybrid_sort_tracker.hybrid_sort_reid import Hybrid_Sort_ReID
from yolox.exp import get_exp

from config import settings

_PROJECT_ROOT = Path(__file__).parent.parent


def _build_tracker_args() -> argparse.Namespace:
    abs_exp = str((_PROJECT_ROOT / settings.model_config_path).resolve())
    exp = get_exp(abs_exp, None)
    args = argparse.Namespace()
    args.track_thresh = 0.6
    args.iou_thresh = exp.iou_thresh
    args.use_byte = exp.use_byte
    args.inertia = exp.inertia
    args.asso = exp.asso
    args.deltat = 3
    args.min_hits = 3
    args.track_buffer = 30
    args.TCM_first_step = exp.TCM_first_step
    args.TCM_byte_step = exp.TCM_byte_step
    args.TCM_first_step_weight = exp.TCM_first_step_weight
    args.TCM_byte_step_weight = exp.TCM_byte_step_weight
    args.EG_weight_high_score = exp.EG_weight_high_score
    args.EG_weight_low_score = exp.EG_weight_low_score
    args.low_thresh = 0.1
    args.high_score_matching_thresh = 0.8
    args.low_score_matching_thresh = 0.5
    args.alpha = 0.8
    args.with_fastreid = exp.with_fastreid
    args.fast_reid_config = str((_PROJECT_ROOT / settings.fast_reid_config).resolve())
    args.fast_reid_weights = str((_PROJECT_ROOT / settings.fast_reid_weights).resolve())
    args.with_longterm_reid = False
    args.longterm_reid_weight = 0.0
    args.longterm_reid_weight_low = 0.0
    args.with_longterm_reid_correction = exp.with_longterm_reid_correction
    args.longterm_reid_correction_thresh = exp.longterm_reid_correction_thresh
    args.longterm_reid_correction_thresh_low = exp.longterm_reid_correction_thresh_low
    args.longterm_bank_length = 30
    args.adapfs = False
    args.ECC = False
    args.max_id_num = 40
    args.dataset = exp.dataset
    args.hybrid_sort_with_reid = exp.hybrid_sort_with_reid
    args.min_box_area = 100
    return args


class TrackerPool:
    def __init__(self) -> None:
        self._trackers: dict[str, Hybrid_Sort_ReID] = {}
        self._lock = threading.Lock()
        self._args = _build_tracker_args()
        logger.info("TrackerPool initialised")

    def update(
        self,
        camera_id: str,
        dets: np.ndarray | None,
        img_info: tuple[int, int],
        img_size: tuple[int, int],
        id_feature: np.ndarray,
    ) -> list:
        if dets is None:
            dets = np.empty((0, 6), dtype=np.float32)
            id_feature = np.zeros((0, 2048), dtype=np.float32)

        with self._lock:
            if camera_id not in self._trackers:
                self._trackers[camera_id] = Hybrid_Sort_ReID(
                    self._args,
                    det_thresh=self._args.track_thresh,
                    iou_threshold=self._args.iou_thresh,
                    asso_func=self._args.asso,
                    delta_t=self._args.deltat,
                    inertia=self._args.inertia,
                )
                logger.info(f"Created tracker for {camera_id}")
            tracker = self._trackers[camera_id]

        return tracker.update(dets, list(img_info), img_size, id_feature=id_feature)
```

- [ ] **Step 4: 確認所有測試通過**

```bash
pytest tests/test_tracker_pool.py -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add inference/tracker_pool.py tests/test_tracker_pool.py
git commit -m "feat: add TrackerPool with lazy per-camera HybridSORT"
```

---

## Task 6: inference/pipeline.py — InferencePipeline

**Files:**
- Create: `inference/pipeline.py`
- Create: `tests/test_inference_pipeline.py`

- [ ] **Step 1: 建立 `tests/test_inference_pipeline.py`**

```python
import sys
import asyncio
import threading
import numpy as np
from unittest.mock import MagicMock, AsyncMock, patch

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_pipeline():
    from inference.pipeline import InferencePipeline
    p = InferencePipeline.__new__(InferencePipeline)
    p._latest = {}
    p._frame_counts = {}
    p._lock = threading.Lock()
    p._detector = None
    p._reid = None
    p._tracker_pool = None
    p._executor = None
    p._loop_thread = None
    p._running = False
    p._event_loop = None
    p._broadcast_fn = None
    return p


def test_update_frame_stores_latest():
    from inference.pipeline import FrameData
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0)
    assert "cam_01" in p._latest
    assert p._latest["cam_01"].rgb_np is rgb


def test_update_frame_increments_frame_count():
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0)
    p.update_frame("cam_01", rgb, None, 2.0)
    assert p._frame_counts["cam_01"] == 2


def test_process_batch_calls_broadcast():
    from inference.pipeline import FrameData, InferencePipeline
    p = _make_pipeline()

    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [np.ones((2, 7), dtype=np.float32)]

    mock_reid = MagicMock()
    mock_reid.extract.return_value = np.ones((2, 2048), dtype=np.float32)

    mock_tracker_pool = MagicMock()
    mock_tracker_pool.update.return_value = [
        [10.0, 20.0, 50.0, 80.0, 1, 0.9]
    ]

    broadcast_calls = []

    async def mock_broadcast(camera_id, msg):
        broadcast_calls.append((camera_id, msg))

    p._detector = mock_detector
    p._reid = mock_reid
    p._tracker_pool = mock_tracker_pool
    p._broadcast_fn = mock_broadcast

    loop = asyncio.new_event_loop()
    p._event_loop = loop

    from concurrent.futures import ThreadPoolExecutor
    p._executor = ThreadPoolExecutor(max_workers=2)

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    snapshot = {"cam_01": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1)}

    # run _process_batch and let the broadcast future complete
    p._process_batch(snapshot)
    loop.run_until_complete(asyncio.sleep(0.05))
    loop.close()
    p._executor.shutdown(wait=False)

    assert len(broadcast_calls) == 1
    cam, msg = broadcast_calls[0]
    assert cam == "cam_01"
    assert "objects" in msg
    assert msg["objects"][0]["object_id"] == 1


def test_process_batch_skips_on_exception():
    from inference.pipeline import FrameData
    p = _make_pipeline()
    mock_detector = MagicMock()
    mock_detector.infer.side_effect = RuntimeError("GPU error")
    mock_detector.test_size = (736, 1280)
    p._detector = mock_detector
    p._reid = MagicMock()
    p._tracker_pool = MagicMock()
    p._broadcast_fn = AsyncMock()
    p._event_loop = asyncio.new_event_loop()

    from concurrent.futures import ThreadPoolExecutor
    p._executor = ThreadPoolExecutor(max_workers=1)

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    snapshot = {"cam_01": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1)}

    # should not raise
    p._process_batch(snapshot)
    p._event_loop.close()
    p._executor.shutdown(wait=False)
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_inference_pipeline.py -v
```

Expected: FAIL with `ModuleNotFoundError`

- [ ] **Step 3: 建立 `inference/pipeline.py`**

```python
import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable, Coroutine

import numpy as np
from loguru import logger

import inference  # triggers sys.path setup


@dataclass
class FrameData:
    rgb_np: np.ndarray
    thermal_np: np.ndarray | None
    ts: float
    frame_id: int


class InferencePipeline:
    LOOP_INTERVAL: float = 0.1

    def __init__(self) -> None:
        self._latest: dict[str, FrameData] = {}
        self._frame_counts: dict[str, int] = {}
        self._lock = threading.Lock()
        self._detector = None
        self._reid = None
        self._tracker_pool = None
        self._executor: ThreadPoolExecutor | None = None
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_fn: Callable | None = None

    def start(
        self,
        event_loop: asyncio.AbstractEventLoop,
        broadcast_fn: Callable | None = None,
    ) -> None:
        from inference.batch_detector import BatchDetector
        from inference.reid_extractor import ReIDExtractor
        from inference.tracker_pool import TrackerPool
        from config import settings

        self._detector = BatchDetector(settings.model_weights, settings.model_config_path)
        self._reid = ReIDExtractor(settings.fast_reid_config, settings.fast_reid_weights)
        self._tracker_pool = TrackerPool()
        self._executor = ThreadPoolExecutor(max_workers=settings.mot_worker_threads)
        self._event_loop = event_loop

        if broadcast_fn is not None:
            self._broadcast_fn = broadcast_fn
        else:
            from routers.tracking import ws_manager
            self._broadcast_fn = ws_manager.broadcast

        self._running = True
        self._loop_thread = threading.Thread(
            target=self._loop, daemon=True, name="inference-loop"
        )
        self._loop_thread.start()
        logger.info("InferencePipeline started")

    def stop(self) -> None:
        self._running = False
        if self._loop_thread:
            self._loop_thread.join(timeout=5)
        if self._executor:
            self._executor.shutdown(wait=False)
        if self._tracker_pool:
            pass  # TrackerPool has no blocking shutdown
        logger.info("InferencePipeline stopped")

    def update_frame(
        self,
        camera_id: str,
        rgb_np: np.ndarray,
        thermal_np: np.ndarray | None,
        ts: float,
    ) -> None:
        with self._lock:
            count = self._frame_counts.get(camera_id, 0) + 1
            self._frame_counts[camera_id] = count
            self._latest[camera_id] = FrameData(
                rgb_np=rgb_np, thermal_np=thermal_np, ts=ts, frame_id=count
            )

    def _loop(self) -> None:
        while self._running:
            time.sleep(self.LOOP_INTERVAL)
            with self._lock:
                snapshot = dict(self._latest)
            if not snapshot:
                continue
            self._process_batch(snapshot)

    def _process_batch(self, snapshot: dict[str, FrameData]) -> None:
        try:
            cameras = list(snapshot.keys())
            frames = [snapshot[c] for c in cameras]
            batch_imgs = [f.rgb_np for f in frames]

            all_dets = self._detector.infer(batch_imgs)
            test_size = self._detector.test_size

            # ReID: GPU sequential
            all_id_feats: list[np.ndarray] = []
            for frame_data, dets in zip(frames, all_dets):
                if dets is None or len(dets) == 0:
                    all_id_feats.append(np.zeros((0, 2048), dtype=np.float32))
                else:
                    h, w = frame_data.rgb_np.shape[:2]
                    scale = min(test_size[0] / h, test_size[1] / w)
                    bbox_orig = (dets[:, :4] / scale).astype(np.float32)
                    all_id_feats.append(self._reid.extract(frame_data.rgb_np, bbox_orig))

            # Tracker: CPU parallel
            futures = []
            for cam, frame_data, dets, id_feats in zip(cameras, frames, all_dets, all_id_feats):
                h, w = frame_data.rgb_np.shape[:2]
                fut = self._executor.submit(
                    self._tracker_pool.update,
                    cam, dets, (h, w), test_size, id_feats,
                )
                futures.append((cam, frame_data, fut))

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
                payload = {
                    "frame_id": frame_data.frame_id,
                    "timestamp": frame_data.ts,
                    "objects": objects,
                }
                asyncio.run_coroutine_threadsafe(
                    self._broadcast_fn(cam, payload), self._event_loop
                )
        except Exception:
            logger.exception("InferencePipeline._process_batch error, skipping frame")


inference_pipeline = InferencePipeline()
```

- [ ] **Step 4: 確認所有測試通過**

```bash
pytest tests/test_inference_pipeline.py -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add inference/pipeline.py tests/test_inference_pipeline.py
git commit -m "feat: add InferencePipeline coordinator"
```

---

## Task 7: routers/tracking.py — ConnectionManager + WebSocket

**Files:**
- Modify: `routers/tracking.py`
- Create: `tests/test_ws_tracking.py`

- [ ] **Step 1: 建立 `tests/test_ws_tracking.py`**

```python
import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# mock HybridSORT 避免 import 時觸發 GPU init
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
    """TestClient with all lifespan side-effects mocked"""
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


# ── ConnectionManager 單元測試（不需 TestClient，用 mock WebSocket）──

def test_broadcast_sends_text_to_connected_ws():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    mock_ws = MagicMock()
    mock_ws.accept = AsyncMock()
    mock_ws.send_text = AsyncMock()

    loop.run_until_complete(mgr.connect("cam_01", mock_ws))
    loop.run_until_complete(mgr.broadcast("cam_01", {"frame_id": 1, "objects": []}))

    mock_ws.send_text.assert_called_once()
    sent = mock_ws.send_text.call_args[0][0]
    assert '"frame_id": 1' in sent
    loop.close()


def test_broadcast_removes_dead_connection():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    dead_ws = MagicMock()
    dead_ws.accept = AsyncMock()
    dead_ws.send_text = AsyncMock(side_effect=RuntimeError("closed"))

    loop.run_until_complete(mgr.connect("cam_01", dead_ws))
    assert dead_ws in mgr._connections["cam_01"]

    loop.run_until_complete(mgr.broadcast("cam_01", {"frame_id": 1}))
    assert dead_ws not in mgr._connections.get("cam_01", set())
    loop.close()


def test_disconnect_removes_ws():
    from routers.tracking import ConnectionManager
    loop = asyncio.new_event_loop()
    mgr = ConnectionManager()

    mock_ws = MagicMock()
    mock_ws.accept = AsyncMock()

    loop.run_until_complete(mgr.connect("cam_01", mock_ws))
    loop.run_until_complete(mgr.disconnect("cam_01", mock_ws))
    assert mock_ws not in mgr._connections.get("cam_01", set())
    loop.close()


# ── HTTP endpoint 確認（via TestClient）──

def test_ws_tracking_http_endpoint_still_works(app_client):
    resp = app_client.get("/tracking/cam_01")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}
```

- [ ] **Step 2: 確認測試失敗**

```bash
pytest tests/test_ws_tracking.py -v
```

Expected: FAIL（ConnectionManager 尚未存在）

- [ ] **Step 3: 改寫 `routers/tracking.py`**

```python
import asyncio
import json
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

router = APIRouter()  # 無 prefix：HTTP 和 WS 路徑都完整寫


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
async def ws_tracking(ws: WebSocket, camera_id: str) -> None:
    await ws_manager.connect(camera_id, ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        await ws_manager.disconnect(camera_id, ws)


@router.get("/tracking/{camera_id}")
async def get_tracking(
    camera_id: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    object_id: Optional[int] = None,
):
    return {"status": "not implemented"}
```

- [ ] **Step 4: 確認所有測試通過**

```bash
pytest tests/test_ws_tracking.py -v
```

Expected: 4 tests PASS

- [ ] **Step 5: Commit**

```bash
git add routers/tracking.py tests/test_ws_tracking.py
git commit -m "feat: add ConnectionManager and WebSocket tracking endpoint"
```

---

## Task 8: zmq_receiver.py — 加 JPEG decode 與 pipeline.update_frame

**Files:**
- Modify: `zmq_receiver.py`
- Modify: `tests/test_zmq_receiver.py`

- [ ] **Step 1: 在 `tests/test_zmq_receiver.py` 加入新測試**

在檔案末尾加入：

```python
def test_process_frame_calls_pipeline_update_frame(monkeypatch):
    import numpy as np
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    fake_rgb_np = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: fake_rgb_np)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._process_frame([topic, metadata, rgb, b""])

    mock_pipeline.update_frame.assert_called_once()
    call_args = mock_pipeline.update_frame.call_args[0]
    assert call_args[0] == "cam_01"
    assert call_args[1] is fake_rgb_np


def test_process_frame_skips_pipeline_when_decode_fails(monkeypatch):
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    # imdecode returns None (invalid JPEG)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: None)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._process_frame([topic, metadata, rgb, b""])

    mock_pipeline.update_frame.assert_not_called()
```

- [ ] **Step 2: 確認新測試失敗**

```bash
pytest tests/test_zmq_receiver.py::test_process_frame_calls_pipeline_update_frame \
       tests/test_zmq_receiver.py::test_process_frame_skips_pipeline_when_decode_fails -v
```

Expected: FAIL

- [ ] **Step 3: 修改 `zmq_receiver.py`**

在檔案頂端 import 區塊加入 `cv2` 和 `numpy`：

```python
import struct
import threading
from typing import Optional

import cv2
import numpy as np
import zmq
from loguru import logger

import hls_manager as hls_mod
import inference.pipeline as pipeline_mod
from config import settings
```

將 `_process_frame` 方法整個取代為：

```python
    def _process_frame(self, parts: list) -> None:
        if len(parts) < 4:
            return
        topic = parts[0].decode()
        ts, frame_id = struct.unpack("dQ", parts[1])
        rgb_bytes: bytes = parts[2]
        thermal_bytes: bytes = parts[3]
        logger.debug(
            f"[{topic}] frame={frame_id} ts={ts:.3f} "
            f"rgb={len(rgb_bytes)}B thermal={len(thermal_bytes)}B"
        )

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
            pipeline_mod.inference_pipeline.update_frame(topic, rgb_np, thermal_np, ts)
```

- [ ] **Step 4: 確認所有 zmq_receiver 測試通過**

```bash
pytest tests/test_zmq_receiver.py -v
```

Expected: 7 tests PASS（5 既有 + 2 新增）

- [ ] **Step 5: Commit**

```bash
git add zmq_receiver.py tests/test_zmq_receiver.py
git commit -m "feat: add JPEG decode and inference pipeline call in zmq_receiver"
```

---

## Task 9: main.py — lifespan 加 pipeline start/stop

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: 更新 `tests/test_main.py` 的 client fixture**

將 fixture 中的 `with (...)` 區塊取代為（加上 pipeline mock）：

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
    ):
        from main import app
        with TestClient(app) as c:
            yield c
```

並在 import 區塊加上 `import inference.pipeline as pipeline_mod`... 但因為 pipeline_mod 是在 fixture 內部 import，直接在 fixture 內 import 即可，不需要頂層 import。

- [ ] **Step 2: 確認現有 test_main.py 測試失敗（因 lifespan 缺少 pipeline mock）**

```bash
pytest tests/test_main.py -v
```

Expected: FAIL 或 ERROR（因 InferencePipeline.start 嘗試初始化 GPU 模型）

- [ ] **Step 3: 修改 `main.py` 的 lifespan**

將 lifespan 函式整個取代為：

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

並在 `main.py` 頂端 import 區塊加入：

```python
import asyncio

from inference.pipeline import inference_pipeline
```

- [ ] **Step 4: 確認 test_main.py 全數通過**

```bash
pytest tests/test_main.py -v
```

Expected: 9 tests PASS

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: wire inference_pipeline into app lifespan"
```

---

## Task 10: static/index.html — canvas overlay + WebSocket JS

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 在 `#video-wrap` 的 CSS 加上 `position: relative`**

在 `static/index.html` 的 `<style>` 區塊，找到 `#video-wrap {` 那一段，在大括號內加入 `position: relative;`：

```css
#video-wrap {
  position: relative;
  width: 100%;
  max-width: 800px;
  background: #000;
  border-radius: 6px;
  overflow: hidden;
  aspect-ratio: 4/3;
}
```

並加上 `#overlay` 樣式（在 `#video-wrap` 樣式之後）：

```css
#overlay {
  position: absolute;
  top: 0; left: 0;
  width: 100%; height: 100%;
  pointer-events: none;
}
```

- [ ] **Step 2: 在 `<video>` 標籤之後加入 `<canvas>`**

找到：
```html
<video id="video" autoplay muted playsinline controls></video>
```

改為：
```html
<video id="video" autoplay muted playsinline controls></video>
<canvas id="overlay"></canvas>
```

- [ ] **Step 3: 在 `<script>` 區塊頂端加入 WS 相關變數**

在 `let hls = null;` 之後加入：

```javascript
let ws = null;
let latestBoxes = [];
const ORIG_W = 640, ORIG_H = 480;
```

- [ ] **Step 4: 加入 `connectWS` 函式與 `drawBoxes` rAF 迴圈**

在 `function setStatus` 函式之後加入：

```javascript
function connectWS(cameraId) {
  if (ws) { ws.close(); ws = null; }
  if (!cameraId) return;
  ws = new WebSocket(`ws://${location.host}/ws/tracking/${cameraId}`);
  ws.onmessage = (e) => {
    try { latestBoxes = JSON.parse(e.data).objects || []; } catch (_) {}
  };
  ws.onclose = () => { latestBoxes = []; };
}

function drawBoxes() {
  const canvas = document.getElementById('overlay');
  const video = document.getElementById('video');
  canvas.width  = video.offsetWidth  || 1;
  canvas.height = video.offsetHeight || 1;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  const sx = canvas.width / ORIG_W;
  const sy = canvas.height / ORIG_H;
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

- [ ] **Step 5: 在 `loadStream()` 函式的最後一行（`} catch (e) {` 之前）加入 `connectWS` 呼叫**

找到 `async function loadStream()` 函式，在函式內部 `try {` 區塊的第一行加入：

```javascript
async function loadStream() {
  if (!currentCamera) return;
  if (hls) { hls.destroy(); hls = null; }
  connectWS(currentCamera);   // ← 加在這裡
  setStatus('正在連線…');
  // ... 其餘不變
```

- [ ] **Step 6: 手動驗證**

啟動服務（需要有真實 ZMQ feed 或直接測 WS 端點）：

```bash
uvicorn main:app --reload
```

開瀏覽器到 `http://localhost:8000`，在 DevTools → Network → WS 確認 `/ws/tracking/cam_01` 連線建立。

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat: add canvas bbox overlay and WebSocket connection to index.html"
```

---

## Task 11: 全測試套件驗證

**Files:** 無修改

- [ ] **Step 1: 執行完整測試套件**

```bash
pytest tests/ -v
```

Expected: 所有測試 PASS。

測試數量預估：
- 既有 41 tests（Phase 1 + 2）
- 新增 Task 1：2 tests
- 新增 Task 3：4 tests
- 新增 Task 4：3 tests
- 新增 Task 5：4 tests
- 新增 Task 6：4 tests
- 新增 Task 7：4 tests
- 新增 Task 8：2 tests
- Task 9 更新 fixture（tests 數不變）

預計總計 ≥ 64 tests PASS

- [ ] **Step 2: 確認 commit log 整潔**

```bash
git log --oneline -12
```

Expected: 看到 Phase 3 的所有 commit（Task 1 到 10 各一個 commit）

---

## 自我審查 checklist（執行前再過一遍）

- [x] `config.py` 新增欄位 → Task 1
- [x] `sys.path` 整合 `ref/HybridSORT` → Task 2
- [x] `BatchDetector.infer()` 回傳 model-space dets → Task 3
- [x] `ReIDExtractor.extract()` 接收原圖座標 → Task 4
- [x] `TrackerPool.update()` lazy-create tracker → Task 5
- [x] `InferencePipeline._process_batch` 正確計算 scale → Task 6
- [x] `ConnectionManager.broadcast()` 移除死連線 → Task 7
- [x] `zmq_receiver._process_frame` decode + pipeline call → Task 8
- [x] `main.py` lifespan start/stop pipeline → Task 9
- [x] `static/index.html` canvas + WS → Task 10
- [x] 所有 test 不依賴真實 GPU → sys.modules mock 在每個 inference 測試檔頂端
- [x] `TrackerPool.update` 的 `img_size` 參數傳入 `detector.test_size` → Task 6 `_process_batch`
