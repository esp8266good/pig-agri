from fastapi import APIRouter, HTTPException, Request

import database
from config import settings as app_settings
from db_writer import get_all_settings, upsert_settings

router = APIRouter(prefix="/settings", tags=["settings"])

ALLOWED_KEYS = frozenset({
    "jpeg_quality",
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
})


@router.get("")
async def get_settings():
    pool = database.get_pool()
    if pool is None:
        return {
            "jpeg_quality":              str(app_settings.jpeg_quality),
            "analysis_interval_minutes": str(app_settings.analysis_interval_minutes),
            "anomaly_std_threshold":     str(app_settings.anomaly_std_threshold),
            "hls_retention_days":        str(app_settings.hls_retention_days),
        }
    return await get_all_settings(pool)


@router.put("")
async def update_settings(request: Request, body: dict[str, str]):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    if "analysis_interval_minutes" in updates or "anomaly_std_threshold" in updates:
        current = await get_all_settings(pool)
        request.app.state.scheduler.reload(
            interval_minutes=int(current["analysis_interval_minutes"]),
            std_threshold=float(current["anomaly_std_threshold"]),
        )
    return {"ok": True, "updated": list(updates.keys())}
