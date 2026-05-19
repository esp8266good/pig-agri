# tests/test_hls_manager.py
import json
import pytest
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from hls_manager import _make_ffmpeg_cmd


def test_ffmpeg_cmd_has_correct_hls_settings(tmp_path):
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-hls_time") + 1] == "4"
    assert cmd[cmd.index("-hls_list_size") + 1] == "0"
    assert "append_list" in " ".join(cmd)
    assert "delete_segments" not in " ".join(cmd)
    assert str(tmp_path / "index.m3u8") in cmd
    assert str(tmp_path / "seg_%03d.ts") in " ".join(cmd)


def _make_stream(tmp_path, monkeypatch, proc=None, *, start_writer=False):
    from hls_manager import HLSStream
    if proc is None:
        proc = MagicMock()
        proc.stdin = MagicMock()
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    if not start_writer:
        monkeypatch.setattr("hls_manager.HLSStream._start_writer", lambda self: None)
    out_dir = tmp_path / "cam_01" / "rgb" / datetime.now().strftime("%Y-%m-%d-%H")
    out_dir.mkdir(parents=True)
    return HLSStream("cam_01", "rgb", proc, out_dir), proc


_FFMPEG_M3U8 = (
    "#EXTM3U\n"
    "#EXT-X-VERSION:3\n"
    "#EXT-X-TARGETDURATION:5\n"
    "#EXT-X-MEDIA-SEQUENCE:0\n"
    "#EXT-X-DISCONTINUITY\n"
    "#EXTINF:4.004000,\n"
    "#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\n"
    "seg_000.ts\n"
    "#EXTINF:4.004000,\n"
    "#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.004+08:00\n"
    "seg_001.ts\n"
)


def test_corrected_m3u8_replaces_pdt_with_real_capture_time(tmp_path, monkeypatch):
    """根因 #2 根治：live m3u8 的 PDT 必須是該 segment 首幀的真實擷取
    時間（_seg_pdt），而非 ffmpeg 媒體導出的會漂移的時間。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True  # 停掉 writer thread，純測 corrected_m3u8
    (stream.out_dir / "index.m3u8").write_text(_FFMPEG_M3U8)
    cap0, cap1 = 1_900_000_000.0, 1_900_000_007.0
    stream._seg_pdt = {"seg_000.ts": cap0, "seg_001.ts": cap1}

    out = stream.corrected_m3u8(stream.out_dir.name)
    assert out is not None
    from hls_manager import _iso_local
    lines = out.splitlines()
    i0 = lines.index("seg_000.ts")
    i1 = lines.index("seg_001.ts")
    assert lines[i0 - 1] == f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap0)}"
    assert lines[i1 - 1] == f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap1)}"
    assert "2099-01-01T00:00:00.000" not in out  # ffmpeg 原值已被取代
    assert out.count("seg_000.ts") == 1 and out.count("#EXTINF:") == 2


def test_corrected_m3u8_falls_back_when_segment_unknown(tmp_path, monkeypatch):
    """未知 segment（race / 無 capture_ts）保留 ffmpeg 原 PDT，不臆造。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    (stream.out_dir / "index.m3u8").write_text(_FFMPEG_M3U8)
    stream._seg_pdt = {}  # 完全不知道
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert out is not None
    assert "2099-01-01T00:00:00.000+08:00" in out
    assert "2099-01-01T00:00:04.004+08:00" in out


