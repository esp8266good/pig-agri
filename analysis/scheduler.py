import asyncio
import math
import time
from typing import Optional

import numpy as np
from loguru import logger

from db_writer import write_health_alert

_anomaly_cache: dict[str, dict[int, dict]] = {}


def get_anomaly_cache() -> dict:
    return _anomaly_cache


class Scheduler:
    def __init__(self, pool, settings) -> None:
        self._pool = pool
        self._settings = settings
        self._task: Optional[asyncio.Task] = None

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

    async def _loop(self) -> None:
        interval = self._settings.analysis_interval_minutes * 60
        while True:
            await asyncio.sleep(interval)
            try:
                await self._run_analysis()
            except Exception:
                logger.exception("Scheduler._run_analysis error")

    async def _rebuild_cache(self) -> None:
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
                cam = row["camera_id"]
                oid = row["object_id"]
                metric = row["metric"]
                entry = _anomaly_cache.setdefault(cam, {}).setdefault(oid, {
                    "activity_anomaly": False, "temp_anomaly": False,
                    "activity_current": None, "activity_mean": None, "activity_std": None,
                    "temp_current": None, "temp_mean": None, "temp_std": None,
                })
                if metric == "activity":
                    entry["activity_anomaly"] = True
                elif metric == "temperature":
                    entry["temp_anomaly"] = True
        except Exception:
            logger.exception("Scheduler._rebuild_cache error")

    async def _run_analysis(self) -> None:
        if self._pool is None:
            return
        now = time.time()
        window_start = now - self._settings.analysis_window_minutes * 60

        rows = await self._pool.fetch(
            """SELECT DISTINCT camera_id, object_id
               FROM tracking_logs
               WHERE timestamp >= $1 AND timestamp < $2""",
            window_start, now,
        )

        for r in rows:
            camera_id = r["camera_id"]
            object_id = r["object_id"]
            logs = await self._pool.fetch(
                """SELECT bb_left, bb_top, bb_width, bb_height, thermal_intensity, timestamp
                   FROM tracking_logs
                   WHERE camera_id=$1 AND object_id=$2
                     AND timestamp >= $3 AND timestamp < $4
                   ORDER BY timestamp""",
                camera_id, object_id, window_start, now,
            )
            if len(logs) < self._settings.anomaly_min_samples:
                continue

            centers = [
                (log["bb_left"] + log["bb_width"] / 2, log["bb_top"] + log["bb_height"] / 2)
                for log in logs
            ]
            displacements = [
                math.hypot(centers[i][0] - centers[i-1][0], centers[i][1] - centers[i-1][1])
                for i in range(1, len(centers))
            ]
            temps = [
                log["thermal_intensity"] for log in logs
                if log["thermal_intensity"] is not None
            ]

            entry = _anomaly_cache.setdefault(camera_id, {}).setdefault(object_id, {
                "activity_anomaly": False, "temp_anomaly": False,
                "activity_current": None, "activity_mean": None, "activity_std": None,
                "temp_current": None, "temp_mean": None, "temp_std": None,
            })

            if len(displacements) >= 2:
                mean_a = float(np.mean(displacements))
                std_a = float(np.std(displacements))
                current_a = displacements[-1]
                entry.update({
                    "activity_current": current_a,
                    "activity_mean": mean_a,
                    "activity_std": std_a,
                })
                if std_a > 0 and current_a < mean_a - self._settings.anomaly_std_threshold * std_a:
                    if not entry["activity_anomaly"]:
                        await write_health_alert(
                            self._pool, camera_id=camera_id, object_id=object_id,
                            metric="activity", current_value=current_a,
                            mean_value=mean_a, std_value=std_a,
                        )
                    entry["activity_anomaly"] = True
                else:
                    entry["activity_anomaly"] = False

            if len(temps) >= 2:
                mean_t = float(np.mean(temps))
                std_t = float(np.std(temps))
                current_t = temps[-1]
                entry.update({
                    "temp_current": current_t,
                    "temp_mean": mean_t,
                    "temp_std": std_t,
                })
                if std_t > 0 and abs(current_t - mean_t) > self._settings.anomaly_std_threshold * std_t:
                    if not entry["temp_anomaly"]:
                        await write_health_alert(
                            self._pool, camera_id=camera_id, object_id=object_id,
                            metric="temperature", current_value=current_t,
                            mean_value=mean_t, std_value=std_t,
                        )
                    entry["temp_anomaly"] = True
                else:
                    entry["temp_anomaly"] = False
