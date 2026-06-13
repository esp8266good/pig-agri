from datetime import datetime

import pytest

import storage_monitor as sm


def test_parse_hhmm_valid_and_invalid():
    assert sm.parse_hhmm("17:00") == 17 * 60
    assert sm.parse_hhmm("06:30") == 6 * 60 + 30
    assert sm.parse_hhmm("bad") == -1
    assert sm.parse_hhmm("25:00") == -1


@pytest.mark.parametrize("hour,minute,expected", [
    (12, 0, True),
    (6, 30, True),
    (16, 59, True),
    (17, 0, False),
    (23, 0, False),
    (3, 0, False),
    (6, 29, False),
])
def test_is_recording_time_overnight_window(hour, minute, expected):
    now = datetime(2026, 6, 13, hour, minute)
    assert sm.is_recording_time(now, 17 * 60, 6 * 60 + 30, True) is expected


def test_is_recording_time_disabled_always_true():
    now = datetime(2026, 6, 13, 23, 0)
    assert sm.is_recording_time(now, 17 * 60, 6 * 60 + 30, False) is True


def test_is_recording_time_empty_window_always_true():
    now = datetime(2026, 6, 13, 23, 0)
    assert sm.is_recording_time(now, 600, 600, True) is True
    assert sm.is_recording_time(now, -1, 390, True) is True


def test_classify_health_down_when_not_writable():
    s = sm.StorageSettings()
    assert sm.classify_health(False, True, 10**12, 0.5, s) == "down"
    assert sm.classify_health(True, False, 10**12, 0.5, s) == "down"


def test_classify_health_degraded_on_low_space_or_inodes():
    s = sm.StorageSettings(min_free_bytes=10 * 1024**3, min_free_inodes_ratio=0.02)
    assert sm.classify_health(True, True, 1 * 1024**3, 0.5, s) == "degraded"
    assert sm.classify_health(True, True, 100 * 1024**3, 0.001, s) == "degraded"


def test_classify_health_ok():
    s = sm.StorageSettings(min_free_bytes=10 * 1024**3, min_free_inodes_ratio=0.02)
    assert sm.classify_health(True, True, 100 * 1024**3, 0.5, s) == "ok"


def test_next_state_debounce():
    assert sm.next_state("ok", "ok", 3, 2) == ("ok", 0)
    assert sm.next_state("ok", "down", 0, 2) == ("ok", 1)
    assert sm.next_state("ok", "down", 1, 2) == ("down", 0)
