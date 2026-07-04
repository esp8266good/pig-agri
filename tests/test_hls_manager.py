# tests/test_hls_manager.py
import json
import pytest
import threading
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from hls_manager import _make_ffmpeg_cmd, _HLS_TIME


def test_ffmpeg_cmd_has_correct_hls_settings(tmp_path):
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-hls_time") + 1] == str(_HLS_TIME)
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
        proc.poll.return_value = None  # 預設模擬「ffmpeg 仍在跑」,避免 writer self-revive
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
    proc.poll.return_value = None
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
    fake_proc.poll.return_value = None
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
    fake_proc.poll.return_value = None
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
    proc1.poll.return_value = None
    proc2.poll.return_value = None
    with patch("hls_manager._start_ffmpeg", side_effect=[proc1, proc2]):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
        m.ensure_started("cam_02", "rgb")
        m.stop_all()
    proc1.terminate.assert_called()
    proc2.terminate.assert_called()
    assert len(m._streams) == 0


def test_ffmpeg_cmd_drops_fps_filter_but_declares_input_framerate(tmp_path):
    # 移除「輸出端 -vf fps 重採樣器」（造成漸進漂移的元兇）——保持移除。
    # 但「輸入端 -framerate」必須保留：否則 mjpeg pipe demuxer 預設用 25fps
    # 給 JPEG 打 PTS，而 writer 真實每秒只餵 TARGET_FPS 幀 → .ts 被以
    # 25/TARGET_FPS 倍速燒進 PTS → 播放被等比加速。writer 已是真實速率
    # 權威，宣告輸入 framerate=TARGET_FPS 是準確值、不會重引入漂移。
    from hls_manager import _make_ffmpeg_cmd, TARGET_FPS
    cmd = _make_ffmpeg_cmd(tmp_path)
    joined = " ".join(cmd)
    assert "fps=" not in joined  # 無輸出端 fps filter
    assert "-vf" not in cmd      # 無任何 video filter chain
    # 輸入端 -framerate 必須緊接在 -i pipe:0 之前（輸入選項，非輸出）
    fr = cmd.index("-framerate")
    assert cmd[fr + 1] == str(TARGET_FPS)
    assert cmd.index("-i") == fr + 2
    assert cmd[fr + 3] == "pipe:0"
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


# ─── 2026-06-13 根因修正：writer 自癒、_restart race-safe ────────────────
#
# 過去 bug:
#  1) 小時交接 _restart 與 writer _emit_frame 競態 → BrokenPipe → _stopped=True
#     → writer 死 → 後續所有小時都餵不到 ffmpeg → 8 小時 segment 空檔。
#  2) ffmpeg 中途死(OOM/libx264 internal)同路徑:BrokenPipe → writer 死 →
#     無自癒機制(watchdog 看 last_feed_time,ZMQ 仍在跑就不會逐出)。
# 修正:_proc_lock 序列化 stdin 寫入 vs proc swap;writer_tick 偵測
# proc.poll() != None → _restart_in_place 原地復生;_stopped 只由 stop() 觸發。


def test_proc_lock_exists_and_usable(tmp_path, monkeypatch):
    """_emit_frame 與 proc swap 需互斥的鎖。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    assert hasattr(stream, "_proc_lock")
    # 可 acquire/release(無類型強檢,Lock 是工廠函式)
    acquired = stream._proc_lock.acquire(blocking=False)
    assert acquired
    stream._proc_lock.release()


def test_restart_acquires_proc_lock(tmp_path, monkeypatch):
    """_restart 必須在 _proc_lock 內換 proc,否則 writer 寫到剛 close 的
    stdin → BrokenPipe → 寫死整條 stream。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    # 預先 acquire,模擬 writer 持鎖中 → _restart 應該阻塞
    stream._proc_lock.acquire()
    new_dir = tmp_path / "cam_01" / "rgb" / "2099-01-01-05"
    new_proc = MagicMock()
    new_proc.stdin = MagicMock()
    new_proc.poll.return_value = None
    done = threading.Event()
    error: list[BaseException] = []
    def go():
        try:
            with patch("hls_manager._start_ffmpeg", return_value=new_proc):
                stream._restart(new_dir)
        except BaseException as e:
            error.append(e)
        finally:
            done.set()
    threading.Thread(target=go, daemon=True).start()
    assert not done.wait(0.2), "_restart 應該被 _proc_lock 阻塞,卻沒有"
    stream._proc_lock.release()
    assert done.wait(2.0), "_restart 未在合理時間內完成"
    assert not error, f"_restart raised: {error}"


