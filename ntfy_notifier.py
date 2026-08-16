"""ntfy 推播純傳輸模組。

不讀 config、不決定 policy（要推哪些事件由 main 決定）——只負責「把一則訊息
發佈到給定的 ntfy topic url」，並保證絕不拋例外、絕不阻塞事件迴圈（timeout）。

採用 ntfy 的 **JSON 發佈**（POST JSON 到 server base、topic 放 body）而非 header
方式：title/message 含中文+emoji 時，HTTP header 只能是 latin-1，httpx 會對非
latin-1 的 header 值拋例外（'ascii' codec can't encode）→ 推播全失敗。JSON body
為 UTF-8，unicode 完全沒問題。
"""
import socket

import httpx
from loguru import logger

# ntfy priority 字串 → 整數（JSON 發佈用整數；1=min … 5=max/urgent）。
_PRIORITY_MAP: dict[str, int] = {
    "min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5,
}

# 主機名放在標題最前面。ed716 有 pig 與 swine 兩個訂閱點，同一個 topic 也可能
# 被多台機器共用（遷移期間新舊機會同時在跑），標題不帶機器名就分不出是誰在叫。
# 放前面而不是後面：手機通知的標題是從尾巴截斷的，擺後面會第一個被吃掉。
#
# 這個模組的原則是「不決定 policy」，唯獨這件事破例——它是傳送端的身分，
# 不是事件內容；擺在這個唯一的出口，將來新增呼叫端也不可能忘記加。
_HOSTNAME = socket.gethostname()


def _with_host(title: str) -> str:
    return f"[{_HOSTNAME}] {title}" if _HOSTNAME else title


def _split_topic_url(url: str) -> "tuple[str, str] | None":
    """topic url（如 https://host/pig）→ (base, topic)=(https://host, pig)。
    無法切出 topic（結尾無路徑段）→ None。"""
    base, sep, topic = url.rstrip("/").rpartition("/")
    if not sep or not topic or not base:
        return None
    return base, topic


async def notify(url: str, title: str, message: str, *,
                 priority: str = "default", tags: str = "") -> bool:
    """發佈一則 ntfy 通知。url 空 / 無法切出 topic → no-op 回 False。成功回 True，
    任何網路/逾時錯誤只 log warning 並回 False（呼叫端不需處理例外）。"""
    if not url:
        return False
    parts = _split_topic_url(url)
    if parts is None:
        logger.warning(f"ntfy url 無法解析出 topic：{url}")
        return False
    base, topic = parts
    payload: dict = {
        "topic": topic,
        "title": _with_host(title),
        "message": message,
        "priority": _PRIORITY_MAP.get(priority, 3),
    }
    if tags:
        payload["tags"] = [t for t in tags.split(",") if t]
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(base, json=payload)
        if resp.status_code >= 400:
            logger.warning(f"ntfy notify 回 {resp.status_code}")
            return False
        return True
    except Exception as e:
        logger.warning(f"ntfy notify 失敗：{e}")
        return False
