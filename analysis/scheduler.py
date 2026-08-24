import asyncio
import math
import time
from collections import defaultdict
from typing import Optional

import numpy as np
from loguru import logger

from db_writer import write_health_alert

_anomaly_cache: dict[str, dict[int, dict]] = {}

# 每台相機最近一輪分析的結論。以前這兩個旗標只寫在 per-object 的 entry 裡，
# 於是「這台相機的 entry 被清光了」與「這台相機從來沒被分析過」在下游長得
# 一模一樣——清掉過期 object_id 之後，夜間本來該說「豬群活動量普遍偏低」
# 就會變成「目前沒有需要注意的豬」，把一個保護講成了一份保證。
_camera_state: dict[str, dict] = {}


def get_anomaly_cache() -> dict:
    return _anomaly_cache


def get_camera_state() -> dict:
    return _camera_state


def _default_entry() -> dict:
    return {
        "activity_anomaly": False, "temp_anomaly": False,
        "activity_state": "normal", "temp_state": "normal",
        "activity_current": None, "activity_mean": None, "activity_std": None,
        "temp_current": None, "temp_mean": None, "temp_std": None,
        # 全欄是否有評估依據。per-camera 的判斷存在 per-object 的 entry 裡，
        # 沿用 activity_mean 的既有做法；關注清單要靠它決定給不給名字。
        "herd_ok": False,
        # 是否已經被分析過至少一次。_loop 是先 sleep 再分析，重啟後最長要等一個
        # analysis_interval 才有第一筆結果；沒有這個旗標就分不出「全欄安靜」
        # 與「還沒算過」，關注清單會在重啟後誤報。
        "analyzed": False,
        # 這個 object_id 最後一次出現在分析視窗裡的時刻（unix 秒）。
        # None = 從沒出現過（重啟時由 _rebuild_cache 從歷史告警建的骨架）。
        # 這是逐出的依據，見 _prune_stale。
        "last_seen": None,
    }


