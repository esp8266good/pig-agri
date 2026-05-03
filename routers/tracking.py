from typing import Optional

from fastapi import APIRouter

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/{camera_id}")
async def get_tracking(
    camera_id: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    object_id: Optional[int] = None,
):
    return {"status": "not implemented"}
