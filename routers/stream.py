from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse, Response

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
    # live playlist：回傳後端自管 PDT 版本（非當前小時/無 stream → None → fallback）。
    if filename == "index.m3u8":
        corrected = hls_manager.corrected_m3u8(camera_id, stream_type, date_hour)
        if corrected is not None:
            return PlainTextResponse(
                corrected, media_type="application/vnd.apple.mpegurl"
            )
    # 先試 active stream 的當前 out_dir（含 ephemeral /dev/shm）；命中 date_hour 才用。
    active_dir = hls_manager.active_out_dir(camera_id, stream_type, date_hour)
    if active_dir is not None:
        ad = active_dir.resolve()
        fp = (ad / filename).resolve()
        if fp.is_relative_to(ad) and fp.exists():
            return FileResponse(fp)
    # fallback：歷史錄影一律在 hls_base_dir。
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


@router.get("/{camera_id}/snapshot")
async def get_snapshot(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    """這台相機此刻最新的一張 JPEG，給熱像對位校正當底圖用。

    只取 live，不支援「回放中的某個時間點」。鏡頭是鎖死的，所以今天的 live RGB
    跟上週的熱像錄影拍到的是同一片固定背景；校正的是一組幾何關係，兩張圖不必
    同時間。要支援任意時間點就得為單一幀另開一支 ffmpeg 去解 .ts（VOD 目前只是
    把既有 segment 列成 m3u8，整條路徑上沒有任何解碼），代價與收益不成比例。

    ⛔ 它會失效的情況只有兩種：相機此刻斷線，或現在是夜間（rgb 全黑）。
    兩種都回得出「這招現在沒用」——404 或一張黑圖——比回一張舊畫面好。
    """
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    if camera_id not in [s.label for s in settings.zmq_sources]:
        raise HTTPException(status_code=404, detail="Camera not found")
    frame = hls_manager.latest_frame(camera_id, stream_type)
    if frame is None:
        raise HTTPException(status_code=404, detail="No recent frame for this camera")
    # 不快取：這是「現在」的畫面，隔一秒重抓要拿到新的。
    return Response(content=frame, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


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
