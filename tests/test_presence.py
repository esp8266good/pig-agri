import time

import pytest

import presence


@pytest.fixture(autouse=True)
def _clear():
    presence.clear()
    yield
    presence.clear()


def test_on_screen_within_hold_and_not_after():
    now = 1000.0
    presence.mark_seen("cam_01", [1, 2], now)
    assert presence.on_screen_ids("cam_01", now=now, hold_seconds=10.0) == {1, 2}
    # 剛好在容忍時間內（含邊界）
    assert presence.on_screen_ids("cam_01", now=now + 10.0, hold_seconds=10.0) == {1, 2}
    assert presence.on_screen_ids("cam_01", now=now + 10.1, hold_seconds=10.0) == set()


def test_hold_absorbs_short_occlusion():
    """豬被另一隻擋住半秒就從清單閃掉再閃回來，看起來像壞的。"""
    now = 1000.0
    presence.mark_seen("cam_01", [1, 2], now)
    presence.mark_seen("cam_01", [1], now + 0.5)      # 2 這一幀被擋住
    assert 2 in presence.on_screen_ids("cam_01", now=now + 0.5, hold_seconds=10.0)


def test_cameras_do_not_leak_into_each_other():
    presence.mark_seen("cam_01", [7], 1000.0)
    presence.mark_seen("cam_02", [7], 1000.0)
    presence.clear("cam_01")
    assert presence.on_screen_ids("cam_01", now=1000.0) == set()
    assert presence.on_screen_ids("cam_02", now=1000.0) == {7}


def test_gone_seconds_is_never_negative():
    """擷取時間與伺服器時鐘差幾十毫秒是正常的，
    「離開 -0.3 秒」流到前端只會讓人以為壞了。"""
    presence.mark_seen("cam_01", [1], 1000.3)
    assert presence.gone_seconds("cam_01", 1, now=1000.0) == 0.0
    assert presence.gone_seconds("cam_01", 1, now=1030.3) == pytest.approx(30.0)
    assert presence.gone_seconds("cam_01", 99, now=1000.0) is None


def test_retention_forgets_long_gone_ids():
    presence.mark_seen("cam_01", [1], 1000.0)
    presence.mark_seen("cam_01", [2], 1000.0 + presence.RETENTION_SECONDS + 1)
    assert presence.last_seen_map("cam_01") == {2: 1000.0 + presence.RETENTION_SECONDS + 1}


def test_last_seen_map_is_a_copy():
    presence.mark_seen("cam_01", [1], 1000.0)
    m = presence.last_seen_map("cam_01")
    m[1] = 0.0
    assert presence.last_seen_map("cam_01")[1] == 1000.0


def test_default_now_is_wall_clock():
    presence.mark_seen("cam_01", [1], time.time())
    assert presence.on_screen_ids("cam_01") == {1}
