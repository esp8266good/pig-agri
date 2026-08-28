# tests/test_vod_generator.py
from datetime import datetime
from pathlib import Path
import re

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


def _write_m3u8_with_pdt(
    hour_dir: Path, segment_count: int = 3, duration: float = 4.0
) -> None:
    """真實 ffmpeg `-hls_flags +program_date_time` 的輸出格式：
    PDT 行夾在 #EXTINF 與 segment 檔名之間。"""
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        "#EXT-X-TARGETDURATION:4",
        "#EXT-X-MEDIA-SEQUENCE:0",
        "#EXT-X-DISCONTINUITY",
    ]
    for i in range(segment_count):
        lines.append(f"#EXTINF:{duration:.6f},")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:2026-05-05T00:0{i}:00.000+08:00")
        lines.append(f"seg_{i:03d}.ts")
    (hour_dir / "index.m3u8").write_text("\n".join(lines) + "\n")


def test_segment_urls_are_ts_files_when_m3u8_has_pdt_lines(tmp_path, monkeypatch):
    """回歸：ffmpeg 加 program_date_time 後，每段 #EXTINF 後面多一行
    #EXT-X-PROGRAM-DATE-TIME，segment URL 必須仍指向 .ts 檔，
    不能把 PDT 行當成檔名（會讓瀏覽器把 #... 當 fragment → 請求裸目錄 404）。"""
    monkeypatch.setattr("config.settings.hls_base_dir", str(tmp_path))
    from vod_generator import build_vod_m3u8
    hour_dir = _make_hour_dir(tmp_path, "cam_01", "rgb", HOUR_TS)
    _write_m3u8_with_pdt(hour_dir, segment_count=3)
    result = build_vod_m3u8("cam_01", "rgb", float(HOUR_TS), float(HOUR_TS + 3600))
    assert result is not None
    assert result.count("#EXTINF:") == 3
    for line in result.splitlines():
        if line and not line.startswith("#"):
            assert line.endswith(".ts"), f"segment URL is not a .ts file: {line!r}"
            assert "PROGRAM-DATE-TIME" not in line


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


# ---------------------------------------------------------------------------
# Sidecar (pdt.jsonl) tests — Task 4
# ---------------------------------------------------------------------------

import json


def _write_hour(base: Path, cam: str, st: str, hour_name: str,
                segs: list, extinfs: list, sidecar):
    d = base / cam / st / hour_name
    d.mkdir(parents=True, exist_ok=True)
    lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:5"]
    for seg, e in zip(segs, extinfs):
        lines += [f"#EXTINF:{e:.6f},", seg]
    (d / "index.m3u8").write_text("\n".join(lines) + "\n")
    for seg in segs:
        (d / seg).write_bytes(b"x")
    if sidecar is not None:
        with (d / "pdt.jsonl").open("a") as fh:
            for seg, pdt in sidecar.items():
                fh.write(json.dumps({"seg": seg, "pdt": pdt}) + "\n")


def test_vod_uses_sidecar_real_pdt(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts"], [4.0, 4.0],
                {"seg_000.ts": hour_unix + 1.0, "seg_001.ts": hour_unix + 5.5})
    from vod_generator import build_vod_m3u8
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    assert m3u8 is not None
    assert m3u8.count("#EXT-X-PROGRAM-DATE-TIME:") >= 2
    assert "#EXTINF:4.500000," in m3u8     # seg_000 real dur = 5.5-1.0
    assert "#EXT-X-DISCONTINUITY" not in m3u8


def test_vod_falls_back_without_sidecar(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts"], [4.0, 4.0], None)
    from vod_generator import build_vod_m3u8
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    assert m3u8 is not None
    assert "#EXTINF:4.000000," in m3u8     # 回退舊 ΣEXTINF 行為


def test_vod_discontinuity_before_post_gap_segment(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour = dt.datetime(2099, 1, 1, 0, 0, 0)
    hour_unix = hour.timestamp()
    hname = hour.strftime("%Y-%m-%d-%H")
    # seg_000→seg_001 normal 4s; seg_001→seg_002 big 60s gap → DISC before seg_002 only
    _write_hour(tmp_path, "cam_01", "rgb", hname,
                ["seg_000.ts", "seg_001.ts", "seg_002.ts"], [4.0, 4.0, 4.0],
                {"seg_000.ts": hour_unix + 1.0,
                 "seg_001.ts": hour_unix + 5.0,
                 "seg_002.ts": hour_unix + 65.0})
    from vod_generator import build_vod_m3u8
    m3u8 = build_vod_m3u8("cam_01", "rgb", hour_unix, hour_unix + 3600)
    lines = m3u8.splitlines()
    i1 = next(k for k, l in enumerate(lines) if l.endswith("seg_001.ts"))
    i2 = next(k for k, l in enumerate(lines) if l.endswith("seg_002.ts"))
    assert "#EXT-X-DISCONTINUITY" not in lines[:i1]   # not before seg_000/seg_001
    assert "#EXT-X-DISCONTINUITY" in lines[i1:i2]     # before seg_002
    assert "#EXTINF:5.000000," in m3u8   # seg_001 next-gap 60s > _DISC → nominal


def test_iso_local_matches_hls_manager_format():
    import hls_manager, vod_generator
    for ts in (1779165126.49, 1700000000.0, 1779165126.0):
        assert vod_generator._iso_local(ts) == hls_manager._iso_local(ts)


def test_cross_hour_discontinuity(tmp_path, monkeypatch):
    monkeypatch.setattr("vod_generator.settings.hls_base_dir", str(tmp_path))
    import datetime as dt
    hour_a = dt.datetime(2099, 1, 1, 0, 0, 0)
    a_unix = hour_a.timestamp()
    hour_b = dt.datetime(2099, 1, 1, 1, 0, 0)
    b_unix = hour_b.timestamp()
    a_name = hour_a.strftime("%Y-%m-%d-%H")
    b_name = hour_b.strftime("%Y-%m-%d-%H")
    # Hour A last seg PDT ≈ 4s before boundary; Hour B first seg PDT ≈ 14s
    # after boundary → ~18s cross-hour gap (> _DISC) must yield DISCONTINUITY.
    _write_hour(tmp_path, "cam_01", "rgb", a_name,
                ["seg_000.ts"], [4.0], {"seg_000.ts": a_unix + 3596.0})
    _write_hour(tmp_path, "cam_01", "rgb", b_name,
                ["seg_000.ts"], [4.0], {"seg_000.ts": b_unix + 14.0})
    from vod_generator import build_vod_m3u8
    m3u8 = build_vod_m3u8("cam_01", "rgb", a_unix, b_unix + 3600)
    lines = m3u8.splitlines()
    seg_idxs = [k for k, l in enumerate(lines) if l.endswith("seg_000.ts")]
    assert len(seg_idxs) == 2                       # one per hour
    # DISCONTINUITY must appear between the two hours' segments, not before the first
    assert "#EXT-X-DISCONTINUITY" in lines[seg_idxs[0]:seg_idxs[1]]
    assert "#EXT-X-DISCONTINUITY" not in lines[:seg_idxs[0]]
