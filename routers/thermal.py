"""熱像對位參數的讀寫。

熱像與 RGB 是兩顆分開的鏡頭，同一隻豬在兩張圖上不會落在同一個位置。這裡存的
四個數字（見 thermal_align）同時決定兩件事：熱像畫面上框畫在哪，以及那隻豬的
體溫從熱像的哪一塊取樣。所以這不只是視覺調整，存錯會讓體溫也一起錯。

驗證比照 routers/masks：這條路徑會改到寫進 DB 的體溫數值，寧可擋下來。
"""
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
import thermal_align
from db_writer import query_thermal_aligns, upsert_thermal_align
from inference.pipeline import inference_pipeline

router = APIRouter(prefix="/thermal-align", tags=["thermal"])


class AlignPayload(BaseModel):
    off_x: float = 0.0
    off_y: float = 0.0
    scale_x: float = 1.0
    scale_y: float = 1.0


@router.get("/{camera_id}")
async def get_align(camera_id: str):
    """DB 不可用時回 pipeline 記憶中的值，而不是 503。

    校正參數是唯讀就能用的東西，DB 掛掉時讓前端至少畫得對，比整個面板變成
    錯誤訊息有用。"""
    pool = database.get_pool()
    if pool is None:
        return {"camera_id": camera_id,
                "align": inference_pipeline.get_thermal_align(camera_id)}
    try:
        aligns = await query_thermal_aligns(pool, camera_id=camera_id)
    except Exception:
        return {"camera_id": camera_id,
                "align": inference_pipeline.get_thermal_align(camera_id)}
    return {"camera_id": camera_id,
            "align": thermal_align.normalize(aligns.get(camera_id))}


@router.put("/{camera_id}")
async def put_align(camera_id: str, payload: AlignPayload):
    align, err = thermal_align.validate(payload.model_dump())
    if err:
        raise HTTPException(status_code=400, detail=err)
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    await upsert_thermal_align(pool, camera_id, align)
    # 推進 pipeline 而不是讓它輪詢：pipeline 在自己的 thread，查不了 async pool。
    # 照 routers/masks 的先例。
    inference_pipeline.set_thermal_align(camera_id, align)
    return {"ok": True, "align": align}
