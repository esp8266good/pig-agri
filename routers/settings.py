from fastapi import APIRouter, Request

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings():
    return {"status": "not implemented"}


@router.put("")
async def update_settings(request: Request):
    return {"status": "not implemented"}
