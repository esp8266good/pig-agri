from typing import Optional

from fastapi import APIRouter

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def get_alerts(
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
):
    return {"status": "not implemented"}


@router.put("/{alert_id}/read")
async def mark_alert_read(alert_id: int):
    return {"status": "not implemented"}
