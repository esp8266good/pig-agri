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
