import time
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import database
import presence
from alert_grouping import fold_alerts, page_groups
from config import settings as app_settings
from focus_list import select_focus
from analysis.scheduler import get_anomaly_cache, get_camera_state
from db_writer import (
    count_unread_alerts,
    get_all_settings,
    delete_alert,
    delete_alerts_bulk,
    delete_alerts_by_ids,
    mark_alert_read,
    mark_alerts_read,
    query_health_alerts,
)

router = APIRouter(prefix="/alerts", tags=["alerts"])

# 每次向 DB 要多少「原始」告警。折疊率無法預先知道，所以這是一個批次大小
# 而不是一頁的大小；router 會一直抓到湊滿一頁折疊結果為止。
RAW_BATCH = 200

# 抓取原始列的總量上限。折疊率極高時（同一隻豬連續告警幾千筆）若不設上限，
# 湊滿一頁可能要掃完整張表。撞到上限就回目前湊到的，寧可少一頁也不要卡住請求。
RAW_BUDGET = 5000


class AlertIds(BaseModel):
    ids: list[int]


@router.get("/active")
async def get_active_alerts(camera_id: Optional[str] = None):
    cache = get_anomaly_cache()
    if camera_id is not None:
        return {"cache": {camera_id: {str(k): v for k, v in cache.get(camera_id, {}).items()}}}
    return {"cache": {cam: {str(k): v for k, v in objs.items()} for cam, objs in cache.items()}}


def _as_bool(v, default: bool) -> bool:
    if v is None:
        return default
    return str(v).strip().lower() == "true"


def _as_int(v, default: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


@router.get("/focus")
async def get_focus_list(camera_id: str):
    """關注清單：現在該去看哪幾隻豬。

    資料來源是 scheduler 的異常快取，跟豬隻狀態表格同源，所以兩邊的活動量一致。
    DB 不可用時退回 app_settings 的預設值，清單少幾隻總比整個掛掉好。
    """
    cache = get_anomaly_cache().get(camera_id, {})
    now = time.time()
    seen = presence.last_seen_map(camera_id)
    # presence 一筆都沒有＝這台相機從 app 啟動到現在沒送過任何偵測（剛開機、
    # 相機斷線、夜間全黑）。這時給一個空集合會讓清單一律空掉，分不出「沒有豬
    # 需要注意」與「還不知道」，所以退回舊行為：把每一筆都當成在畫面上。
    on_screen = presence.on_screen_ids(camera_id, now=now) if seen else None
    gone = {oid: max(0.0, now - ts) for oid, ts in seen.items()}
    pool = database.get_pool()
    if pool is None:
        db = {}
    else:
        try:
            db = await get_all_settings(pool)
        except Exception:
            db = {}
    result = select_focus(
        cache,
        lowest_enabled=_as_bool(db.get("focus_lowest_enabled"),
                                app_settings.focus_lowest_enabled),
        lowest_n=_as_int(db.get("focus_lowest_n"), app_settings.focus_lowest_n),
        top_n=_as_int(db.get("focus_top_n"), app_settings.focus_top_n),
        camera_state=get_camera_state().get(camera_id),
        on_screen=on_screen,
        gone_seconds=gone,
    )
    return {"camera_id": camera_id, **result}


@router.get("/count")
async def get_unread_count(camera_id: Optional[str] = None):
    """未讀數。前端 badge 專用，不受清單分頁影響。"""
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    n = await count_unread_alerts(pool, camera_id=camera_id)
    return {"unread": n}


@router.get("")
async def get_alerts(
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    before_ts: Optional[float] = None,
    before_id: Optional[int] = None,
):
    """回傳折疊後的告警群組，一頁 `limit` 條。

    折疊率事先不知道，所以這裡是一個迴圈：抓一批原始列、折疊、還不夠就往回再抓。
    停止條件是「折出 limit + 1 個群組」而不是 limit，因為要看到第 limit + 1 個群組
    開頭，才能確定第 limit 個群組已經收攏完畢；否則同一個群組會被切到兩頁，
    在使用者眼裡就是同一條通知重複出現。
    """
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")

    collected: list[dict] = []
    groups: list[dict] = []
    cur_ts, cur_id = before_ts, before_id

    while True:
        rows = await query_health_alerts(
            pool,
            camera_id=camera_id,
            unread_only=unread_only,
            limit=RAW_BATCH,
            start_ts=start_ts,
            end_ts=end_ts,
            before_ts=cur_ts,
            before_id=cur_id,
        )
        collected.extend(rows)
        groups = fold_alerts(collected)
        if len(rows) < RAW_BATCH:          # DB 裡沒有更舊的了
            break
        if len(groups) > limit:            # 第 limit 個群組已經收攏
            break
        if len(collected) >= RAW_BUDGET:   # 掃太多了，就這樣回
            break
        cur_ts = rows[-1]["triggered_at_unix"]
        cur_id = rows[-1]["id"]

    # cursor 的正確性見 alert_grouping.page_groups：不能只看最後一個群組，
    # 長跨距的群組會讓同一筆告警在兩頁都出現。
    page, cursor = page_groups(groups, limit)
    return {
        "alerts": page,
        "total": len(page),
        "has_more": cursor is not None,
        "next_before_ts": cursor[0] if cursor else None,
        "next_before_id": cursor[1] if cursor else None,
    }


@router.put("/read")
async def mark_read_many(payload: AlertIds):
    """把一整個折疊群組的成員全部標為已讀。

    只標最新那筆的話，底下的成員仍然未讀，badge 會留下一個清不掉的紅點。
    """
    if not payload.ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    n = await mark_alerts_read(pool, payload.ids)
    return {"updated": n}


@router.delete("/by-ids")
async def delete_many(payload: AlertIds):
    """刪掉一整個折疊群組。理由同 mark_read_many。"""
    if not payload.ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    n = await delete_alerts_by_ids(pool, payload.ids)
    return {"deleted": n}


@router.put("/{alert_id}/read")
async def mark_read(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    found = await mark_alert_read(pool, alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}


@router.delete("/{alert_id}")
async def delete_one(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    found = await delete_alert(pool, alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}


@router.delete("")
async def delete_bulk(read_only: bool = True, camera_id: Optional[str] = None):
    """批量刪除 health_alerts。read_only 預設 True(只刪已讀)是保險,避免
    誤刪未處理的警示;camera_id 可選用於 narrow 到單一攝影機。"""
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    n = await delete_alerts_bulk(pool, read_only=read_only, camera_id=camera_id)
    return {"deleted": n}
