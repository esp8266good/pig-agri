"""每台相機「最後一次看到某個 object_id」的時刻。

生命週期是「秒」：寫入端是 inference/pipeline 的每一幀，讀取端是 /alerts/focus。
⚠ 不要跟 analysis.scheduler 的 _anomaly_cache 合併。那個 cache 的 last_seen
一輪分析才更新一次（預設 30 分鐘），拿它回答「這隻豬離開畫面幾秒了」會差兩個
數量級；兩份資料放在一起遲早有人拿錯解析度的那個。

也不能讓前端自己記：bboxHistory 是 1000 筆的上限，而且重整頁面就歸零，
使用者一按 F5「最近消失」就無從算起。
"""
import time

# 「在畫面上」的容忍時間。豬被另一隻擋住半秒就從清單閃掉再閃回來，看起來像壞的。
# 不往上頂到 30 秒是因為 tracker 的 max_age 就在那個尺度（300 幀 ≈ 30 秒）：
# 清單比 tracker 還晚放手，等於把 tracker 已經放棄的編號留在畫面上。
DEFAULT_HOLD_SECONDS = 10.0

# 超過這個時間沒再出現的編號直接忘掉。「最近消失」只回頭看 10 分鐘，
# 留一小時是給時鐘抖動與重啟留餘裕，不是拿來查歷史的（歷史在 tracking_logs）。
RETENTION_SECONDS = 3600.0

_last_seen: dict[str, dict[int, float]] = {}


def mark_seen(camera_id: str, object_ids, ts: float) -> None:
    """這一幀在 camera_id 上看到了這些 object_id。ts 用擷取時間（capture_ts）。"""
    cam = _last_seen.setdefault(camera_id, {})
    for oid in object_ids:
        cam[int(oid)] = ts
    cutoff = ts - RETENTION_SECONDS
    if any(v < cutoff for v in cam.values()):
        for oid in [oid for oid, v in cam.items() if v < cutoff]:
            del cam[oid]


def last_seen_map(camera_id: str) -> dict[int, float]:
    """object_id → 最後一次被看到的時刻。回傳的是複本，呼叫端改它不影響本模組。"""
    return dict(_last_seen.get(camera_id, {}))


def on_screen_ids(
    camera_id: str,
    now: float | None = None,
    hold_seconds: float = DEFAULT_HOLD_SECONDS,
) -> set[int]:
    """現在算在畫面上的 object_id。"""
    if now is None:
        now = time.time()
    cam = _last_seen.get(camera_id, {})
    return {oid for oid, ts in cam.items() if now - ts <= hold_seconds}


def gone_seconds(camera_id: str, object_id: int, now: float | None = None):
    """離開畫面多久（秒）。從沒看過這個編號回 None。

    夾在 0 以上：擷取時間與伺服器時鐘之間有幾十毫秒的差是正常的，
    讓「離開 -0.3 秒」這種數字流到前端只會讓人以為壞了。
    """
    if now is None:
        now = time.time()
    ts = _last_seen.get(camera_id, {}).get(int(object_id))
    if ts is None:
        return None
    return max(0.0, now - ts)


def clear(camera_id: str | None = None) -> None:
    """測試與相機重連用。不給 camera_id 就整份清掉。"""
    if camera_id is None:
        _last_seen.clear()
    else:
        _last_seen.pop(camera_id, None)
