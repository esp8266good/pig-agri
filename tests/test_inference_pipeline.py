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
