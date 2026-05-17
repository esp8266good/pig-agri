import asyncio
import math
import time
from collections import defaultdict
from typing import Optional

import numpy as np
from loguru import logger

from db_writer import write_health_alert

_anomaly_cache: dict[str, dict[int, dict]] = {}


def get_anomaly_cache() -> dict:
    return _anomaly_cache


def _default_entry() -> dict:
    return {
        "activity_anomaly": False, "temp_anomaly": False,
        "activity_state": "normal", "temp_state": "normal",
        "activity_current": None, "activity_mean": None, "activity_std": None,
        "temp_current": None, "temp_mean": None, "temp_std": None,
    }


def _activity_rate(logs: list, window_seconds: float, min_coverage: float) -> Optional[float]:
    """視窗內路徑長度 ÷ 時間跨度（px/s）。資料不足回 None。"""
    if len(logs) < 2:
        return None
    centers = [
        (lg["bb_left"] + lg["bb_width"] / 2, lg["bb_top"] + lg["bb_height"] / 2)
        for lg in logs
    ]
    ts = [lg["timestamp"] for lg in logs]
    span = ts[-1] - ts[0]
    if span < 60.0:
        return None
    if window_seconds <= 0 or span / window_seconds < min_coverage:
        return None
    path = sum(
        math.hypot(centers[i][0] - centers[i - 1][0], centers[i][1] - centers[i - 1][1])
        for i in range(1, len(centers))
    )
    return path / span


class Scheduler:
    def __init__(self, pool, settings) -> None:
        self._pool = pool
        self._settings = settings
        self._task: Optional[asyncio.Task] = None
        self._interval: int = settings.analysis_interval_minutes * 60
        self._threshold: float = float(settings.anomaly_std_threshold)
        self._window_minutes: int = int(settings.analysis_window_minutes)
        self._temp_enabled: bool = bool(getattr(settings, "temp_anomaly_enabled", True))
        self._low_ratio: float = float(getattr(settings, "activity_low_ratio", 0.3))
        self._recover_ratio: float = float(getattr(settings, "activity_recover_ratio", 0.5))
        self._abs_floor: float = float(getattr(settings, "activity_abs_floor", 2.0))
        self._min_coverage: float = float(getattr(settings, "activity_min_coverage", 0.5))

    async def start(self) -> None:
        await self._rebuild_cache()
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Scheduler stopped")

    def reload(
        self,
        interval_minutes: int,
        std_threshold: float,
        window_minutes: int,
        temp_anomaly_enabled: bool,
    ) -> None:
        self._interval = interval_minutes * 60
        self._threshold = std_threshold
        self._window_minutes = int(window_minutes)
        self._temp_enabled = bool(temp_anomaly_enabled)

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self._run_analysis()
            except Exception:
                logger.exception("Scheduler._run_analysis error")

    async def _rebuild_cache(self) -> None:
        """重啟：建立 cache 骨架，但 state 一律 normal、旗標 False（不被歷史 alert 閂死）。"""
        if self._pool is None:
            return
        try:
            rows = await self._pool.fetch(
                """SELECT DISTINCT ON (camera_id, object_id, metric)
                   camera_id, object_id, metric
                   FROM health_alerts
                   ORDER BY camera_id, object_id, metric, triggered_at DESC"""
            )
            for row in rows:
                _anomaly_cache.setdefault(row["camera_id"], {}).setdefault(
                    row["object_id"], _default_entry()
                )
        except Exception:
            logger.exception("Scheduler._rebuild_cache error")

    async def _run_analysis(self) -> None:
        if self._pool is None:
            return
        now = time.time()
        window_seconds = self._window_minutes * 60
        window_start = now - window_seconds

        rows = await self._pool.fetch(
            """SELECT DISTINCT camera_id, object_id
               FROM tracking_logs
               WHERE timestamp >= $1 AND timestamp < $2""",
            window_start, now,
        )

        by_cam: dict[str, list] = defaultdict(list)
        for r in rows:
            by_cam[r["camera_id"]].append(r["object_id"])

        for camera_id, object_ids in by_cam.items():
            rates: dict[int, float] = {}
            logs_by_obj: dict[int, list] = {}

            for object_id in object_ids:
                logs = await self._pool.fetch(
                    """SELECT bb_left, bb_top, bb_width, bb_height,
                              thermal_intensity, timestamp
                       FROM tracking_logs
                       WHERE camera_id=$1 AND object_id=$2
                         AND timestamp >= $3 AND timestamp < $4
                       ORDER BY timestamp""",
                    camera_id, object_id, window_start, now,
                )
                logs_by_obj[object_id] = logs
                entry = _anomaly_cache.setdefault(camera_id, {}).setdefault(
                    object_id, _default_entry()
                )
                rate = _activity_rate(logs, window_seconds, self._min_coverage)
                entry["activity_current"] = rate
                if rate is not None:
                    rates[object_id] = rate

            median_rate = (
                float(np.median(list(rates.values()))) if len(rates) >= 2 else None
            )
            herd_ok = median_rate is not None and median_rate >= self._abs_floor

            for object_id in object_ids:
                entry = _anomaly_cache[camera_id][object_id]
                rate = rates.get(object_id)

                if herd_ok and rate is not None:
                    entry["activity_mean"] = median_rate
                    low = rate < median_rate * self._low_ratio
                    recovered = rate > median_rate * self._recover_ratio
                    if entry["activity_state"] == "normal":
                        if low:
                            await write_health_alert(
                                self._pool, camera_id=camera_id, object_id=object_id,
                                metric="activity", current_value=rate,
                                mean_value=median_rate, std_value=0.0,
                            )
                            entry["activity_state"] = "alerted"
                    else:  # alerted
                        if recovered:
                            entry["activity_state"] = "normal"
                    entry["activity_anomaly"] = entry["activity_state"] == "alerted"

                if self._temp_enabled:
                    temps = [
                        lg["thermal_intensity"] for lg in logs_by_obj[object_id]
                        if lg["thermal_intensity"] is not None
                    ]
                    if len(temps) >= self._settings.anomaly_min_samples:
                        mean_t = float(np.mean(temps))
                        std_t = float(np.std(temps))
                        current_t = temps[-1]
                        entry.update({
                            "temp_current": current_t,
                            "temp_mean": mean_t,
                            "temp_std": std_t,
                        })
                        anomalous = std_t > 0 and abs(current_t - mean_t) > self._threshold * std_t
                        if entry["temp_state"] == "normal":
                            if anomalous:
                                await write_health_alert(
                                    self._pool, camera_id=camera_id, object_id=object_id,
                                    metric="temperature", current_value=current_t,
                                    mean_value=mean_t, std_value=std_t,
                                )
                                entry["temp_state"] = "alerted"
                        else:  # alerted
                            if not anomalous:
                                entry["temp_state"] = "normal"
                        entry["temp_anomaly"] = entry["temp_state"] == "alerted"
                else:
                    entry["temp_anomaly"] = False
                    entry["temp_state"] = "normal"
