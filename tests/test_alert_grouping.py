"""折疊純函式的行為。

折疊放在後端 Python 而不是 SQL，正是為了讓這些斷言寫得出來：
全套件不連真的 postgres，SQL 的行為只能靠斷言字串，證明不了折得對。
設計理由見 docs/superpowers/specs/2026-08-22-focus-list-mask-notify-manual-design.md。
"""
import pytest

from alert_grouping import FOLD_GAP_SECONDS, fold_alerts

BASE_TS = 1_750_000_000.0


def _alert(alert_id, ts, camera_id="cam_01", object_id=3,
           metric="activity", is_read=False):
    """依 triggered_at_unix 遞減排序是 fold_alerts 的前提，由呼叫端負責。"""
    return {
        "id": alert_id, "camera_id": camera_id, "object_id": object_id,
        "metric": metric, "current_value": 12.4, "mean_value": 38.1,
        "std_value": 8.5, "is_read": is_read, "triggered_at_unix": ts,
    }


def test_empty_input_gives_empty_output():
    assert fold_alerts([]) == []


def test_gap_under_six_hours_folds_into_one():
    rows = [_alert(2, BASE_TS), _alert(1, BASE_TS - 5 * 3600)]
    groups = fold_alerts(rows)
    assert len(groups) == 1
    assert groups[0]["count"] == 2
    assert groups[0]["alert_ids"] == [2, 1]


def test_gap_over_six_hours_stays_two():
    rows = [_alert(2, BASE_TS), _alert(1, BASE_TS - 7 * 3600)]
    groups = fold_alerts(rows)
    assert len(groups) == 2
    assert [g["count"] for g in groups] == [1, 1]


def test_gap_exactly_six_hours_stays_two():
    """門檻是「小於 6 小時」，剛好 6 小時不折。"""
    rows = [_alert(2, BASE_TS), _alert(1, BASE_TS - FOLD_GAP_SECONDS)]
    assert len(fold_alerts(rows)) == 2


def test_chain_of_short_gaps_folds_across_more_than_six_hours():
    """連續的短間隔會串成一條，即使頭尾跨距超過 6 小時。

    這是「連續」的定義，不是 bug：一隻豬每 5 小時被標一次、標了三次，
    是同一段持續的異常，不是三次獨立事件。
    """
    rows = [_alert(3, BASE_TS), _alert(2, BASE_TS - 5 * 3600),
            _alert(1, BASE_TS - 10 * 3600)]
    groups = fold_alerts(rows)
    assert len(groups) == 1
    assert groups[0]["count"] == 3


@pytest.mark.parametrize("field,value", [
    ("camera_id", "cam_02"), ("object_id", 4), ("metric", "temperature"),
])
def test_different_key_never_folds(field, value):
    rows = [_alert(2, BASE_TS), _alert(1, BASE_TS - 60, **{field: value})]
    assert len(fold_alerts(rows)) == 2


def test_interleaved_keys_fold_independently():
    """兩隻豬交錯告警時，各自折各自的，不會因為中間插了別人就斷開。"""
    rows = [
        _alert(4, BASE_TS, object_id=3),
        _alert(3, BASE_TS - 60, object_id=7),
        _alert(2, BASE_TS - 120, object_id=3),
        _alert(1, BASE_TS - 180, object_id=7),
    ]
    groups = fold_alerts(rows)
    assert len(groups) == 2
    assert {g["object_id"]: g["count"] for g in groups} == {3: 2, 7: 2}


def test_group_carries_latest_alert_fields_and_span():
    rows = [_alert(2, BASE_TS), _alert(1, BASE_TS - 3600)]
    g = fold_alerts(rows)[0]
    assert g["id"] == 2, "標記已讀/刪除要作用在最新那筆"
    assert g["triggered_at_unix"] == BASE_TS
    assert g["first_triggered_at_unix"] == BASE_TS - 3600


def test_group_is_read_only_when_every_member_is_read():
    mixed = [_alert(2, BASE_TS, is_read=True), _alert(1, BASE_TS - 60, is_read=False)]
    assert fold_alerts(mixed)[0]["is_read"] is False
    both = [_alert(2, BASE_TS, is_read=True), _alert(1, BASE_TS - 60, is_read=True)]
    assert fold_alerts(both)[0]["is_read"] is True


