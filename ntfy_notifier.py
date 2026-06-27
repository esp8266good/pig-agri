"""ntfy 推播純傳輸模組。

不讀 config、不決定 policy（要推哪些事件由 main 決定）——只負責「把一則訊息
POST 到給定的 ntfy url」，並保證絕不拋例外、絕不阻塞事件迴圈（timeout）。
"""
import httpx
from loguru import logger


async def notify(url: str, title: str, message: str, *,
                 priority: str = "default", tags: str = "") -> bool:
    """POST 一則 ntfy 通知。url 空 → no-op 回 False。成功回 True，
    任何網路/逾時錯誤只 log warning 並回 False（呼叫端不需處理例外）。"""
    if not url:
        return False
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(
                url, content=message.encode("utf-8"), headers=headers
            )
        if resp.status_code >= 400:
            logger.warning(f"ntfy notify 回 {resp.status_code}")
            return False
        return True
    except Exception as e:
        logger.warning(f"ntfy notify 失敗：{e}")
        return False