def _activity_rate(logs: list, min_span_seconds: float) -> Optional[float]:
    """視窗內路徑長度 ÷ 時間跨度（px/s）。資料不足、或軌跡跨度 <
    min_span_seconds（資料太少不足以估速率）回 None。門檻是絕對秒數、
    與分析視窗長度無關——避免長視窗下被 MOT ID 跳號永久卡死（見 config）。"""
    if len(logs) < 2:
        return None
    centers = [
        (lg["bb_left"] + lg["bb_width"] / 2, lg["bb_top"] + lg["bb_height"] / 2)
        for lg in logs
    ]
    ts = [lg["timestamp"] for lg in logs]
    span = ts[-1] - ts[0]
    if span <= 0 or span < min_span_seconds:
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
        self._min_span_seconds: float = float(
            getattr(settings, "activity_min_span_seconds", 300.0)
        )

    async def start(self) -> None:
        await self._apply_db_settings()
        await self._rebuild_cache()
        self._task = asyncio.create_task(self._loop())
        logger.info("Scheduler started")

    async def _apply_db_settings(self) -> None:
        """啟動時讓 DB 持久化設定覆蓋建構時的 config 預設。

        設定 UI 是權威來源；否則 app 重啟後會用 config 預設（temp 預設 True）
        靜默重新啟用體溫偵測，即使使用者早已在 UI 停用並存進 DB。
        """
        if self._pool is None:
            return
        try:
            from db_writer import get_all_settings
            s = await get_all_settings(self._pool)
        except Exception:
            logger.exception("Scheduler._apply_db_settings error")
            return
        if s.get("temp_anomaly_enabled") is not None:
            self._temp_enabled = str(s["temp_anomaly_enabled"]).strip().lower() == "true"
        if s.get("analysis_interval_minutes"):
            self._interval = int(s["analysis_interval_minutes"]) * 60
        if s.get("anomaly_std_threshold"):
            self._threshold = float(s["anomaly_std_threshold"])
        if s.get("analysis_window_minutes"):
            self._window_minutes = int(s["analysis_window_minutes"])
        if not self._temp_enabled:
            self._clear_all_temp_flags()

    @staticmethod
    def _prune_stale(window_start: float) -> int:
        """清掉「已經不存在的 object_id」。回傳清掉幾筆。

        MOT 的 ID 會跳號：同一隻豬遮擋一次出來就換一個新號碼，舊號碼從此再也
        不會出現在任何一筆 tracking_log 裡。但 _anomaly_cache 只會長不會縮，
        於是關注清單上那些 id 永遠不消退——畫面上根本沒有那隻豬，清單卻一直
        指著牠，愈積愈長，把真正該看的擠到看不見的地方。

        逐出的判準用「活動量評估窗口」而不是定時整批清空：
          · 定時清空會連遲滯狀態機一起重置（`activity_state` 回到 normal），
            真正持續低活動的豬每一輪都會被當成新的異常重新告警、推播一直響。
          · 分析本來就只看得到視窗內出現過的 object_id，視窗外的那些不管留多久
            都不可能再被更新一次。所以「視窗內沒出現過」＝「這個 id 已經死了」，
            清掉它不會丟掉任何還在更新的判斷。

        last_seen 是 None 的是重啟時從歷史告警建的骨架（見 _rebuild_cache），
        它們的用途只到「第一輪分析跑完之前讓關注清單說得出 not_analyzed」為止，
        跑完就該讓位給真實資料。
        """
        removed = 0
        for cam, objs in list(_anomaly_cache.items()):
            for oid, entry in list(objs.items()):
                last_seen = entry.get("last_seen")
                if last_seen is None or last_seen < window_start:
                    del objs[oid]
                    removed += 1
        return removed

    @staticmethod
    def _clear_all_temp_flags() -> None:
        """掃過整個 cache（含不在當前視窗的殘留 object_id）清掉 temp 旗標。

        /alerts/active 直接回傳整個 _anomaly_cache；ID 跳號使被標記過的
        舊 object_id 不會再進分析視窗，若不全掃就永遠清不掉那些紅框。
        """
        for objs in _anomaly_cache.values():
            for entry in objs.values():
                entry["temp_anomaly"] = False
                entry["temp_state"] = "normal"

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
        if not self._temp_enabled:
            self._clear_all_temp_flags()

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
        if not self._temp_enabled:
            # 每輪整批清，連不在視窗的殘留 object_id 也涵蓋（根因 A）
            self._clear_all_temp_flags()
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

        # 這一輪視窗內完全沒有偵測資料的相機（夜間全黑、相機斷線）也要記狀態，
        # 否則 _camera_state 會停在上一次有資料時的結論，關注清單會拿一份幾小時
        # 前的「一切正常」當成現在的判斷。
        for camera_id in set(_camera_state) | set(_anomaly_cache):
            if camera_id not in by_cam:
                _camera_state[camera_id] = {
                    "analyzed": True, "herd_ok": False, "updated_at": now,
                }

        for camera_id, object_ids in by_cam.items():
            rates: dict[int, float] = {}
            logs_by_obj: dict[int, list] = {}

            for object_id in object_ids:
                logs = await self._pool.fetch(
                    """SELECT bb_left, bb_top, bb_width, bb_height,
                              thermal_celsius, timestamp
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
                # 這一輪視窗裡有這個 id 的資料，牠還活著。
                entry["last_seen"] = now
                rate = _activity_rate(logs, self._min_span_seconds)
                entry["activity_current"] = rate
                if rate is not None:
                    rates[object_id] = rate

            median_rate = (
                float(np.median(list(rates.values()))) if len(rates) >= 2 else None
            )
            herd_ok = median_rate is not None and median_rate >= self._abs_floor
            _camera_state[camera_id] = {
                "analyzed": True, "herd_ok": herd_ok, "updated_at": now,
            }

            for object_id in object_ids:
                entry = _anomaly_cache[camera_id][object_id]
                rate = rates.get(object_id)

                entry["activity_mean"] = median_rate
                entry["herd_ok"] = herd_ok
                entry["analyzed"] = True

                # 當 herd_ok 為 False（全欄休息 / 豬數不足 / median < abs_floor）時，
                # 此區塊整個跳過——已在 alerted 的豬不會被自動清旗，
                # 確保真正低活動的採血警報持續留存，直到該豬自行恢復為止。
                if herd_ok and rate is not None:
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
                        lg["thermal_celsius"] for lg in logs_by_obj[object_id]
                        if lg["thermal_celsius"] is not None
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

        removed = self._prune_stale(window_start)
        if removed:
            logger.info(f"關注快取清掉 {removed} 個已消失的 object_id（ID 跳號的殘留）")
