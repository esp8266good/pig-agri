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


# 送幀停滯多久（秒）才開始餵空偵測讓 tracker age out。
#
# 這裡原本是「連續 5 個 tick」的計數式門檻，前提是相機約 10fps、停滯是例外。
# 實測遠端相機經慢速網路只有 0.3~1 fps（幀間隔中位數 3.4 秒），而推論迴圈被
# GPU 綁在約 3 Hz——於是每收到 1 張真幀就夾著約 7 個停滯 tick，其中 4 個以上
# 會餵空偵測。tracker 的 min_hits=3 要求連續命中，每次空偵測都把 hit_streak
# 打回 0，結果那支相機整天吐不出任何已確認軌跡（cam_02 曾連續 23 小時零筆）。
#
# 改用牆鐘門檻，並且要遠大於相機正常的送幀間隔，才不會把「正常的慢」誤判成
# 「停滯」。tick 數會隨 GPU 負載浮動，牆鐘不會。
STALE_SECONDS_BEFORE_AGING: float = 10.0

# 進入 age-out 後，兩次餵空偵測的最小間隔（秒）。不限速的話會以迴圈速率狂餵，
# 又回到原本那個問題。age-out 的用途是「相機真的斷了，讓殘留 track 慢慢消掉」，
# 本來就不需要高頻。
STALE_AGE_OUT_INTERVAL: float = 1.0


