# tests/test_vod_generator.py
from datetime import datetime
from pathlib import Path
import re
import pytest

HOUR_TS = 1746403200  # 2026-05-05 00:00:00 UTC


def _make_hour_dir(base: Path, camera_id: str, stream_type: str, hour_ts: int) -> Path:
    dt = datetime.fromtimestamp(hour_ts)  # local time, matches hls_manager._hour_dir()
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


def test_pdt_tag_contains_timezone_offset(tmp_path, monkeypatch):
    """PDT tag 應包含時區偏移（+08:00 或 -07:00 等），不能是 Z（UTC only）"""
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=3)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is not None
    pdt_match = re.search(r"#EXT-X-PROGRAM-DATE-TIME:(.+)", result)
    assert pdt_match is not None
    pdt_value = pdt_match.group(1)
    # 必須包含時區偏移（+HH:MM 或 -HH:MM），不能只有 Z
    assert re.search(r"[+-]\d{2}:\d{2}$", pdt_value), f"PDT tag missing timezone offset: {pdt_value}"


def test_excludes_segments_outside_range(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=6, duration=600.0)
    # 只要第一個 segment（HOUR_TS ~ HOUR_TS+600）
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 600))
    assert result is not None
    assert result.count("#EXTINF:") == 1


def test_spans_two_hours(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    for h in [HOUR_TS, HOUR_TS + 3600]:
        hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", h)
        _write_m3u8(hour_dir, segment_count=2, duration=1800.0)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 7200))
    assert result is not None
    assert result.count("#EXTINF:") == 4


def test_segment_urls_contain_camera_and_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8(hour_dir, segment_count=1)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is not None
    assert "/cam_01/rgb/" in result