def test_emit_frame_brokenpipe_does_not_set_stopped(tmp_path, monkeypatch):
    """BrokenPipe 不應殺死 writer。過去:_writer_tick 把 _stopped 設 True
    → loop 退出 → 8 小時 segment 空檔。修正:錯誤交給下一輪 poll 健康檢查
    走 revive 路徑,_stopped 只由外部 stop() 觸發。"""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write.side_effect = BrokenPipeError()
    proc.poll.return_value = None
    stream, _ = _make_stream(tmp_path, monkeypatch, proc=proc)
    stream._frame_buffer.append((b"X", 1000.0))
    stream._writer_tick()
    assert stream._stopped is False, (
        "BrokenPipe 觸發 _stopped=True 會讓 writer 永久死亡 → 必須移除"
    )


def test_writer_tick_revives_dead_ffmpeg_in_place(tmp_path, monkeypatch):
    """proc.poll() != None(ffmpeg 已退出)→ writer_tick 應呼叫
    _restart_in_place 原地復生新 ffmpeg,維持同小時目錄。"""
    dead_proc = MagicMock()
    dead_proc.stdin = MagicMock()
    dead_proc.poll.return_value = 1  # 已退出
    stream, _ = _make_stream(tmp_path, monkeypatch, proc=dead_proc)
    # 預先寫過 seg_000 與 seg_001 → _restart_in_place 應該用 -start_number=2
    (stream.out_dir / "seg_000.ts").write_bytes(b"x")
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    revived = MagicMock()
    revived.stdin = MagicMock()
    revived.poll.return_value = None
    with patch("hls_manager._start_ffmpeg", return_value=revived) as mock_start:
        stream._writer_tick()
    assert stream.proc is revived
    # 同小時目錄不變
    assert stream.proc is revived
    # ffmpeg 用接續編號避免覆蓋舊 segment(seg_NNN.ts 不可變,HLS spec 要求)
    call_kwargs = mock_start.call_args.kwargs
    assert call_kwargs.get("start_number") == 2, (
        f"_start_ffmpeg 應該被 start_number=2 呼叫,實得 {call_kwargs}"
    )
    assert stream._stopped is False


def test_restart_in_place_resets_emit_index_and_offset(tmp_path, monkeypatch):
    """ffmpeg 復生 → 新 ffmpeg 從 frame 0 計算它自己的輸出 segment,故
    _emit_idx 必須重置為 0,_emit_log 清空。_seg_index_offset 記錄新
    ffmpeg 的 start_number,供 _scan_new_segments 計算相對 segment index。"""
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.poll.return_value = 1  # 已死
    stream, _ = _make_stream(tmp_path, monkeypatch, proc=proc)
    # 模擬死前已餵 100 幀
    stream._emit_idx = 100
    stream._emit_log.extend([(i, 1000.0 + i * 0.04) for i in range(100)])
    stream._writer_last_frame = (b"old", 1099.96)
    # 寫過 3 個 seg → start_number 應是 3
    for i in range(3):
        (stream.out_dir / f"seg_{i:03d}.ts").write_bytes(b"x")
    revived = MagicMock()
    revived.stdin = MagicMock()
    revived.poll.return_value = None
    with patch("hls_manager._start_ffmpeg", return_value=revived):
        stream._restart_in_place()
    assert stream._emit_idx == 0
    assert list(stream._emit_log) == []
    assert stream._writer_last_frame is None
    assert stream._seg_index_offset == 3


