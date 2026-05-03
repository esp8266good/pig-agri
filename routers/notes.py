from typing import Optional

from fastapi import APIRouter, Request

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post("")
async def create_note(request: Request):
    return {"status": "not implemented"}


@router.get("")
async def get_notes(
    camera_id: Optional[str] = None,
    start: Optional[float] = None,
    end: Optional[float] = None,
):
    return {"status": "not implemented"}
