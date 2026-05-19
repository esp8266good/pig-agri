# tests/test_hls_manager.py
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


def _make_stream(tmp_path, monkeypatch, proc=None):
    from hls_manager import HLSStream
    if proc is None:
        proc = MagicMock()
        proc.stdin = MagicMock()
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
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
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._last_capture_ts = 1_700_000_123.5
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 1_700_000_123.5
    # 同名不覆寫；無 capture_ts 的新段不記
    stream._last_capture_ts = 9_999_999_999.0
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_000.ts"] == 1_700_000_123.5
    stream._last_capture_ts = None
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert "seg_001.ts" not in stream._seg_pdt


def test_feed_threads_capture_ts_into_stream(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream.feed(b"\xff\xd8\xff", capture_ts=1_700_000_500.0)
    assert stream._last_capture_ts == 1_700_000_500.0


def test_hlsstream_feed_writes_to_stdin(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
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
    stream, proc = _make_stream(tmp_path, monkeypatch)
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


def test_pdt_offset_defaults_to_zero(manager):
    m, _ = manager
    assert m.get_pdt_offset("cam_never_seen") == 0.0


def test_feed_without_capture_ts_keeps_offset_zero(manager):
    """Backward compat: legacy callers pass no capture_ts → offset stays 0."""
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    m.feed("cam_01", "rgb", b"\xff\xd8\xff")
    assert m.get_pdt_offset("cam_01") == 0.0


def test_feed_with_capture_ts_tracks_pdt_offset(manager):
    """First sample initializes the EMA directly, so offset ≈ wallclock gap
    between the capture-ts clock and the server clock (what ffmpeg PDT uses)."""
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=time.time() - 5.0)
    assert m.get_pdt_offset("cam_01") == pytest.approx(5.0, abs=0.5)


def test_pdt_offset_rejects_absurd_samples(manager):
    """A wildly stale/negative capture_ts must not poison the offset."""
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=time.time() - 5.0)
    m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=time.time() - 9999.0)
    m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=time.time() + 9999.0)
    assert m.get_pdt_offset("cam_01") == pytest.approx(5.0, abs=1.0)


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


def test_feed_buffers_jpeg_with_frame_id(tmp_path, monkeypatch):
    """feed() 把 (jpeg, capture_ts) 放進 buffer；frame_id 參數保留簽名相容但不入
    buffer（capture_ts 才是新授權時間軸所需）；_fed_log/_fed_count 不由 feed 累加。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._fed_log.clear()
    stream._fed_count = 0
    stream.feed(b"\xff\xd8\xff", capture_ts=1_700_000_001.0, frame_id=10)
    assert stream._frame_buffer[-1] == (b"\xff\xd8\xff", 1_700_000_001.0)
    assert stream._fed_count == 0
    assert list(stream._fed_log) == []


def test_emit_frame_records_writer_index_fed_log(tmp_path, monkeypatch):
    """_emit_frame（writer 每 tick 呼叫，含補幀）用 _emit_log 記 (emit_idx, capture_ts)；
    舊 _fed_count/_fed_log 保留欄位但此 Task 起不再由 _emit_frame 更新（後續 Task 刪除）。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._fed_log.clear()
    stream._fed_count = 0
    # _emit_frame 現在接受 (frame, capture_ts)，寫入 _emit_log/_emit_idx
    assert stream._emit_frame(b"\xff\xd8\xff", 1000.0) is True
    assert stream._emit_frame(b"\xff\xd8\xff", 1001.0) is True
    assert stream._emit_idx == 2
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1001.0)]
    # 補幀 capture_ts=None → emit_idx 仍增、不記入 _emit_log
    assert stream._emit_frame(b"\xff\xd8\xff", None) is True
    assert stream._emit_idx == 3
    assert list(stream._emit_log) == [(0, 1000.0), (1, 1001.0)]
    # 舊 _fed_count/_fed_log 不再由此路徑更新
    assert stream._fed_count == 0
    assert list(stream._fed_log) == []


def test_scan_records_seg_first_fid_by_frame_count(tmp_path, monkeypatch):
    """segment 首幀 frame_id 用『餵入幀計數』推算（避開管線延遲 L），
    取 fed_index 最接近 round(ordinal*TARGET_FPS*_HLS_TIME) 的 frame_id。"""
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    # 餵入足夠覆蓋到 seg_002 期望位置的記錄：fed_index i → frame_id 1000+i
    expected2 = round(2 * TARGET_FPS * _HLS_TIME)
    for i in range(expected2 + 5):
        stream._fed_log.append((i, 1000 + i))
    (stream.out_dir / "seg_002.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_first_fid["seg_002.ts"] == 1000 + expected2
    # 同名不覆寫
    stream._fed_log.append((expected2, 99999))
    stream._scan_new_segments()
    assert stream._seg_first_fid["seg_002.ts"] == 1000 + expected2
    # _fed_log 為空 → 不記
    stream._fed_log.clear()
    (stream.out_dir / "seg_003.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert "seg_003.ts" not in stream._seg_first_fid


def test_restart_clears_frameid_state(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    stream._fed_count = 5
    stream._fed_log.append((4, 77))
    stream._seg_first_fid["seg_000.ts"] = 77
    new_dir = stream.out_dir.parent / "2099-01-01-00"
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        stream._restart(new_dir)
    assert stream._fed_count == 0
    assert list(stream._fed_log) == []
    assert stream._seg_first_fid == {}


def test_corrected_m3u8_inserts_pig_frameid_tag(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    (stream.out_dir / "index.m3u8").write_text(_FFMPEG_M3U8)
    stream._seg_pdt = {"seg_000.ts": 1_900_000_000.0}
    stream._seg_first_fid = {"seg_000.ts": 4242}  # seg_001 故意未知
    out = stream.corrected_m3u8(stream.out_dir.name)
    assert out is not None
    lines = out.splitlines()
    i0 = lines.index("seg_000.ts")
    i1 = lines.index("seg_001.ts")
    # seg_000 緊鄰前一行（URI 行前）含 frameid 標籤
    assert "#EXT-X-PIG-FRAMEID:4242" in lines[i0 - 3:i0]
    # 未知段不插入標籤
    assert not any("PIG-FRAMEID" in ln for ln in lines[i1 - 3:i1])
    assert out.count("seg_000.ts") == 1 and out.count("#EXTINF:") == 2


def test_manager_feed_threads_frame_id(tmp_path, monkeypatch):
    from hls_manager import HLSManager
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    with patch("hls_manager._start_ffmpeg", return_value=MagicMock(stdin=MagicMock())):
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
        captured = {}
        real = m._streams[("cam_01", "rgb")].feed

        def spy(jpeg, capture_ts=None, frame_id=None):
            captured["frame_id"] = frame_id
            return real(jpeg, capture_ts, frame_id)

        m._streams[("cam_01", "rgb")].feed = spy
        m.feed("cam_01", "rgb", b"\xff\xd8\xff", capture_ts=1_700_000_000.0, frame_id=55)
    assert captured["frame_id"] == 55


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