def test_scan_new_segments_uses_seg_index_offset(tmp_path, monkeypatch):
    """ffmpeg 復生後新段名為 seg_<offset>.ts ... seg_<offset+N>.ts,但其
    內部首幀是新 ffmpeg 的 _emit_idx=0 對應的 capture_ts。_scan_new_segments
    需扣除 _seg_index_offset 才能正確錨定 capture_ts。"""
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream._stopped = True
    # 模擬:ffmpeg 復生後 start_number=5,故新段 seg_005, seg_006, ...
    stream._seg_index_offset = 5
    stream._emit_log.append((0, 8000.0))  # seg_005 首幀
    stream._emit_log.append((round(TARGET_FPS * _HLS_TIME), 8004.0))  # seg_006
    (stream.out_dir / "seg_005.ts").write_bytes(b"x")
    (stream.out_dir / "seg_006.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert stream._seg_pdt["seg_005.ts"] == 8000.0
    assert stream._seg_pdt["seg_006.ts"] == 8004.0


def test_writer_loop_swallows_exceptions(tmp_path, monkeypatch):
    """任何 _writer_tick exception 都應該被 catch,writer thread 不死。
    過去任何未捕捉錯誤都會讓 writer 死、且無自癒機制(原 bug 同源)。"""
    stream, _ = _make_stream(tmp_path, monkeypatch, start_writer=True)
    calls = [0]
    def crashy_tick():
        calls[0] += 1
        if calls[0] <= 2:
            raise RuntimeError(f"boom-{calls[0]}")
        # 之後 tick:正常返回
    monkeypatch.setattr(stream, "_writer_tick", crashy_tick)
    # polling 而非 fixed sleep,避免 full suite 並發負載下的 timing flake
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and calls[0] <= 2:
        time.sleep(0.02)
    assert stream._writer_thread.is_alive(), (
        "writer thread 死於未捕捉的 exception → 8 小時 gap 同源 bug"
    )
    assert calls[0] > 2, (
        f"exception 後 tick 沒被繼續呼叫(loop 退出),calls={calls[0]}"
    )
    stream.stop()


def test_make_ffmpeg_cmd_accepts_start_number(tmp_path):
    """_make_ffmpeg_cmd 需接受 start_number,_restart_in_place 才能讓
    新 ffmpeg 接續 segment 編號(避免覆蓋舊段、避免 m3u8 重複條目)。"""
    cmd = _make_ffmpeg_cmd(tmp_path, start_number=42)
    assert "-start_number" in cmd
    assert cmd[cmd.index("-start_number") + 1] == "42"
    # 預設 0 仍可省略 — 但放著也無害(ffmpeg 預設就是 0)
    cmd0 = _make_ffmpeg_cmd(tmp_path)
    if "-start_number" in cmd0:
        assert cmd0[cmd0.index("-start_number") + 1] == "0"


def test_ffmpeg_cmd_rolling_uses_delete_segments(tmp_path, monkeypatch):
    from hls_manager import _make_ffmpeg_cmd
    cmd = _make_ffmpeg_cmd(tmp_path, rolling=True)
    joined = " ".join(cmd)
    assert "delete_segments" in joined
    assert cmd[cmd.index("-hls_list_size") + 1] == "8"


def test_ffmpeg_cmd_uses_config_crf_and_codec(tmp_path, monkeypatch):
    from hls_manager import _make_ffmpeg_cmd
    monkeypatch.setattr("hls_manager.settings.hls_crf", 28, raising=False)
    monkeypatch.setattr("hls_manager.settings.hls_video_codec", "libx265", raising=False)
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-crf") + 1] == "28"
    assert cmd[cmd.index("-c:v") + 1] == "libx265"


def test_ffmpeg_cmd_default_still_keeps_all_segments(tmp_path):
    from hls_manager import _make_ffmpeg_cmd
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-hls_list_size") + 1] == "0"
    assert "delete_segments" not in " ".join(cmd)


# ─── Task 6 新測試：模式感知輸出（record/ephemeral/drop）────────────────────


def test_feed_drops_frame_in_drop_mode(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "drop")
    before = len(stream._frame_buffer)
    stream.feed(b"jpegdata", capture_ts=123.0)
    assert len(stream._frame_buffer) == before     # 沒進 buffer
    assert stream._dropped_frames == 1


def test_feed_switches_to_ephemeral_dir(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    eph = tmp_path / "eph"
    monkeypatch.setattr("hls_manager._EPHEMERAL_BASE", str(eph))
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "ephemeral")
    calls = {}
    def fake_restart(new_dir, *, rolling=False, mode="record"):
        calls["dir"] = new_dir; calls["rolling"] = rolling; calls["mode"] = mode
        stream.out_dir = new_dir; stream.mode = mode; stream.rolling = rolling
    monkeypatch.setattr(stream, "_restart", fake_restart)
    stream.feed(b"j", capture_ts=1.0)
    assert calls["mode"] == "ephemeral"
    assert calls["rolling"] is True
    assert calls["dir"].name == "_live"


def test_writer_tick_skips_when_drop(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "drop")
    proc.poll.return_value = 1            # 假裝 ffmpeg 死
    revived = {"n": 0}
    monkeypatch.setattr(stream, "_restart_in_place",
                        lambda: revived.__setitem__("n", revived["n"] + 1))
    stream._writer_tick()
    assert revived["n"] == 0              # drop 模式不 revive（不 spawn 失敗 ffmpeg）


def test_scan_new_segments_skips_sidecar_in_rolling(tmp_path, monkeypatch):
    from hls_manager import TARGET_FPS, _HLS_TIME
    stream, _ = _make_stream(tmp_path, monkeypatch)
    stream.rolling = True
    stream._emit_log.append((round(TARGET_FPS * _HLS_TIME), 1700.0))
    (stream.out_dir / "seg_001.ts").write_bytes(b"x")
    stream._scan_new_segments()
    assert not (stream.out_dir / "pdt.jsonl").exists()   # rolling 不寫 sidecar
    assert "seg_001.ts" in stream._seg_pdt               # 但 in-memory PDT 仍記（live 需要）


def test_active_out_dir_matches_hour(tmp_path, monkeypatch):
    from hls_manager import HLSManager
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    with patch("hls_manager._start_ffmpeg") as mk:
        mk.return_value = MagicMock(stdin=MagicMock(), poll=MagicMock(return_value=None))
        with patch("hls_manager.HLSStream._start_writer", lambda self: None):
            mgr = HLSManager.__new__(HLSManager)
            mgr._streams = {}
            mgr._lock = threading.Lock()
            d = mgr.ensure_started("cam_01", "rgb")
            assert mgr.active_out_dir("cam_01", "rgb", d.name) == d
            assert mgr.active_out_dir("cam_01", "rgb", "1999-01-01-00") is None


def test_ensure_started_falls_back_to_ephemeral_when_record_dir_unwritable(tmp_path, monkeypatch):
    """cold-start 時錄影碟不可寫（mkdir 失敗）→ 降級 ephemeral live（不 500）。"""
    from hls_manager import HLSManager
    # 錄影碟指向一個「父是檔案」的路徑 → mkdir 必失敗
    deadfile = tmp_path / "deadrec"
    deadfile.write_text("x")
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(deadfile / "sub"))
    eph = tmp_path / "eph"
    monkeypatch.setattr("hls_manager._EPHEMERAL_BASE", str(eph))
    monkeypatch.setattr("storage_monitor.get_target_mode", lambda: "record")
    with patch("hls_manager._start_ffmpeg") as mk:
        mk.return_value = MagicMock(stdin=MagicMock(), poll=MagicMock(return_value=None))
        with patch("hls_manager.HLSStream._start_writer", lambda self: None):
            mgr = HLSManager.__new__(HLSManager)
            mgr._streams = {}
            mgr._lock = threading.Lock()
            out = mgr.ensure_started("cam_01", "rgb")
    assert out.name == "_live"                 # 已降級 ephemeral
    assert str(eph) in str(out)


