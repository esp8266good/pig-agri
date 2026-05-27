from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
from config import settings as app_settings
from db_writer import (
    delete_recordings_in_range,
    delete_saved_segment,
    delete_saved_segments_by_hours,
    list_bookmarks,
    list_saved_segments,
    update_saved_segment,
    upsert_saved_segment,
)
from hls_retention import delete_recording_hours

router = APIRouter(prefix="/storage", tags=["storage"])


def _require_pool():
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    return pool


def _require_camera(camera_id: str) -> None:
    if camera_id not in [s.label for s in app_settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")


class SegmentCreate(BaseModel):
    camera_id: str
    hours: list[int]
    label: Optional[str] = None
    note: Optional[str] = None


class SegmentUpdate(BaseModel):
    label: Optional[str] = None
    note: Optional[str] = None


class RecordingsDelete(BaseModel):
    camera_id: str
    hours: list[int]


@router.get("/segments")
async def get_segments(camera_id: str, start_ts: float, end_ts: float):
    pool = _require_pool()
    _require_camera(camera_id)
    segments = await list_saved_segments(pool, camera_id, start_ts, end_ts)
    return {"segments": segments}


@router.get("/bookmarks")
async def get_bookmarks(camera_id: Optional[str] = None):
    pool = _require_pool()
    if camera_id is not None:
        _require_camera(camera_id)
    bookmarks = await list_bookmarks(pool, camera_id)
    return {"bookmarks": bookmarks}


@router.post("/segments")
async def create_segments(body: SegmentCreate):
    pool = _require_pool()
    _require_camera(body.camera_id)
    if not body.hours:
        raise HTTPException(status_code=400, detail="hours must not be empty")
    for h in body.hours:
        await upsert_saved_segment(pool, body.camera_id, h, body.label, body.note)
    return {"ok": True, "count": len(body.hours)}


@router.put("/segments/{seg_id}")
async def edit_segment(seg_id: int, body: SegmentUpdate):
    pool = _require_pool()
    found = await update_saved_segment(pool, seg_id, body.label, body.note)
    if not found:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.delete("/segments/{seg_id}")
async def remove_segment(seg_id: int):
    pool = _require_pool()
    found = await delete_saved_segment(pool, seg_id)
    if not found:
        raise HTTPException(status_code=404, detail="Segment not found")
    return {"ok": True}


@router.post("/recordings/delete")
async def delete_recordings(body: RecordingsDelete):
    pool = _require_pool()
    _require_camera(body.camera_id)
    if not body.hours:
        raise HTTPException(status_code=400, detail="hours must not be empty")
    dirs = delete_recording_hours(
        app_settings.hls_base_dir, body.camera_id, body.hours
    )
    tl = ha = 0
    for h in body.hours:
        counts = await delete_recordings_in_range(pool, body.camera_id, h, h + 3600)
        tl += counts["tracking_logs"]
        ha += counts["health_alerts"]
    await delete_saved_segments_by_hours(pool, body.camera_id, body.hours)
    return {
        "ok": True,
        "deleted_hours": len(body.hours),
        "dirs_removed": len(dirs),
        "tracking_logs": tl,
        "health_alerts": ha,
    }
