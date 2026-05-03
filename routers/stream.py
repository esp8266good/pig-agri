from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from config import settings
from hls_manager import hls_manager

router = APIRouter(prefix="/stream", tags=["stream"])


# /hls/... must be defined BEFORE /{camera_id}/live to prevent the
# parametric route from capturing the literal "hls" path segment.
@router.get("/hls/{camera_id}/{stream_type}/{date_hour}/{filename}")
async def serve_hls(
    camera_id: str, stream_type: str, date_hour: str, filename: str
):
    base = Path(settings.hls_base_dir).resolve()
    file_path = (base / camera_id / stream_type / date_hour / filename).resolve()
    if not file_path.is_relative_to(base) or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@router.get("/{camera_id}/live")
async def get_live_stream(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    out_dir = hls_manager.ensure_started(camera_id, stream_type)
    return {
        "url": f"/stream/hls/{camera_id}/{stream_type}/{out_dir.name}/index.m3u8"
    }


@router.get("/{camera_id}/vod")
async def get_vod_stream(camera_id: str, start: float = 0, end: float = 0):
    return {"status": "not implemented"}