def test_restart_swallows_spawn_failure(tmp_path, monkeypatch):
    """_restart 時 _start_ffmpeg 失敗 → swallow exception + log，不向上拋（過去會冒泡到 feed → zmq thread）。"""
    stream, _ = _make_stream(tmp_path, monkeypatch)
    # 讓 _start_ffmpeg 噴錯，模擬整點換目錄時 spawn 失敗
    monkeypatch.setattr("hls_manager._start_ffmpeg",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("spawn fail")))
    new_dir = tmp_path / "newhour"
    # 不應拋例外（過去會冒泡到 feed → zmq thread）
    stream._restart(new_dir, rolling=False, mode="record")


def test_desired_recording_keys_only_recently_seen(manager, monkeypatch):
    # 從未送幀（斷線/離線）的攝影機 → 不納入，避免建出空錄影目錄
    m, fake_proc = manager
    keys = m.desired_recording_keys(["cam_01"])
    assert ("cam_01", "rgb") not in keys
    assert ("cam_01", "thermal") not in keys

    # 送 rgb 幀後才納入 rgb（即使沒有 active stream，feed 也記 last_seen）
    m.feed("cam_01", "rgb", b"\xff\xd8", capture_ts=None)
    keys2 = m.desired_recording_keys(["cam_01"])
    assert ("cam_01", "rgb") in keys2
    assert ("cam_01", "thermal") not in keys2

    # 送 thermal 幀後 thermal 也納入
    m.feed("cam_01", "thermal", b"\xff\xd8", capture_ts=None)
    keys3 = m.desired_recording_keys(["cam_01"])
    assert ("cam_01", "thermal") in keys3


def test_desired_recording_keys_excludes_stale_seen(manager, monkeypatch):
    # 超過 _RECORDING_SEEN_WINDOW 沒送幀（斷線）→ 不再納入
    import hls_manager
    m, fake_proc = manager
    m.feed("cam_01", "rgb", b"\xff\xd8", capture_ts=None)
    # 把 last_seen 往回撥超過窗
    m._last_seen[("cam_01", "rgb")] = time.time() - hls_manager._RECORDING_SEEN_WINDOW - 5
    keys = m.desired_recording_keys(["cam_01"])
    assert ("cam_01", "rgb") not in keys


def test_has_stream_reflects_streams(manager):
    m, fake_proc = manager
    assert m.has_stream("cam_01", "rgb") is False
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    assert m.has_stream("cam_01", "rgb") is True
