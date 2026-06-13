import asyncio
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from loguru import logger

import database
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


async def _storage_alert(metric: str, current_value: float, mean_value: float) -> None:
    """storage_monitor 狀態轉換 → 寫一筆系統級 health_alert（進通知中心，
    不碰 get_anomaly_cache → 不亂亮紅框）。DB 不可用 → 只 log。"""
    pool = database.get_pool()
    if pool is None:
        logger.error(f"storage alert {metric} free={current_value:.1f}GB 但 DB 不可用")
        return
    try:
        await write_health_alert(
            pool, camera_id="_system", object_id=0, metric=metric,
            current_value=float(current_value), mean_value=float(mean_value),
            std_value=0.0,
        )
    except Exception as e:
        logger.error(f"寫 storage alert 失敗：{e}")


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
        except Exception as e:
            logger.warning(f"storage monitor loop 錯誤：{e}")
        await asyncio.sleep(interval)


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
    yield
    retention_task.cancel()
    storage_task.cancel()
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
