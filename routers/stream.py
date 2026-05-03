from fastapi import APIRouter

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/{camera_id}/live")
async def get_live_stream(camera_id: str):
    return {"status": "not implemented"}


@router.get("/{camera_id}/vod")
async def get_vod_stream(camera_id: str, start: float = 0, end: float = 0):
    return {"status": "not implemented"}


@router.get("/hls/{camera_id}/{path:path}")
async def serve_hls(camera_id: str, path: str):
    return {"status": "not implemented"}
