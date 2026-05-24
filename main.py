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
from hls_retention import purge_expired_hls
from inference.pipeline import inference_pipeline
from routers import alerts, notes, stream, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver

# HLS retention 巡檢間隔：每 6 小時掃一次過期小時目錄。過期判定的天數來自
# app_settings.hls_retention_days（每輪即時讀取）。
_RETENTION_INTERVAL_SECONDS = 6 * 3600


async def _retention_loop() -> None:
    """週期性刪除超過 hls_retention_days 的 HLS 小時目錄，避免磁碟無限長大。
    先等一個間隔再首次巡檢（避免啟動時的重磁碟 I/O；保留天數遠大於間隔，
    晚一輪清無妨），之後每 _RETENTION_INTERVAL_SECONDS 跑一次。"""
    while True:
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)
        try:
            purge_expired_hls(
                app_settings.hls_base_dir, app_settings.hls_retention_days
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
