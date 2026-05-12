import argparse
import threading

import numpy as np
import torch
from loguru import logger
from pathlib import Path

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
    # 軌跡在連續 max_age 幀沒有偵測後才會被刪除（10fps → 預設 300 幀 ≈ 30 秒）。
    # 豬欄遮擋常達十幾秒，調大可避免「遮擋 → 刪 → 重建新 ID」的高頻 churn。
    args.max_age = 300
    # 被刪掉的軌跡會放進 ReID 失蹤庫保留這麼多幀（≈ 120 秒），期間若有外觀相符的
    # 新偵測出現就復活舊 ID，而非開新號碼。
    args.lost_track_buffer = 1200
    # 失蹤庫最多保留幾條軌跡（避免長時間運行後無限成長 / 增加誤配面）。
    args.lost_pool_max = 100
    # ReID 復活的 cosine 距離閾值（越小越保守；寧可開新 ID 也不要把兩隻豬合併）。
    args.reid_revive_thresh = 0.3
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
                    max_age=getattr(self._args, "max_age", 300),
                    iou_threshold=self._args.iou_thresh,
                    asso_func=self._args.asso,
                    delta_t=self._args.deltat,
                    inertia=self._args.inertia,
                )
                logger.info(f"Created tracker for {camera_id}")
            tracker = self._trackers[camera_id]

        # Hybrid_Sort_ReID.update expects a tensor; BatchDetector returns numpy.
        if isinstance(dets, np.ndarray):
            dets = torch.from_numpy(dets)
        # tracker.update is called outside the lock; callers must ensure
        # at most one concurrent update per camera_id (guaranteed by _process_batch).
        return tracker.update(dets, list(img_info), img_size, id_feature=id_feature)
