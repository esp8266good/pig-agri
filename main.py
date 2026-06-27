import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from loguru import logger

import database
import ntfy_notifier
import storage_monitor
from analysis.scheduler import Scheduler
from config import settings as app_settings
from hls_manager import hls_manager
from hls_retention import effective_retention_days, purge_expired_hls
from db_writer import get_all_settings, get_protected_hours, write_health_alert
from inference.pipeline import inference_pipeline
from routers import alerts, notes, stream, storage, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver

# HLS retention 巡檢間隔：每 1 小時掃一次過期小時目錄。保留天數每輪即時讀
# DB（user_settings.hls_retention_days），前端改設定免重啟生效；DB 不可用則
# 回退 app_settings 建構時值。
_RETENTION_INTERVAL_SECONDS = 1 * 3600

# 錄影監督者巡檢間隔：每 10s 確保每攝影機錄影串流存在（不依賴有人開直播頁）。
_SUPERVISOR_INTERVAL_SECONDS = 10

# 上一輪監督者確認過「應存在」的串流 keys；用來區分「首次建立」（不告警）與
# 「之前在、這輪不見了的重建」（告警 recording_supervisor_revive）。
_supervised_prev: set = set()


async def _run_retention_once() -> None:
    """單輪 retention 巡檢。DB 不可用或讀取失敗 → 跳過本輪、不刪任何東西，
    確保「保留/書籤」時段在無法取得權威保護集合時絕不被誤刪（保留 = 絕不
    自動刪的承諾優先於磁碟回收；錯過一輪無妨，下輪 DB 恢復即補上）。"""
    pool = database.get_pool()
    if pool is None:
        logger.warning("HLS retention：DB 不可用，跳過本輪巡檢（不刪除）")
        return
    try:
        db_settings = await get_all_settings(pool)
        protected = await get_protected_hours(pool)
    except Exception as e:
        logger.warning(f"HLS retention：讀取 DB 失敗，跳過本輪巡檢（不刪除）：{e}")
        return
    days = effective_retention_days(db_settings, app_settings.hls_retention_days)
    purge_expired_hls(app_settings.hls_base_dir, days, protected=protected)


async def _retention_loop() -> None:
    """週期性刪除超過保留天數且未受保護的 HLS 小時目錄。先等一個間隔再首次
    巡檢（避免啟動時重磁碟 I/O；保留天數遠大於間隔，晚一輪清無妨）。"""
    while True:
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)
        try:
            await _run_retention_once()
        except Exception as e:  # 巡檢失敗不可拖垮服務
            logger.warning(f"HLS retention 巡檢失敗：{e}")


# metric → (ntfy 標題, priority, tags)。未列入的 metric 不推播。
_NTFY_MAP: dict[str, tuple[str, str, str]] = {
    "storage_unwritable":          ("🚨 錄影碟不可寫", "urgent", "rotating_light"),
    "storage_low_space":           ("⚠️ 儲存空間偏低", "high", "warning"),
    "storage_recovered":           ("✅ 儲存已恢復", "default", "white_check_mark"),
    "recording_paused":            ("🌙 夜間暫停錄影", "low", "moon"),
    "recording_resumed":           ("✅ 已恢復錄影", "default", "white_check_mark"),
    "recording_supervisor_revive": ("⚠️ 錄影串流已自動重建", "high", "warning"),
}


async def _push_ntfy(metric: str, free_gb: float) -> None:
    """依 metric 推播 ntfy。停用 / metric 未列入 → no-op；URL 空由 ntfy_notifier 處理。
    URL/開關優先讀 DB（即時生效），失敗回退 app_settings。"""
    spec = _NTFY_MAP.get(metric)
    if spec is None:
        return
    url = app_settings.ntfy_url
    enabled = app_settings.ntfy_enabled
    pool = database.get_pool()
    if pool is not None:
        try:
            db = await get_all_settings(pool)
            if db.get("ntfy_url") is not None:
                url = db["ntfy_url"]
            if db.get("ntfy_enabled") is not None:
                enabled = str(db["ntfy_enabled"]).strip().lower() == "true"
        except Exception:
            pass
    if not enabled:
        return
    title, priority, tags = spec
    if metric == "recording_supervisor_revive":
        msg = "偵測到錄影串流消失，已自動重新啟動"
    else:
        msg = f"{metric} | 錄影碟可用 {free_gb:.1f} GB"
    await ntfy_notifier.notify(url, title, msg, priority=priority, tags=tags)


