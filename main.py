from contextlib import asynccontextmanager

from fastapi import FastAPI

import database
from config import settings as app_settings
from routers import alerts, notes, stream, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    await database.disconnect()


app = FastAPI(title="豬隻疾病監測系統", lifespan=lifespan)


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
