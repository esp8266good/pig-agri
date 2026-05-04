# tests/test_vod_generator.py
from datetime import datetime, timezone
from pathlib import Path
import pytest


HOUR_TS = 1746403200  # 2026-05-05 00:00:00 UTC


def _make_hour_dir(base: Path, camera_id: str, stream_type: str, hour_ts: int) -> Path:
    dt = datetime.fromtimestamp(hour_ts, tz=timezone.utc)
    dir_name = dt.strftime("%Y-%m-%d-%H")
    hour_dir = base / camera_id / stream_type / dir_name
    hour_dir.mkdir(parents=True, exist_ok=True)
    return hour_dir


def _write_m3u8(hour_dir: Path, segment_count: int = 3, duration: float = 4.0) -> None:
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
    ]
    for i in range(segment_count):
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"seg_{i:03d}.ts")
    (hour_dir / "index.m3u8").write_text("\n".join(lines) + "\n")


def test_returns_none_when_no_directory(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is None


def test_returns_m3u8_string_with_required_tags(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is not None
    assert "#EXTM3U" in result
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in result
    assert "#EXT-X-PROGRAM-DATE-TIME:" in result
    assert "#EXT-X-ENDLIST" in result


def test_segment_urls_use_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=2)
    dt = datetime.fromtimestamp(HOUR_TS, tz=timezone.utc)
    dir_name = dt.strftime("%Y-%m-%d-%H")
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert f"/stream/hls/cam_01/rgb/{dir_name}/seg_000.ts" in result


def test_filters_segments_before_start_ts(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3, duration=4.0)
    # 從第 2 個 segment（offset=4s）開始
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS + 4), float(HOUR_TS + 3600))
    assert result is not None
    assert "seg_000.ts" not in result
    assert "seg_001.ts" in result
    assert "seg_002.ts" in result


def test_spans_multiple_hour_directories(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour1_ts = HOUR_TS
    hour2_ts = HOUR_TS + 3600
    dt1 = datetime.fromtimestamp(hour1_ts, tz=timezone.utc)
    dt2 = datetime.fromtimestamp(hour2_ts, tz=timezone.utc)
    for hour_ts, dt in [(hour1_ts, dt1), (hour2_ts, dt2)]:
        hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", hour_ts)
        _write_m3u8(hour_dir, segment_count=1)
    result = build_vod_m3u8("cam_01", "rgb", float(hour1_ts), float(hour2_ts + 3600))
    assert result is not None
    assert dt1.strftime("%Y-%m-%d-%H") in result
    assert dt2.strftime("%Y-%m-%d-%H") in result


def test_target_duration_taken_from_m3u8(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    # 手動寫 TARGETDURATION:6
    (hour_dir / "index.m3u8").write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:6\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:6.000000,\nseg_000.ts\n"
    )
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert "#EXT-X-TARGETDURATION:6" in result