class InferencePipeline:
    LOOP_INTERVAL: float = 0.1
    # 處理統計的輸出間隔（秒）。60s 夠密到能看出「哪一刻開始不動」，
    # 又不會把 log 洗掉。
    STATS_INTERVAL: float = 60.0

    def __init__(self) -> None:
        self._latest: dict[str, FrameData] = {}
        # 觀測用計數：每支 camera 這段期間有幾個 tick 拿到新幀 / 幾個 tick 是停滯。
        self._fresh_ticks: dict[str, int] = {}
        self._stale_ticks: dict[str, int] = {}
        self._stats_since: float = time.monotonic()  # start() 後由 _clock 接手
        # 每支 camera 上一輪「實際處理過」的 frame_id。ZMQ 送幀停滯時 _latest 會卡住
        # 同一張凍結幀，若不比對就會每 100ms 重跑 detect→track→write，把單一幀灌進 DB
        # 上百萬列、汙染活動量計算（根因見 docs/handoff-tracking-gap-2026-07-20.md）。
        self._last_processed_fid: dict[str, int] = {}
        # 每支 camera 最後一次拿到新幀 / 最後一次餵空偵測的 monotonic 時刻，
        # 用來區分「正常的慢」與「真的斷了」，並限制 age-out 的頻率。
        self._last_fresh_mono: dict[str, float] = {}
        self._last_ageout_mono: dict[str, float] = {}
        self._lock = threading.Lock()
        self._detector = None
        self._reid = None
        self._tracker_pool = None
        self._executor: ThreadPoolExecutor | None = None
        self._loop_thread: threading.Thread | None = None
        self._running = False
        self._active = True
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._broadcast_fn: Callable | None = None
        # 單調時鐘，抽成欄位是為了測試可以換掉。不可用 monkeypatch 直接改
        # time.monotonic——那是全域的，asyncio 的排程器也在用，凍住會讓
        # run_until_complete 永遠回不來。
        self._clock: Callable[[], float] = time.monotonic

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

    def set_active(self, active: bool) -> None:
        """夜間省電閘門：False → _process_batch 跳過 GPU 計算（detector/ReID/
        tracker 皆不呼叫，GPU 閒置）。執行緒間僅單一 bool 寫入，無需鎖。"""
        self._active = active

    def _log_stats(self) -> None:
        """每 STATS_INTERVAL 秒印一次每支 camera 的處理統計。

        在這之前，「幀有進來」（zmq_receiver）與「DB 有列」之間是全黑的：
        某支 camera 整天沒資料時，看不出是沒收到幀、被凍結防護跳過、還是
        推論被 _active 關掉。這三種的處置完全不同，用猜的會一直猜錯。
        """
        now = self._clock()
        if now - self._stats_since < self.STATS_INTERVAL:
            return
        elapsed = now - self._stats_since
        parts = []
        for cam in sorted(set(self._fresh_ticks) | set(self._stale_ticks)):
            fresh = self._fresh_ticks.get(cam, 0)
            stale = self._stale_ticks.get(cam, 0)
            parts.append(
                f"{cam}: fresh={fresh}({fresh / elapsed:.1f}/s) stale={stale} "
                f"fid={self._last_processed_fid.get(cam)}"
            )
        logger.info(
            f"[inference] active={self._active} {elapsed:.0f}s | "
            + " | ".join(parts or ["(無 camera 送幀)"])
        )
        self._fresh_ticks.clear()
        self._stale_ticks.clear()
        self._stats_since = now

    def _loop(self) -> None:
        while self._running:
            time.sleep(self.LOOP_INTERVAL)
            with self._lock:
                snapshot = dict(self._latest)
            self._log_stats()   # 空 snapshot 也要印，否則「完全沒幀」時一樣是全黑
            if not snapshot:
                continue
            self._process_batch(snapshot)

    def _process_batch(self, snapshot: dict[str, FrameData]) -> None:
        try:
            if not self._active:
                return
            test_size = self._detector.test_size

            # 凍結畫面防護：只處理 frame_id 相對上一輪有前進的 camera。frame_id 沒動 =
            # 該 camera 的 ZMQ 送幀停滯、_latest 卡在舊幀，不可重跑 detector／重複寫 DB。
            # frame_id 是 per-camera 單調遞增，跨 camera 會重複，故一律以 camera_id 分別比對。
            fresh_cams = [c for c, fd in snapshot.items()
                          if self._last_processed_fid.get(c) != fd.frame_id]
            stale_cams = [c for c in snapshot if c not in fresh_cams]

            # 停滯 camera：停滯超過 STALE_SECONDS_BEFORE_AGING 秒才開始餵空偵測，
            # 讓 tracker 內部殘留 track 正常 age out（避免送幀恢復時舊 track 殘留
            # 跳位）；且兩次之間至少隔 STALE_AGE_OUT_INTERVAL 秒。未達門檻完全跳過
            # tracker，不擾動健康軌跡的 hit_streak／frame_count——慢速相機的常態就是
            # 停滯，餵空偵測會讓它永遠達不到 min_hits。
            # 全程不重跑 detector／不寫 DB／不重播 WS。
            now_m = self._clock()
            stale_futures = []
            for cam in stale_cams:
                self._stale_ticks[cam] = self._stale_ticks.get(cam, 0) + 1
                # 沒有 last_fresh 紀錄（剛啟動就看到這支）→ 以現在起算，先不 age out
                since_fresh = now_m - self._last_fresh_mono.get(cam, now_m)
                if since_fresh < STALE_SECONDS_BEFORE_AGING:
                    continue
                if now_m - self._last_ageout_mono.get(cam, 0.0) < STALE_AGE_OUT_INTERVAL:
                    continue
                self._last_ageout_mono[cam] = now_m
                fd = snapshot[cam]
                h, w = fd.rgb_np.shape[:2]
                stale_futures.append(self._executor.submit(
                    self._tracker_pool.update,
                    cam, None, (h, w), test_size,
                    np.zeros((0, 2048), dtype=np.float32),
                ))

            try:
                if not fresh_cams:
                    return

                cameras = fresh_cams
                frames = [snapshot[c] for c in cameras]
                batch_imgs = [f.rgb_np for f in frames]

                all_dets = self._detector.infer(batch_imgs)
                for c, fd in zip(cameras, frames):
                    self._last_processed_fid[c] = fd.frame_id
                    self._last_fresh_mono[c] = now_m
                    self._fresh_ticks[c] = self._fresh_ticks.get(c, 0) + 1

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

                # 每支 camera 各自 try：任一支丟例外都只跳過那一支，迴圈continue
                # 下去把其餘 future 取回。若讓例外往外傳，後面 camera 的 future 就
                # 永遠不會被 result()，下一輪 loop 會對同一支再送一次 update ——
                # 違反 tracker_pool.py 明載的「每 camera 同時至多一個 update」不變式
                # （tracker 內部狀態無鎖，並行更新會讓軌跡錯亂 → ID 亂跳 → 活動量算錯）。
                for cam, frame_data, fut in futures:
                    try:
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
                        logger.exception(f"[{cam}] tracker update/publish failed, skipping camera")
            finally:
                # 無論 fresh 路徑成功或丟例外，都要 await 停滯 camera 的 age-out 更新，
                # 維持 tracker_pool 明載的「每 camera 同時至多一個 update」不變式。
                for f in stale_futures:
                    try:
                        f.result()
                    except Exception:
                        logger.exception("stale tracker age-out failed")
        except Exception:
            logger.exception("InferencePipeline._process_batch error, skipping frame")


inference_pipeline = InferencePipeline()
