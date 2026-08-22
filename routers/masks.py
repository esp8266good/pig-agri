"""遮罩區域的讀寫。

遮罩不改變影像，只讓與它重疊過多的偵測框被丟掉（見 mask_filter）。
因為它是唯一碰推論路徑的功能，這裡的驗證刻意寫嚴：座標超出畫面、頂點太少、
區域數量灌爆，任何一項漏掉都會直接影響偵測結果。
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
from db_writer import query_camera_masks, replace_camera_masks
from inference.pipeline import inference_pipeline

router = APIRouter(prefix="/masks", tags=["masks"])

# 上限存在的理由是擋灌爆，不是功能限制：每多一塊遮罩，每幀就多一次多邊形填色。
MAX_REGIONS = 20
MAX_VERTICES = 64


class MaskRegion(BaseModel):
    label: str = ""
    enabled: bool = True
    points: list


class MaskPayload(BaseModel):
    regions: list[MaskRegion]


def _validate(regions: list[MaskRegion]) -> list[dict]:
    if len(regions) > MAX_REGIONS:
        raise HTTPException(status_code=400,
                            detail=f"最多 {MAX_REGIONS} 塊遮罩")
    out = []
    for region in regions:
        pts = region.points
        if len(pts) < 3:
            raise HTTPException(status_code=400, detail="每塊遮罩至少要三個頂點")
        if len(pts) > MAX_VERTICES:
            raise HTTPException(status_code=400,
                                detail=f"每塊遮罩最多 {MAX_VERTICES} 個頂點")
        clean = []
        for pt in pts:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise HTTPException(status_code=400, detail="頂點格式應為 [x, y]")
            x, y = pt
            if isinstance(x, bool) or isinstance(y, bool) \
                    or not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
                raise HTTPException(status_code=400, detail="頂點座標必須是數字")
            # 座標是正規化的 0..1。超出範圍多半代表前端送了像素座標，
            # 靜靜夾住會讓遮罩畫在完全不對的位置，寧可擋下來。
            if not (0.0 <= x <= 1.0) or not (0.0 <= y <= 1.0):
                raise HTTPException(status_code=400, detail="頂點座標必須落在 0..1")
            clean.append([float(x), float(y)])
        out.append({"label": region.label[:64], "enabled": region.enabled,
                    "points": clean})
    return out


@router.get("/{camera_id}")
async def get_masks(camera_id: str):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    regions = await query_camera_masks(pool, camera_id=camera_id)
    return {"camera_id": camera_id, "regions": regions}


@router.put("/{camera_id}")
async def put_masks(camera_id: str, payload: MaskPayload):
    """整批覆蓋，並立刻推進 pipeline。

    推而不是讓 pipeline 自己輪詢 DB：pipeline 跑在自己的 thread、不是 async
    context，要查 DB 得另接一條橋；而寫入是一天幾次、讀取是每秒十次。
    照 routers/settings.py 的 scheduler.reload 先例。
    """
    regions = _validate(payload.regions)
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    await replace_camera_masks(pool, camera_id, regions)
    inference_pipeline.set_masks(camera_id, regions)
    return {"ok": True, "saved": len(regions)}