async def _storage_alert(metric: str, current_value: float, mean_value: float) -> None:
    """storage_monitor 狀態轉換 → 寫一筆系統級 health_alert（進通知中心，
    不碰 get_anomaly_cache → 不亂亮紅框）。DB 不可用 → 只 log。
    不論 DB 是否可用都嘗試推播（_push_ntfy 自行讀 DB/回退 app_settings）。"""
    pool = database.get_pool()
    if pool is None:
        logger.error(f"storage alert {metric} free={current_value:.1f}GB 但 DB 不可用")
    else:
        try:
            await write_health_alert(
                pool, camera_id="_system", object_id=0, metric=metric,
                current_value=float(current_value), mean_value=float(mean_value),
                std_value=0.0,
            )
        except Exception as e:
            logger.error(f"寫 storage alert 失敗：{e}")
    # 不論 DB 是否可用都嘗試推播（_push_ntfy 自行讀 DB/回退 app_settings）。
    try:
        await _push_ntfy(metric, current_value)
    except Exception as e:
        logger.warning(f"ntfy 推播失敗：{e}")


def _apply_gpu_schedule(db_settings: "dict | None") -> None:
    """依 DB/app_settings 的 gpu_off 排程算當下推論是否該開，設 inference 旗標。"""
    active = storage_monitor.resolve_gpu_active(
        db_settings, app_settings, datetime.now())
    inference_pipeline.set_active(active)


async def _storage_monitor_loop() -> None:
    """每 storage_check_interval_seconds 量錄影碟/ephemeral 碟健康 → 更新
    target_mode（hls_manager 讀取）+ 狀態轉換時告警。設定每輪讀 DB（即時生效）。
    首輪立即跑（讓 target_mode 早就緒）。"""
    eph_base = storage_monitor.effective_ephemeral_dir(app_settings.hls_ephemeral_dir)
    while True:
        interval = app_settings.storage_check_interval_seconds
        try:
            pool = database.get_pool()
            if pool is not None:
                db_settings = await get_all_settings(pool)
            else:
                db_settings = None
                logger.debug("storage monitor：DB 不可用，本輪用 app_settings 預設")
            s = storage_monitor.resolve_settings(db_settings, app_settings)
            interval = max(5, s.check_interval_seconds)
            await storage_monitor.monitor.run_once(
                recording_base=app_settings.hls_base_dir,
                ephemeral_base=eph_base,
                settings=s,
                now=datetime.now(),
                alert_cb=_storage_alert,
            )
            _apply_gpu_schedule(db_settings)
        except Exception as e:
            logger.warning(f"storage monitor loop 錯誤：{e}")
        await asyncio.sleep(interval)


async def _run_recording_supervisor_once() -> None:
    """確保每個攝影機的錄影串流存在；被逐出/死掉的下一輪重建。drop（雙碟全死）
    時不重建（無處可寫）。某串流之前在、這輪卻不見 → 視為重建並告警。"""
    global _supervised_prev
    if storage_monitor.get_target_mode() == "drop":
        return
    cameras = [s.label for s in app_settings.zmq_sources]
    desired = hls_manager.desired_recording_keys(cameras)
    revived: list = []
    for cam, stype in desired:
        present_before = hls_manager.has_stream(cam, stype)
        try:
            hls_manager.ensure_started(cam, stype)
        except Exception as e:
            logger.warning(f"[{cam}/{stype}] 錄影監督者 ensure_started 失敗：{e}")
            continue
        if not present_before and (cam, stype) in _supervised_prev:
            revived.append((cam, stype))
    _supervised_prev = set(desired)
    for cam, stype in revived:
        logger.warning(f"[{cam}/{stype}] 錄影監督者重建已消失的串流")
        await _storage_alert("recording_supervisor_revive", 0.0, 0.0)


async def _recording_supervisor_loop() -> None:
    """週期性確保錄影串流存活。例外只 log，絕不拖垮服務。"""
    while True:
        await asyncio.sleep(_SUPERVISOR_INTERVAL_SECONDS)
        try:
            await _run_recording_supervisor_once()
        except Exception as e:
            logger.warning(f"錄影監督者巡檢失敗：{e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    scheduler = Scheduler(database.get_pool(), app_settings)
    await scheduler.start()
    app.state.scheduler = scheduler
    loop = asyncio.get_event_loop()
    inference_pipeline.start(loop)
    zmq_receiver.start()
    retention_task = asyncio.create_task(_retention_loop())
    storage_task = asyncio.create_task(_storage_monitor_loop())
    supervisor_task = asyncio.create_task(_recording_supervisor_loop())
    yield
    retention_task.cancel()
    storage_task.cancel()
    supervisor_task.cancel()
    zmq_receiver.stop()
    inference_pipeline.stop()
    hls_manager.stop_all()
    await scheduler.stop()
    await database.disconnect()


app = FastAPI(title="豬隻疾病監測系統", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse(url="/static/index.html")


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok"}


@app.get("/cameras", tags=["system"])
async def list_cameras():
    return {"cameras": [s.label for s in app_settings.zmq_sources]}


app.include_router(stream.router)
app.include_router(tracking.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
app.include_router(notes.router)
app.include_router(storage.router)
