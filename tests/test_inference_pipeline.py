import sys
import asyncio
import threading
import numpy as np
import pytest
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
    p._lock = threading.Lock()
    p._detector = None
    p._reid = None
    p._tracker_pool = None
    p._executor = None
    p._loop_thread = None
    p._running = False
    p._active = True
    p._event_loop = None
    p._broadcast_fn = None
    return p


def test_update_frame_stores_latest():
    from inference.pipeline import FrameData
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0, 438190)
    assert "cam_01" in p._latest
    assert p._latest["cam_01"].rgb_np is rgb


def test_update_frame_uses_camera_frame_id():
    """frame_id 必須是擷取端（zmq 封包頭）的真實 frame_id，供 VOD /tracking 同幀群聚
    （pickClosestFrame）與 DB tracking_logs 記錄；HLS bbox 同步已改用 capture_ts，不再依賴 frame_id。"""
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0, 438190)
    p.update_frame("cam_01", rgb, None, 2.0, 438191)
    assert p._latest["cam_01"].frame_id == 438191


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
