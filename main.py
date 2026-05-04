import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import database
from config import settings as app_settings
from hls_manager import hls_manager
from inference.pipeline import inference_pipeline
from routers import alerts, notes, stream, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    loop = asyncio.get_event_loop()
    inference_pipeline.start(loop)
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    inference_pipeline.stop()
    hls_manager.stop_all()
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
    return {"cameras": app_settings.camera_topics}


app.include_router(stream.router)
app.include_router(tracking.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
app.include_router(notes.router)
