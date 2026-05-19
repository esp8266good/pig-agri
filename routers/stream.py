from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse

from config import settings
from hls_manager import hls_manager
from vod_generator import build_vod_m3u8

router = APIRouter(prefix="/stream", tags=["stream"])


# /hls/... must be defined BEFORE /{camera_id}/live to prevent the
# parametric route from capturing the literal "hls" path segment.
@router.get("/hls/{camera_id}/{stream_type}/{date_hour}/{filename}")
async def serve_hls(
    camera_id: str, stream_type: str, date_hour: str, filename: str
):
    # live playlist：回傳後端自管 PDT（真實擷取時間）的版本，前端
    # hls.playingDate 才會 ≡ 真實時間、bbox 零漂移。非當前小時 / 無
    # 對應 stream → corrected 回 None，落回服務磁碟檔（歷史/已輪替）。
    if filename == "index.m3u8":
        corrected = hls_manager.corrected_m3u8(camera_id, stream_type, date_hour)
        if corrected is not None:
            return PlainTextResponse(
                corrected, media_type="application/vnd.apple.mpegurl"
            )
    base = Path(settings.hls_base_dir).resolve()
    file_path = (base / camera_id / stream_type / date_hour / filename).resolve()
    if not file_path.is_relative_to(base) or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@router.get("/{camera_id}/timeline")
async def get_timeline(
    camera_id: str,
    start_ts: float,
    end_ts: float,
):
    from datetime import datetime
    base = Path(settings.hls_base_dir)
    rgb_dir = base / camera_id / "rgb"
    if not rgb_dir.exists():
        return {"hours": []}

    hours: list[int] = []
    for entry in rgb_dir.iterdir():
        if not entry.is_dir():
            continue
        try:
            dt = datetime.strptime(entry.name, "%Y-%m-%d-%H")
        except ValueError:
            continue
        ts = int(dt.timestamp())
        if start_ts <= ts < end_ts:
            hours.append(ts)

    return {"hours": sorted(hours)}


@router.get("/{camera_id}/live")
async def get_live_stream(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in [s.label for s in settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")
    out_dir = hls_manager.ensure_started(camera_id, stream_type)
    return {"url": f"/stream/hls/{camera_id}/{stream_type}/{out_dir.name}/index.m3u8"}


@router.get("/{camera_id}/vod")
async def get_vod_stream(
    camera_id: str,
    start: float,
    end: float,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in [s.label for s in settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")
    m3u8 = build_vod_m3u8(camera_id, stream_type, start, end)
    if m3u8 is None:
        raise HTTPException(status_code=404, detail="No segments found for this time range")
    return PlainTextResponse(m3u8, media_type="application/vnd.apple.mpegurl")
