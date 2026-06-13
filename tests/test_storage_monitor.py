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


def test_write_probe_success_and_cleanup(tmp_path):
    assert sm.write_probe(tmp_path) is True
    assert not (tmp_path / ".storage_probe").exists()  # 用完即刪


def test_write_probe_fails_on_nonexistent_unwritable(tmp_path):
    bad = tmp_path / "nope" / "deep"
    f = tmp_path / "afile"
    f.write_text("x")
    assert sm.write_probe(f / "sub") is False


def test_marker_present(tmp_path):
    assert sm.marker_present(tmp_path, "") is True
    assert sm.marker_present(tmp_path, ".vol") is False
    (tmp_path / ".vol").write_text("1")
    assert sm.marker_present(tmp_path, ".vol") is True


def test_effective_ephemeral_dir_fallback(monkeypatch):
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: p != "/dev/shm")
    assert sm.effective_ephemeral_dir("/dev/shm/pig_live", "data/hls_live") == "data/hls_live"
    monkeypatch.setattr(sm.os.path, "isdir", lambda p: True)
    assert sm.effective_ephemeral_dir("/dev/shm/pig_live", "data/hls_live") == "/dev/shm/pig_live"


class _AppCfg:
    storage_check_interval_seconds = 20
    storage_min_free_gb = 10.0
    storage_min_free_inodes_ratio = 0.02
    storage_debounce_count = 2
    storage_volume_marker = ""
    recording_schedule_enabled = True
    recording_off_start = "17:00"
    recording_off_end = "06:30"


def test_resolve_settings_db_overrides_app():
    db = {"storage_min_free_gb": "20", "recording_off_start": "18:00",
          "recording_schedule_enabled": "false"}
    s = sm.resolve_settings(db, _AppCfg())
    assert s.min_free_bytes == 20 * 1024**3
    assert s.off_start_min == 18 * 60
    assert s.schedule_enabled is False


def test_resolve_settings_falls_back_when_db_none():
    s = sm.resolve_settings(None, _AppCfg())
    assert s.min_free_bytes == 10 * 1024**3
    assert s.off_start_min == 17 * 60
    assert s.schedule_enabled is True


import asyncio


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_target_mode_record_when_writable_and_recording_time(tmp_path, monkeypatch):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)  # 中午 → 錄影時段
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "record"


def test_target_mode_ephemeral_during_no_record_window(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 23, 0)  # 深夜 → no-record
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "ephemeral"


def test_target_mode_ephemeral_when_recording_disk_down(tmp_path):
    """錄影碟掛掉（探針失敗）但在錄影時段 → 自動轉 ephemeral（不 drop）。"""
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    rec_down = bad / "sub"             # probe 失敗
    eph_ok = tmp_path / "eph"
    _run(mon.run_once(recording_base=rec_down, ephemeral_base=eph_ok,
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "ephemeral"


def test_target_mode_drop_when_both_down(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    _run(mon.run_once(recording_base=bad / "r", ephemeral_base=bad / "e",
                      settings=s, now=now, alert_cb=None))
    assert mon.get_target_mode() == "drop"


def test_alert_fired_on_recording_disk_down_transition(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    bad = tmp_path / "afile"
    bad.write_text("x")
    fired = []

    async def cb(metric, cur, mean):
        fired.append(metric)

    _run(mon.run_once(recording_base=bad / "r", ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=cb))
    assert "storage_unwritable" in fired


def test_snapshot_has_expected_keys(tmp_path):
    mon = sm.StorageMonitor()
    s = sm.StorageSettings(debounce_count=1)
    now = datetime(2026, 6, 13, 12, 0)
    _run(mon.run_once(recording_base=tmp_path, ephemeral_base=tmp_path,
                      settings=s, now=now, alert_cb=None))
    snap = mon.get_snapshot()
    for k in ("recording_state", "ephemeral_state", "target_mode",
              "recording_time", "recording_free_gb"):
        assert k in snap
