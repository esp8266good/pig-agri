"""從異常快取挑出「現在該去看哪幾隻豬」。

關注清單有三種標籤（定義見 CONTEXT.md）：
  anomaly   後端狀態機判定為 alerted 的豬
  lowest    活動量排名最後 N 名，只在該相機沒有任何異常時才出現
  reference 活動量排名最前 M 名，給人眼一個「正常長什麼樣」的參考點

挑選邏輯放在後端而不是前端，是為了讓它被 pytest 釘住：
前端沒有測試框架，純函式寫在 JS 只驗得到語法。
"""
from typing import Optional

LABEL_ANOMALY = "anomaly"
LABEL_LOWEST = "lowest"
LABEL_REFERENCE = "reference"

# 全欄活動量普遍偏低（scheduler 的 herd_ok=false）時的狀態碼。
# 這是既有的夜間保護：整欄不做異常判定。關注清單跟著不給名字，
# 否則等於把那個保護作廢。
STATUS_OK = "ok"
STATUS_HERD_LOW = "herd_low"
# 重啟後到第一輪分析完成之間。這段期間 herd_ok 還是預設的 False，
# 不分開講的話會誤報成「豬群活動量普遍偏低」，把使用者指向錯誤的結論。
STATUS_NOT_ANALYZED = "not_analyzed"


def _item(object_id: int, entry: dict, label: str) -> dict:
    return {
        "object_id": object_id,
        "label": label,
        "activity": entry.get("activity_current"),
        "activity_anomaly": bool(entry.get("activity_anomaly")),
        "temp_anomaly": bool(entry.get("temp_anomaly")),
    }


def _by_activity(pairs: list[tuple[int, dict]], descending: bool):
    """活動量是 None 的豬沒有評估依據，排序時一律沉底、不參與排名。"""
    ranked = [(oid, e) for oid, e in pairs if e.get("activity_current") is not None]
    ranked.sort(key=lambda p: p[1]["activity_current"], reverse=descending)
    return ranked


def select_focus(
    entries: dict[int, dict],
    *,
    lowest_enabled: bool,
    lowest_n: int,
    top_n: int,
    camera_state: Optional[dict] = None,
) -> dict:
    """entries 是單一 camera 的 anomaly cache：object_id → entry。

    回傳 {"status": ..., "items": [...]}，items 已經照顯示順序排好：
    異常（活動量升序）→ 最低（升序）→ 對照（降序）。

    camera_state 是 scheduler 每輪寫下的 per-camera 結論（analyzed / herd_ok）。
    entries 會被清空——MOT 的 ID 跳號讓舊 object_id 永遠不再出現，scheduler 會
    把它們逐出，夜間更是整台相機一筆都不剩。這時候光看 entries 分不出「分析過、
    但全欄都在休息」與「還沒分析過」，兩種都會變成「目前沒有需要注意的豬」，
    把一個保護講成一份保證。有 camera_state 就照它說的講。
    """
    pairs = list(entries.items())
    anomalies = [
        (oid, e) for oid, e in pairs
        if e.get("activity_anomaly") or e.get("temp_anomaly")
    ]
    # 異常清單按活動量升序，最需要先看的排最前面；沒有活動量的沉底。
    anomalies.sort(
        key=lambda p: (p[1].get("activity_current") is None,
                       p[1].get("activity_current") or 0.0)
    )

    # herd_ok / analyzed 優先聽 camera_state（權威來源）。沒有它時退回舊做法：
    # 從 per-object 的 entry 裡撈——那是 camera_state 出現之前的形狀，
    # 現有測試與 DB 不可用的路徑都還走這條。
    if camera_state:
        herd_ok = bool(camera_state.get("herd_ok"))
        analyzed = bool(camera_state.get("analyzed"))
    else:
        herd_ok = any(e.get("herd_ok") for _, e in pairs) if pairs else True
        analyzed = any(e.get("analyzed") for _, e in pairs) if pairs else True

    if not analyzed:
        return {"status": STATUS_NOT_ANALYZED, "items": []}

    if not anomalies and not herd_ok:
        # 沒有評估依據，也沒有還沒解除的舊警報：整份清單讓位給狀態訊息。
        # 對照組一併不顯示——它存在的目的是當作被標記那些豬的參考點，
        # 沒有東西被標記時它沒有工作可做。
        return {"status": STATUS_HERD_LOW, "items": []}

    items = [_item(oid, e, LABEL_ANOMALY) for oid, e in anomalies]
    taken = {oid for oid, _ in anomalies}

    if not anomalies and lowest_enabled and lowest_n > 0:
        for oid, e in _by_activity(pairs, descending=False)[:lowest_n]:
            items.append(_item(oid, e, LABEL_LOWEST))
            taken.add(oid)

    # 對照組只在全欄活動正常時才有意義：從一群都在休息的豬裡挑「最活潑的」，
    # 拿來當「正常長什麼樣」的參考點只會誤導。herd_ok 為假但仍有未解除的舊警報時，
    # 清單只列那些警報，不附對照。
    if top_n > 0 and herd_ok:
        for oid, e in _by_activity(pairs, descending=True):
            if len([i for i in items if i["label"] == LABEL_REFERENCE]) >= top_n:
                break
            if oid in taken:      # 豬太少時 top-N 會撞到已列出的豬
                continue
            items.append(_item(oid, e, LABEL_REFERENCE))
            taken.add(oid)

    return {"status": STATUS_OK, "items": items}
