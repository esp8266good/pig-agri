from datetime import datetime, timedelta
from pathlib import Path

from hls_retention import find_expired_hour_dirs, purge_expired_hls


def _mk(base: Path, cam: str, stype: str, dt: datetime) -> Path:
    d = base / cam / stype / dt.strftime("%Y-%m-%d-%H")
    d.mkdir(parents=True)
    (d / "index.m3u8").write_text("#EXTM3U\n")
    (d / "seg_000.ts").write_bytes(b"x")
    return d


def test_find_expired_returns_only_old_hour_dirs(tmp_path):
    now = datetime(2026, 5, 24, 12, 0, 0)
    old = _mk(tmp_path, "cam_01", "rgb", now - timedelta(days=10))
    recent = _mk(tmp_path, "cam_01", "rgb", now - timedelta(days=1))
    expired = find_expired_hour_dirs(tmp_path, retention_days=7, now=now)
    assert old in expired
    assert recent not in expired


def test_find_expired_ignores_non_hour_named_dirs(tmp_path):
    now = datetime(2026, 5, 24, 12, 0, 0)
    (tmp_path / "cam_01" / "rgb" / "not-a-date").mkdir(parents=True)
    _mk(tmp_path, "cam_01", "rgb", now - timedelta(days=10))
    expired = find_expired_hour_dirs(tmp_path, retention_days=7, now=now)
    assert all(p.name != "not-a-date" for p in expired)


def test_find_expired_missing_base_returns_empty(tmp_path):
    assert find_expired_hour_dirs(tmp_path / "nope", retention_days=7,
                                  now=datetime.now()) == []


def test_purge_deletes_only_expired_and_returns_them(tmp_path):
    now = datetime(2026, 5, 24, 12, 0, 0)
    old = _mk(tmp_path, "cam_01", "rgb", now - timedelta(days=10))
    old2 = _mk(tmp_path, "cam_02", "thermal", now - timedelta(days=100))
    recent = _mk(tmp_path, "cam_01", "rgb", now - timedelta(hours=2))
    deleted = purge_expired_hls(tmp_path, retention_days=7, now=now)
    assert set(deleted) == {old, old2}
    assert not old.exists()
    assert not old2.exists()
    assert recent.exists()  # 當前/近期保留，絕不誤刪
