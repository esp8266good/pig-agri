from typing import Optional

from fastapi import APIRouter, HTTPException

import database
from analysis.scheduler import get_anomaly_cache
from db_writer import mark_alert_read, query_health_alerts

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("/active")
async def get_active_alerts(camera_id: Optional[str] = None):
    cache = get_anomaly_cache()
    if camera_id is not None:
        return {"cache": {camera_id: {str(k): v for k, v in cache.get(camera_id, {}).items()}}}
    return {"cache": {cam: {str(k): v for k, v in objs.items()} for cam, objs in cache.items()}}


@router.get("")
async def get_alerts(
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    alerts = await query_health_alerts(
        pool,
        camera_id=camera_id,
        unread_only=unread_only,
        limit=limit,
        start_ts=start_ts,
        end_ts=end_ts,
    )
    return {"alerts": alerts, "total": len(alerts)}


@router.put("/{alert_id}/read")
async def mark_read(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    found = await mark_alert_read(pool, alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}
