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
