import asyncio
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Callable

import numpy as np
from loguru import logger

import inference  # triggers sys.path setup
import database
from db_writer import write_tracking_log


def _compute_thermal_intensity(
    thermal_np: "np.ndarray | None",
    x1: float, y1: float, x2: float, y2: float,
    orig_w: int = 640, orig_h: int = 480,
    thermal_w: int = 160, thermal_h: int = 120,
) -> "float | None":
    if thermal_np is None:
        return None
    sx = thermal_w / orig_w
    sy = thermal_h / orig_h
    tx1 = int(max(0, x1 * sx))
    ty1 = int(max(0, y1 * sy))
    tx2 = int(min(thermal_w, x2 * sx))
    ty2 = int(min(thermal_h, y2 * sy))
    if tx2 <= tx1 or ty2 <= ty1:
        return None
    return float(np.mean(thermal_np[ty1:ty2, tx1:tx2]))


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
        logger.info("InferencePipeline stopped")

    def update_frame(
        self,
        camera_id: str,
        rgb_np: np.ndarray,
        thermal_np: np.ndarray | None,
        ts: float,
        frame_id: int,
    ) -> None:
        # frame_id 為擷取端真實 frame_id；HLS bbox 同步已改用真實 capture_ts（不再用 frame_id），
        # 此處保留僅供 VOD /tracking 同幀群聚（pickClosestFrame）與 DB tracking_logs 記錄。
        with self._lock:
            self._latest[camera_id] = FrameData(
                rgb_np=rgb_np, thermal_np=thermal_np, ts=ts, frame_id=frame_id
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
                    obj_id = int(t[4])
                    conf = float(t[5]) if len(t) > 5 else 0.0
                    ti = _compute_thermal_intensity(frame_data.thermal_np, x1, y1, x2, y2)
                    objects.append({
                        "object_id": obj_id,
                        "bbox": [x1, y1, x2 - x1, y2 - y1],
                        "confidence": conf,
                        # thermal_intensity is DB-only; not included in live WS payload
                    })
                    pool = database.get_pool()
                    if pool is not None:
                        asyncio.run_coroutine_threadsafe(
                            write_tracking_log(
                                pool,
                                camera_id=cam,
                                timestamp=frame_data.ts,
                                frame_id=frame_data.frame_id,
                                object_id=obj_id,
                                bb_left=x1,
                                bb_top=y1,
                                bb_width=x2 - x1,
                                bb_height=y2 - y1,
                                confidence=conf,
                                thermal_intensity=ti,
                            ),
                            self._event_loop,
                        )
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
