"""把連續的同源告警折成一條。

一隻豬活動量偏低時，狀態機每輪都可能再寫一筆 health_alerts；通知清單如果照單全收，
同一件事會洗掉整頁。折疊把「同一台相機、同一隻豬、同一種指標」且時間上連續的告警
合併成一條，標示發生次數。

時間上分得夠開的告警刻意不折：採血決策關心的是「這隻豬今天是不是又低了」，
把跨天的告警折成一條會直接抹掉這個訊號。
"""
from typing import Optional

# 間隔小於這個秒數的同源告警視為連續。6 小時是「同一段持續異常」與
# 「今天又發生一次」的分界。刻意不做成可調設定：它沒有人有直覺，
# 開放出來只會被亂調然後回報「通知壞了」。
FOLD_GAP_SECONDS: float = 6 * 3600.0

# 折疊群組沿用最新那一筆告警的這些欄位。current_value 之類取最新的，
# 因為使用者要看的是「現在多低」，不是六小時前多低。
_CARRIED_FIELDS = (
    "id", "camera_id", "object_id", "metric",
    "current_value", "mean_value", "std_value",
)


def _key(row: dict) -> tuple:
    return (row["camera_id"], row["object_id"], row["metric"])


def fold_alerts(
    rows: list[dict],
    gap_seconds: float = FOLD_GAP_SECONDS,
) -> list[dict]:
    """rows 必須依 triggered_at_unix 遞減排序（呼叫端負責）。

    回傳的群組維持同樣的遞減順序，每個群組帶：
      - 最新那筆的識別與數值欄位（`id` 供標記已讀／刪除使用）
      - `triggered_at_unix`：群組內最新的時間
      - `first_triggered_at_unix`：群組內最舊的時間，分頁 cursor 用這個
      - `count` 與 `alert_ids`（新→舊）
      - `is_read`：全部成員都已讀才算已讀
    """
    groups: list[dict] = []
    # key → 目前還「開著」的群組。rows 是遞減的，所以每個 key 只會有一個群組
    # 在等著被往更舊的方向延伸；一旦間隔超過門檻就換一個新的群組頂上。
    open_group: dict[tuple, dict] = {}

    for row in rows:
        k = _key(row)
        g = open_group.get(k)
        if g is not None and g["first_triggered_at_unix"] - row["triggered_at_unix"] < gap_seconds:
            g["count"] += 1
            g["alert_ids"].append(row["id"])
            g["first_triggered_at_unix"] = row["triggered_at_unix"]
            g["is_read"] = g["is_read"] and bool(row["is_read"])
            continue

        g = {f: row[f] for f in _CARRIED_FIELDS}
        g["is_read"] = bool(row["is_read"])
        g["triggered_at_unix"] = row["triggered_at_unix"]
        g["first_triggered_at_unix"] = row["triggered_at_unix"]
        g["count"] = 1
        g["alert_ids"] = [row["id"]]
        groups.append(g)
        open_group[k] = g

    return groups


def fold_cursor(group: Optional[dict]) -> Optional[tuple[float, int]]:
    """一個群組的 keyset cursor：它最舊的那筆告警的 (時間, id)。

    下一頁從嚴格早於這個位置的地方接續，所以群組不會被切成兩頁。
    """
    if group is None:
        return None
    return (group["first_triggered_at_unix"], group["alert_ids"][-1])