def test_groups_keep_descending_time_order():
    rows = [_alert(3, BASE_TS), _alert(2, BASE_TS - 7 * 3600),
            _alert(1, BASE_TS - 14 * 3600)]
    groups = fold_alerts(rows)
    assert [g["triggered_at_unix"] for g in groups] == [
        BASE_TS, BASE_TS - 7 * 3600, BASE_TS - 14 * 3600]


# ── 分頁邊界 ─────────────────────────────────────────────────────────
# 實機發現的 bug：群組依「最新成員」排序，但一個橫跨數小時的群組，它最舊的成員
# 可能比後面好幾個群組都還舊。cursor 若只看「最後一個回傳群組的最舊成員」，
# 前面那個長跨距群組的老成員就落在 cursor 之外，下一頁會再抓一次 → 同一筆告警
# 出現在兩頁。單元測試當初沒抓到，是因為假資料裡沒有長短群組交錯的情形。
from alert_grouping import page_groups


def test_page_fits_returns_no_cursor():
    groups = fold_alerts([_alert(2, BASE_TS), _alert(1, BASE_TS - 7 * 3600)])
    page, cursor = page_groups(groups, limit=5)
    assert len(page) == 2 and cursor is None


def test_cursor_is_the_oldest_member_of_the_whole_page():
    """長跨距群組排在前面、短群組排在後面：cursor 必須舊到把長群組的老成員也蓋住。"""
    rows = [
        _alert(4, BASE_TS,             object_id=85),   # 群組 A 最新
        _alert(3, BASE_TS - 1 * 3600,  object_id=61),   # 群組 B（單筆）
        _alert(2, BASE_TS - 5 * 3600,  object_id=85),   # 群組 A 最舊，比 B 還舊
        _alert(1, BASE_TS - 20 * 3600, object_id=70),   # 群組 C
    ]
    page, cursor = page_groups(fold_alerts(rows), limit=2)
    shown = {i for g in page for i in g["alert_ids"]}
    assert 2 in shown, "群組 A 的老成員屬於本頁"
    # cursor 就是本頁全部成員裡最舊的那一筆的位置
    assert cursor == (BASE_TS - 5 * 3600, 2)


def test_page_expands_to_cover_groups_newer_than_cursor():
    """cut 變舊之後，原本排在 limit 之外、但比 cut 新的群組必須一起收進本頁，
    否則它們既不在第一頁、又因為比 cursor 新而被第二頁跳過，直接消失。"""
    rows = [
        _alert(4, BASE_TS,             object_id=85),
        _alert(3, BASE_TS - 1 * 3600,  object_id=61),
        _alert(2, BASE_TS - 5 * 3600,  object_id=85),
        _alert(1, BASE_TS - 20 * 3600, object_id=70),
    ]
    groups = fold_alerts(rows)
    page, cursor = page_groups(groups, limit=1)
    oids = [g["object_id"] for g in page]
    assert 61 in oids, "群組 B 比 cursor 新，不收進本頁就會被兩頁都漏掉"


def test_no_alert_appears_on_both_pages():
    """端到端：把一頁的成員與下一頁的成員取交集，必須是空的。"""
    rows = [
        _alert(6, BASE_TS,             object_id=85),
        _alert(5, BASE_TS - 1 * 3600,  object_id=61),
        _alert(4, BASE_TS - 2 * 3600,  object_id=70),
        _alert(3, BASE_TS - 5 * 3600,  object_id=85),
        _alert(2, BASE_TS - 30 * 3600, object_id=61),
        _alert(1, BASE_TS - 40 * 3600, object_id=70),
    ]
    groups = fold_alerts(rows)
    page1, cursor = page_groups(groups, limit=2)
    ids1 = {i for g in page1 for i in g["alert_ids"]}
    rest = [r for r in rows
            if (r["triggered_at_unix"], r["id"]) < cursor]
    page2, _ = page_groups(fold_alerts(rest), limit=2)
    ids2 = {i for g in page2 for i in g["alert_ids"]}
    assert ids1 & ids2 == set(), f"重疊：{ids1 & ids2}"
    assert ids1 | ids2 == {r["id"] for r in rows}, "也不能有告警兩頁都沒出現"