def test_corrected_m3u8_none_when_missing_or_dir_mismatch(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    assert stream.corrected_m3u8(stream.out_dir.name) is None  # 無 m3u8
    (stream.out_dir / "index.m3u8").write_text(_FFMPEG_M3U8)
    assert stream.corrected_m3u8("1999-01-01-00") is None  # 非當前小時


def test_scan_new_segments_records_last_capture_ts(tmp_path, monkeypatch):
    """_scan_new_segments 用 _emit_log 推每段首幀真實擷取時間（Task 2 改版）。
    同名不覆寫；_emit_log 為空時不記。"""
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    # 準備 emit_log 讓 seg_000 能被錨定到 1_700_000_123.5
    stream._emit_log.append((0, 1_700_000_123.5))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 1_700_000_123.5
    # 同名不覆寫（_seen_segs 防止重複）
    stream._emit_log.clear()
    stream._emit_log.append((0, 9_999_999_999.0))
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 1_700_000_123.5
    # _emit_log 為空時新段不記
    stream._emit_log.clear()
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert "seg_001.ts" not in stream._seg_pdt


def test_feed_threads_capture_ts_into_stream(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream.feed(b"\xff\xd8\xff", capture_ts=1_700_000_500.0)
    assert stream._last_capture_ts == 1_700_000_500.0


def test_hlsstream_feed_writes_to_stdin(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch, start_writer=True)
    stream.feed(b"\xff\xd8\xff")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if proc.stdin.write.called:
            break
        time.sleep(0.005)
    proc.stdin.write.assert_any_call(b"\xff\xd8\xff")
    assert proc.stdin.flush.call_count >= 1


def test_hlsstream_feed_updates_last_feed_time(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    before = stream.last_feed_time
    time.sleep(0.02)
    stream.feed(b"\xff\xd8\xff")
    assert stream.last_feed_time > before


def test_hlsstream_stop_closes_stdin_and_terminates(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch, start_writer=True)
    stream.stop()
    proc.stdin.close.assert_called_once()
    proc.terminate.assert_called_once()


@pytest.fixture
def fake_proc():
    proc = MagicMock()
    proc.stdin = MagicMock()
    return proc


@pytest.fixture
def manager(tmp_path, monkeypatch, fake_proc):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    from hls_manager import HLSManager
    m = HLSManager()
    yield m, fake_proc
    m.stop_all()


def test_ensure_started_creates_dir_and_launches_ffmpeg(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc) as mock_start:
        out_dir = m.ensure_started("cam_01", "rgb")
    assert mock_start.call_count == 1
    assert out_dir.exists()
    assert ("cam_01", "rgb") in m._streams


def test_ensure_started_is_idempotent(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc) as mock_start:
        dir1 = m.ensure_started("cam_01", "rgb")
        dir2 = m.ensure_started("cam_01", "rgb")
    assert dir1 == dir2
    assert mock_start.call_count == 1


def test_feed_writes_bytes_when_stream_exists(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    m.feed("cam_01", "rgb", b"\xff\xd8\xff")
    deadline = time.monotonic() + 1.0
    while time.monotonic() < deadline:
        if fake_proc.stdin.write.called:
            break
        time.sleep(0.005)
    fake_proc.stdin.write.assert_any_call(b"\xff\xd8\xff")


def test_feed_is_noop_when_stream_not_started(manager):
    m, fake_proc = manager
    m.feed("cam_99", "rgb", b"\xff\xd8\xff")
    fake_proc.stdin.write.assert_not_called()


def test_evict_stale_removes_expired_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
    m._streams[("cam_01", "rgb")].last_feed_time = time.time() - 60
    m._evict_stale()
    assert ("cam_01", "rgb") not in m._streams
    fake_proc.terminate.assert_called()


def test_evict_stale_keeps_fresh_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
    m._evict_stale()
    assert ("cam_01", "rgb") in m._streams


def test_stop_all_terminates_all_streams(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    proc1, proc2 = MagicMock(), MagicMock()
    proc1.stdin, proc2.stdin = MagicMock(), MagicMock()
    with patch("hls_manager._start_ffmpeg", side_effect=[proc1, proc2]):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
        m.ensure_started("cam_02", "rgb")
        m.stop_all()
    proc1.terminate.assert_called()
    proc2.terminate.assert_called()
    assert len(m._streams) == 0


def test_ffmpeg_cmd_drops_fps_filter_and_input_framerate(tmp_path):
    from hls_manager import _make_ffmpeg_cmd
    joined = " ".join(_make_ffmpeg_cmd(tmp_path))
    assert "fps=" not in joined
    assert "-framerate" not in joined
    assert "-hls_time" in joined


def test_feed_buffers_jpeg_with_capture_ts(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream.feed(b"J1", capture_ts=1000.0)
    assert stream._frame_buffer[-1] == (b"J1", 1000.0)


def test_emit_frame_records_emit_log_with_capture_ts(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    assert stream._emit_frame(b"A", 1000.0) is True
    assert stream._emit_frame(b"B", 1000.5) is True
    assert stream._emit_idx == 2
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1000.5)]
    assert stream._emit_frame(b"C", None) is True
    assert stream._emit_idx == 3
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1000.5)]


def test_writer_loop_duplicates_with_last_capture_ts(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    written = []
    monkeypatch.setattr(stream, "_emit_frame",
                         lambda f, ts: (written.append((f, ts)) or True))
    stream._frame_buffer.append((b"X", 2000.0))
    stream._writer_tick()
    stream._writer_tick()
    assert written == [(b"X", 2000.0), (b"X", 2000.0)]


def test_scan_records_seg_pdt_from_emit_log(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    from hls_manager import TARGET_FPS, _HLS_TIME
    idx1 = round(1 * TARGET_FPS * _HLS_TIME)
    stream._emit_log.append((0, 5000.0))
    stream._emit_log.append((idx1, 5004.0))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 5000.0
    assert stream._seg_pdt["seg_001.ts"] == 5004.0
    import json as _j
    lines = (stream.out_dir / "pdt.jsonl").read_text().splitlines()
    rows = {_j.loads(x)["seg"]: _j.loads(x)["pdt"] for x in lines}
    assert rows == {"seg_000.ts": 5000.0, "seg_001.ts": 5004.0}


def test_scan_clamps_non_monotonic_pdt(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    from hls_manager import TARGET_FPS, _HLS_TIME
    idx1 = round(1 * TARGET_FPS * _HLS_TIME)
    stream._emit_log.append((0, 5000.0))
    stream._emit_log.append((idx1, 4990.0))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 5000.0
    assert stream._seg_pdt["seg_001.ts"] == pytest.approx(5000.0 + 1e-3)


def test_restart_clears_memory_keeps_sidecar(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._emit_log.append((0, 7000.0))
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    stream._scan_new_segments()
    sidecar = stream.out_dir / "pdt.jsonl"
    assert sidecar.exists()
    new_dir = tmp_path / "cam_01" / "rgb" / "2099-01-01-05"
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        stream._restart(new_dir)
    assert stream._seg_pdt == {}
    assert list(stream._emit_log) == []
    assert stream._emit_idx == 0
    assert stream._writer_last_frame is None
    assert sidecar.exists()


def test_corrected_m3u8_real_pdt_and_extinf(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:4.000000,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\n"
        "seg_000.ts\n"
        "#EXTINF:4.000000,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.000+08:00\n"
        "seg_001.ts\n"
    )
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0, "seg_001.ts": 5004.5}
    from hls_manager import _iso_local
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(5000.0)}" in out
    assert f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(5004.5)}" in out
    assert "#EXTINF:4.500000," in out
    assert "#EXT-X-DISCONTINUITY" not in out


def test_corrected_m3u8_inserts_discontinuity_on_big_gap(tmp_path, monkeypatch):
    # Three segments so DISC placement is unambiguous:
    #   seg_000→seg_001: normal 4 s gap  → no DISC before seg_001
    #   seg_001→seg_002: big 50 s gap    → DISC must appear before seg_002 only
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\nseg_000.ts\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.000+08:00\nseg_001.ts\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:08.000+08:00\nseg_002.ts\n"
    )
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0, "seg_001.ts": 5004.0, "seg_002.ts": 5054.0}
    out = stream.corrected_m3u8(stream.out_dir.name)
    lines = out.splitlines()
    i1 = lines.index("seg_001.ts")
    i2 = lines.index("seg_002.ts")
    assert "#EXT-X-DISCONTINUITY" not in lines[:i1]      # 不在 seg_000/seg_001 前
    assert "#EXT-X-DISCONTINUITY" in lines[i1:i2]        # 在 seg_002 前
    from hls_manager import _HLS_TIME
    # seg_000 真實 EXTINF = 5004-5000 = 4.0；seg_002 因不連續用 nominal
    assert f"#EXTINF:{float(_HLS_TIME):.6f}," in out


def test_corrected_m3u8_last_segment_not_yet_anchored_no_disc(tmp_path, monkeypatch):
    # Hot live path: seg_001 not yet in _seg_pdt (race between scan & serve).
    # Must produce no DISC and keep nominal EXTINF for seg_000 (next unknown).
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = (
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\nseg_000.ts\n"
        "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:04.000+08:00\nseg_001.ts\n"
    )
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0}   # seg_001 尚未錨定（live 熱路徑）
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert "#EXT-X-DISCONTINUITY" not in out
    from hls_manager import _HLS_TIME
    assert f"#EXTINF:{float(_HLS_TIME):.6f}," in out   # seg_000 next 未知 → nominal


# ── Task 6 新測試：FID / pdt_offset 機器已移除 ────────────────────────────


def test_corrected_m3u8_no_pig_frameid_tag(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    m3u8 = ("#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:5\n"
            "#EXTINF:4.0,\n#EXT-X-PROGRAM-DATE-TIME:2099-01-01T00:00:00.000+08:00\nseg_000.ts\n")
    (stream.out_dir / "index.m3u8").write_text(m3u8)
    stream._seg_pdt = {"seg_000.ts": 5000.0}
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert "#EXT-X-PIG-FRAMEID" not in out


def test_hls_manager_has_no_pdt_offset_api():
    from hls_manager import HLSManager
    assert not hasattr(HLSManager, "get_pdt_offset")
    assert not hasattr(HLSManager, "_update_pdt_offset")


def test_feed_signature_has_no_frame_id():
    import inspect
    from hls_manager import HLSStream, HLSManager
    assert "frame_id" not in inspect.signature(HLSStream.feed).parameters
    assert "frame_id" not in inspect.signature(HLSManager.feed).parameters
