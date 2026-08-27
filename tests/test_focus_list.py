"""關注清單的挑選規則。

挑選放在後端而不是前端，就是為了讓這些行為被真的測到：前端沒有測試框架，
純函式寫在 JS 只能靠 node --check 過語法。
設計理由見 docs/superpowers/specs/2026-08-22-focus-list-mask-notify-manual-design.md。
"""
from focus_list import select_focus


def _entry(activity, *, act_anom=False, temp_anom=False, herd_ok=True,
           analyzed=True):
    return {
        "activity_current": activity, "activity_mean": 10.0, "activity_std": 0.0,
        "activity_anomaly": act_anom, "temp_anomaly": temp_anom,
        "temp_current": None, "temp_mean": None, "temp_std": None,
        "herd_ok": herd_ok, "analyzed": analyzed,
    }


def _labels(result):
    return [(i["object_id"], i["label"]) for i in result["items"]]


def test_no_pigs_gives_empty_list():
    r = select_focus({}, lowest_enabled=True, lowest_n=3, top_n=3)
    assert r["items"] == []


def test_lowest_appears_only_when_no_anomaly():
    entries = {1: _entry(2.0), 2: _entry(5.0), 3: _entry(30.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0)
    assert _labels(r) == [(1, "lowest")]


def test_anomaly_replaces_lowest_entirely():
    """有異常時最低 N 完全消失：兩者幾乎必然重疊，同時顯示會讓人
    分不清哪些是系統真的在告警。"""
    entries = {1: _entry(2.0), 2: _entry(5.0), 3: _entry(30.0, act_anom=True)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=2, top_n=0)
    assert _labels(r) == [(3, "anomaly")]


def test_temp_anomaly_also_counts_as_anomaly():
    entries = {1: _entry(20.0, temp_anom=True), 2: _entry(2.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0)
    assert _labels(r) == [(1, "anomaly")]


def test_anomalies_sorted_by_activity_ascending():
    entries = {1: _entry(9.0, act_anom=True), 2: _entry(3.0, act_anom=True)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=0, top_n=0)
    assert _labels(r) == [(2, "anomaly"), (1, "anomaly")]


def test_reference_is_most_active_and_comes_last():
    entries = {1: _entry(2.0), 2: _entry(5.0), 3: _entry(30.0), 4: _entry(28.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=2)
    assert _labels(r) == [(1, "lowest"), (3, "reference"), (4, "reference")]


def test_top_n_zero_hides_reference():
    entries = {1: _entry(2.0), 2: _entry(30.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0)
    assert all(i["label"] != "reference" for i in r["items"])


def test_reference_never_duplicates_a_pig_already_listed():
    """豬太少時 top-N 會撞到已經列出來的豬，不能出現同一隻兩次。"""
    entries = {1: _entry(2.0), 2: _entry(5.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=2, top_n=2)
    oids = [i["object_id"] for i in r["items"]]
    assert len(oids) == len(set(oids))


def test_lowest_disabled_gives_no_lowest():
    entries = {1: _entry(2.0), 2: _entry(30.0)}
    r = select_focus(entries, lowest_enabled=False, lowest_n=3, top_n=0)
    assert r["items"] == []


def test_pigs_without_activity_are_not_ranked():
    """活動量是 None 的豬（軌跡跨度不足）沒有評估依據，不該被列進最低 N。"""
    entries = {1: _entry(None), 2: _entry(5.0), 3: _entry(30.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0)
    assert _labels(r) == [(2, "lowest")]


# ── 全欄活動量偏低 ───────────────────────────────────────────────────
# 這是 scheduler 既有的夜間保護（herd_ok=false 時整欄不做異常判定）。
# 關注清單如果照樣列出「最低 N」，等於把那個保護作廢：清單的意義是
# 「現在該去看哪幾隻豬」，在沒有評估依據時給名字就是給錯誤的指示。

def test_herd_low_suppresses_lowest_and_reference():
    entries = {1: _entry(0.4, herd_ok=False), 2: _entry(0.9, herd_ok=False)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=2, top_n=2)
    assert r["status"] == "herd_low"
    assert r["items"] == []


def test_herd_low_still_shows_existing_anomalies():
    """已經在 alerted 的豬不會因為全欄安靜就被藏起來：那筆採血警報還沒解除。"""
    entries = {1: _entry(0.4, herd_ok=False),
               2: _entry(0.2, act_anom=True, herd_ok=False)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=2, top_n=2)
    assert r["status"] == "ok"
    assert _labels(r) == [(2, "anomaly")]


def test_status_is_ok_when_herd_is_active():
    entries = {1: _entry(2.0), 2: _entry(30.0)}
    assert select_focus(entries, lowest_enabled=True, lowest_n=1,
                        top_n=1)["status"] == "ok"


# ── 重啟後尚未分析 ───────────────────────────────────────────────────
# scheduler 的 _loop 是先 sleep(interval) 再分析，所以重啟後最長要等一個
# analysis_interval（預設 30 分鐘）才有第一筆結果。這段期間 herd_ok 是預設的
# False，若不分開講就會誤報成「豬群活動量普遍偏低」，把使用者指向錯誤的結論。

def test_never_analyzed_is_reported_separately_from_herd_low():
    entries = {1: _entry(None, herd_ok=False, analyzed=False),
               2: _entry(None, herd_ok=False, analyzed=False)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=3, top_n=3)
    assert r["status"] == "not_analyzed"
    assert r["items"] == []


def test_one_analyzed_entry_is_enough_to_leave_the_not_analyzed_state():
    entries = {1: _entry(2.0, analyzed=True), 2: _entry(None, analyzed=False)}
    assert select_focus(entries, lowest_enabled=True, lowest_n=1,
                        top_n=0)["status"] == "ok"


# ── entries 被清空之後 ───────────────────────────────────────────────
# MOT 的 ID 會跳號：同一隻豬遮擋一次出來就換一個新號碼，舊號碼從此不再出現。
# scheduler 會把那些死掉的 object_id 逐出，夜間更是整台相機一筆都不剩。
# 這時光看 entries 分不出「分析過、但全欄都在休息」與「還沒分析過」，
# 兩種都會變成「目前沒有需要注意的豬」——把一個保護講成一份保證。

def test_empty_entries_with_camera_state_says_herd_low():
    r = select_focus({}, lowest_enabled=True, lowest_n=3, top_n=3,
                     camera_state={"analyzed": True, "herd_ok": False})
    assert r["status"] == "herd_low"
    assert r["items"] == []


def test_empty_entries_before_first_analysis_says_not_analyzed():
    r = select_focus({}, lowest_enabled=True, lowest_n=3, top_n=3,
                     camera_state={"analyzed": False, "herd_ok": False})
    assert r["status"] == "not_analyzed"


def test_camera_state_overrides_stale_per_entry_flags():
    """camera_state 是權威來源。entry 裡的 herd_ok 是上一輪寫進去的殘影，
    兩邊不一致時要聽相機的，不是聽某一隻豬的。"""
    entries = {1: _entry(2.0, herd_ok=True), 2: _entry(30.0, herd_ok=True)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=3, top_n=3,
                     camera_state={"analyzed": True, "herd_ok": False})
    assert r["status"] == "herd_low"


def test_without_camera_state_behaviour_is_unchanged():
    """沒有 camera_state 時走舊路徑（DB 不可用、既有測試都在這條）。"""
    entries = {1: _entry(2.0), 2: _entry(30.0)}
    assert select_focus(entries, lowest_enabled=True, lowest_n=1,
                        top_n=1)["status"] == "ok"


# ── 只列在畫面上的豬 ───────────────────────────────────────
def test_ranking_only_considers_pigs_on_screen():
    """lowest/reference 是排名，從已經死掉的編號裡挑出來的名次點下去畫面沒反應。"""
    entries = {1: _entry(1.0), 2: _entry(5.0), 3: _entry(9.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=1,
                     on_screen={2, 3}, gone_seconds={1: 12.0})
    oids = [i["object_id"] for i in r["items"]]
    assert 1 not in oids                      # 活動量最低但不在畫面上
    assert [i["label"] for i in r["items"]] == ["lowest", "reference"]
    assert oids == [2, 3]
    assert r["on_screen_count"] == 2


def test_anomaly_that_left_screen_moves_to_recent():
    entries = {7: _entry(0.5, act_anom=True), 8: _entry(9.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0,
                     on_screen={8}, gone_seconds={7: 45.0})
    assert r["items"] == []
    assert [i["object_id"] for i in r["recent"]] == [7]
    assert r["recent"][0]["on_screen"] is False
    assert r["recent"][0]["gone_seconds"] == 45.0


def test_recent_drops_anomalies_gone_too_long():
    entries = {7: _entry(0.5, act_anom=True)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0,
                     on_screen=set(), gone_seconds={7: 601.0},
                     recent_gone_seconds=600.0)
    assert r["recent"] == []


def test_recent_is_capped_and_sorted_by_freshness():
    entries = {i: _entry(0.5, act_anom=True) for i in range(1, 8)}
    gone = {i: float(i * 10) for i in range(1, 8)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0,
                     on_screen=set(), gone_seconds=gone, recent_gone_max=3)
    assert [i["object_id"] for i in r["recent"]] == [1, 2, 3]


def test_recent_holds_only_anomalies():
    """lowest/reference 是排名，編號一死就沒有意義，不進「最近消失」。"""
    entries = {1: _entry(1.0), 2: _entry(9.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=1,
                     on_screen=set(), gone_seconds={1: 5.0, 2: 5.0})
    assert r["recent"] == []


def test_offscreen_anomaly_still_suppresses_lowest():
    """還沒解除的警報就是還沒解除，不因為編號死了就改列「最低」那三隻。"""
    entries = {7: _entry(0.5, act_anom=True), 8: _entry(9.0), 9: _entry(3.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=0,
                     on_screen={8, 9}, gone_seconds={7: 5.0})
    assert r["items"] == []
    assert [i["object_id"] for i in r["recent"]] == [7]


def test_no_presence_data_falls_back_to_listing_everything():
    """app 剛起來、presence 還沒有資料時，寧可多列幾隻也不要給一份空清單。"""
    entries = {1: _entry(1.0), 2: _entry(9.0)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=1, top_n=1)
    assert [i["object_id"] for i in r["items"]] == [1, 2]
    assert all(i["on_screen"] for i in r["items"])


def test_on_screen_count_counts_pigs_not_cache_hits():
    """重啟後畫面上有 21 隻、快取裡一個都還沒有，這時回 0 會讓前端說
    「畫面上沒有偵測到豬」，把使用者指向相機斷線那個方向。"""
    r = select_focus({}, lowest_enabled=True, lowest_n=3, top_n=3,
                     on_screen={101, 102, 103}, gone_seconds={})
    assert r["on_screen_count"] == 3


def test_not_analyzed_survives_the_on_screen_filter():
    """重啟後 cache 裡全是上一代的編號、一個都不在畫面上。這時候仍然要說
    「首次分析尚未完成」，不能因為過濾完是空的就講成「一切正常」。"""
    entries = {1: _entry(None, analyzed=False), 2: _entry(None, analyzed=False)}
    r = select_focus(entries, lowest_enabled=True, lowest_n=3, top_n=3,
                     on_screen={101, 102}, gone_seconds={})
    assert r["status"] == "not_analyzed"
    assert r["on_screen_count"] == 2
