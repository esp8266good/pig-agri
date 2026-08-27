"""從異常快取挑出「現在該去看哪幾隻豬」。

關注清單有三種標籤（定義見 CONTEXT.md）：
  anomaly   後端狀態機判定為 alerted 的豬
  lowest    活動量排名最後 N 名，只在該相機沒有任何異常時才出現
  reference 活動量排名最前 M 名，給人眼一個「正常長什麼樣」的參考點

清單只放**現在畫面上**的 object_id。理由是使用者拿這份清單的下一個動作是走進
豬舍找那隻豬，而 MOT 的編號會跳號：一小時的分析視窗裡有 80 個編號，同一瞬間
畫面上只有約 30 個（cam_03 實測）。剩下那 50 個點下去畫面上一個框都不會亮，
在使用者眼裡跟「系統壞了」沒有分別。

離開畫面的異常退到 recent（「最近消失」）。它不是採血名單：採血的權威紀錄是
health_alerts，不會因為編號跳號而消失。

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


def _item(
    object_id: int, entry: dict, label: str,
    *, on_screen: bool = True, gone: Optional[float] = None,
) -> dict:
    return {
        "object_id": object_id,
        "label": label,
        "activity": entry.get("activity_current"),
        "activity_anomaly": bool(entry.get("activity_anomaly")),
        "temp_anomaly": bool(entry.get("temp_anomaly")),
        "on_screen": on_screen,
        # 離開畫面幾秒。on_screen 為真時是 None（沒有意義）。
        "gone_seconds": gone,
    }


def _by_activity(pairs: list[tuple[int, dict]], descending: bool):
    """活動量是 None 的豬沒有評估依據，排序時一律沉底、不參與排名。"""
    ranked = [(oid, e) for oid, e in pairs if e.get("activity_current") is not None]
    ranked.sort(key=lambda p: p[1]["activity_current"], reverse=descending)
    return ranked


# 「最近消失」回頭看多久、最多列幾個。列太多就不是「最近」而是一份歷史，
# 而歷史該去通知分頁看。
RECENT_GONE_SECONDS = 600.0
RECENT_GONE_MAX = 5


def select_focus(
    entries: dict[int, dict],
    *,
    lowest_enabled: bool,
    lowest_n: int,
    top_n: int,
    camera_state: Optional[dict] = None,
    on_screen: Optional[set] = None,
    gone_seconds: Optional[dict] = None,
    recent_gone_seconds: float = RECENT_GONE_SECONDS,
    recent_gone_max: int = RECENT_GONE_MAX,
) -> dict:
    """entries 是單一 camera 的 anomaly cache：object_id → entry。

    回傳 {"status", "items", "recent", "on_screen_count"}。items 已經照顯示順序
    排好：異常（活動量升序）→ 最低（升序）→ 對照（降序），而且只含現在畫面上的豬。

    on_screen 是現在畫面上的 object_id 集合（來自 presence），gone_seconds 是
    object_id → 離開畫面幾秒。兩者都給 None 時退回舊行為：把每一筆都當成在畫面
    上。這條路徑留給 presence 還沒有資料的情況（app 剛起來、相機剛接上），
    寧可多列幾隻也不要在冷啟動時給一份空清單。

    camera_state 是 scheduler 每輪寫下的 per-camera 結論（analyzed / herd_ok）。
    entries 會被清空——MOT 的 ID 跳號讓舊 object_id 永遠不再出現，scheduler 會
    把它們逐出，夜間更是整台相機一筆都不剩。這時候光看 entries 分不出「分析過、
    但全欄都在休息」與「還沒分析過」，兩種都會變成「目前沒有需要注意的豬」，
    把一個保護講成一份保證。有 camera_state 就照它說的講。
    """
    all_pairs = list(entries.items())
    if on_screen is None:
        pairs = all_pairs
        off_pairs: list[tuple[int, dict]] = []
    else:
        pairs = [(oid, e) for oid, e in all_pairs if oid in on_screen]
        off_pairs = [(oid, e) for oid, e in all_pairs if oid not in on_screen]

    def _is_anomaly(e: dict) -> bool:
        return bool(e.get("activity_anomaly") or e.get("temp_anomaly"))

    anomalies = [(oid, e) for oid, e in pairs if _is_anomaly(e)]
    # herd_low 的判斷要看「有沒有還沒解除的舊警報」，離開畫面的那些也算。
    any_anomaly = anomalies or [(oid, e) for oid, e in off_pairs if _is_anomaly(e)]

    # 最近消失：只收異常。lowest 與 reference 是排名，編號一死就沒有意義了。
    gone_map = gone_seconds or {}
    recent = []
    for oid, e in off_pairs:
        if not _is_anomaly(e):
            continue
        g = gone_map.get(oid)
        if g is None or g > recent_gone_seconds:
            continue
        recent.append(_item(oid, e, LABEL_ANOMALY, on_screen=False, gone=g))
    recent.sort(key=lambda i: i["gone_seconds"])
    recent = recent[:recent_gone_max]
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
        # ⚠ 這裡要看 all_pairs 不是 pairs：herd_ok 與 analyzed 是「這台相機」的
        # 結論，只是借 per-object 的 entry 存放。用過濾後的 pairs 的話，重啟後
        # cache 裡全是上一代的舊編號、一個都不在畫面上，pairs 會是空的，
        # `if pairs else True` 就把 analyzed 判成 True，"首次分析尚未完成" 這個
        # 保護整個失效。
        herd_ok = any(e.get("herd_ok") for _, e in all_pairs) if all_pairs else True
        analyzed = any(e.get("analyzed") for _, e in all_pairs) if all_pairs else True

    # 「畫面上有幾隻豬」問的是 presence，不是「快取裡有幾隻在畫面上」。
    # 用後者的話，重啟後畫面上明明有 21 隻、卻因為一個都還沒進快取而回 0，
    # 前端就會說「畫面上沒有偵測到豬」——正好是這兩句話要避免的那種指錯方向。
    on_screen_count = len(on_screen) if on_screen is not None else len(all_pairs)

    if not analyzed:
        return {"status": STATUS_NOT_ANALYZED, "items": [], "recent": [],
                "on_screen_count": on_screen_count}

    if not any_anomaly and not herd_ok:
        # 沒有評估依據，也沒有還沒解除的舊警報：整份清單讓位給狀態訊息。
        # 對照組一併不顯示——它存在的目的是當作被標記那些豬的參考點，
        # 沒有東西被標記時它沒有工作可做。
        return {"status": STATUS_HERD_LOW, "items": [], "recent": [],
                "on_screen_count": on_screen_count}

    items = [_item(oid, e, LABEL_ANOMALY) for oid, e in anomalies]
    taken = {oid for oid, _ in anomalies}

    if not any_anomaly and lowest_enabled and lowest_n > 0:
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

    return {"status": STATUS_OK, "items": items, "recent": recent,
            "on_screen_count": on_screen_count}
