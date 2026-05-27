import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from loguru import logger

import database
from analysis.scheduler import Scheduler
from config import settings as app_settings
from hls_manager import hls_manager
from hls_retention import effective_retention_days, purge_expired_hls
from db_writer import get_all_settings, get_protected_hours
from inference.pipeline import inference_pipeline
from routers import alerts, notes, stream, storage, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver

# HLS retention 巡檢間隔：每 1 小時掃一次過期小時目錄。保留天數每輪即時讀
# DB（user_settings.hls_retention_days），前端改設定免重啟生效；DB 不可用則
# 回退 app_settings 建構時值。
_RETENTION_INTERVAL_SECONDS = 1 * 3600


async def _retention_loop() -> None:
    """週期性刪除超過保留天數的 HLS 小時目錄，避免磁碟無限長大。
    每輪從 DB 讀 hls_retention_days（前端設定即時生效，免重啟）；DB 不可用 /
    讀取失敗 / 缺鍵 / 壞值 → 回退 app_settings 建構時值。
    先等一個間隔再首次巡檢（避免啟動時重磁碟 I/O；保留天數遠大於間隔，
    晚一輪清無妨），之後每 _RETENTION_INTERVAL_SECONDS 跑一次。"""
    while True:
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)
        try:
            pool = database.get_pool()
            db_settings = None
            protected: set[tuple[str, int]] = set()
            if pool is not None:
                try:
                    db_settings = await get_all_settings(pool)
                    protected = await get_protected_hours(pool)
                except Exception as e:
                    logger.warning(f"HLS retention 讀取 DB 設定失敗，回退 app_settings：{e}")
            days = effective_retention_days(
                db_settings, app_settings.hls_retention_days
            )
            purge_expired_hls(
                app_settings.hls_base_dir, days, protected=protected
            )
        except Exception as e:  # 巡檢失敗不可拖垮服務
            logger.warning(f"HLS retention 巡檢失敗：{e}")


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
    yield
    retention_task.cancel()
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
