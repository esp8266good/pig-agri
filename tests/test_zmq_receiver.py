import time
from contextlib import contextmanager
from unittest.mock import MagicMock

from zmq_receiver import ZMQReceiver
import hls_manager as hls_mod


@contextmanager
def _dummy_zmq_sources():
    """ZMQReceiver() 只在 settings.zmq_sources 為空時 raise；注入一個不會被
    任何人監聽的假 source（connect 是非阻塞的，即使沒人在聽也不會出錯）。
    結束還原，避免污染其他測試檔案的 baseline。"""
    from config import ZmqSource, settings as _cfg
    _orig = _cfg.zmq_sources
    _cfg.zmq_sources = [ZmqSource(
        name="t", src_host="127.0.0.1", src_port=15999,
        src_topic="t", label="cam_01",
    )]
    try:
        yield
    finally:
        _cfg.zmq_sources = _orig


# ── thread 生命週期（多來源架構：_threads 為 list，_running 為 Event）────────

def test_receiver_starts_thread(monkeypatch):
    monkeypatch.setattr("zmq_receiver.settings.zmq_warmup_secs", 0.01)
    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
        receiver.start()
        assert receiver._running.is_set() is True
        assert len(receiver._threads) == 1
        assert receiver._threads[0].is_alive()
        receiver.stop()


def test_receiver_thread_is_daemon(monkeypatch):
    monkeypatch.setattr("zmq_receiver.settings.zmq_warmup_secs", 0.01)
    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
        receiver.start()
        assert receiver._threads[0].daemon is True
        receiver.stop()


def test_receiver_stops_cleanly(monkeypatch):
    monkeypatch.setattr("zmq_receiver.settings.zmq_warmup_secs", 0.01)
    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
        receiver.start()
        t = receiver._threads[0]
        time.sleep(0.05)
        receiver.stop()
        assert receiver._running.is_set() is False
        assert not t.is_alive()


# ── _on_frame：所有 source thread 共用的 callback ───────────────────────────

def test_on_frame_feeds_hls_manager(monkeypatch):
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    thermal = b"\xff\xd8\xff" + b"\x00" * 5
    receiver._on_frame("cam_01", 1234567890.0, 42, rgb, thermal)

    mock_manager.feed.assert_any_call("cam_01", "rgb", rgb, capture_ts=1234567890.0)
    mock_manager.feed.assert_any_call("cam_01", "thermal", thermal)


def test_on_frame_skips_empty_rgb(monkeypatch):
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
    receiver._on_frame("cam_01", 1234567890.0, 42, b"", b"\xff\xd8\xff")

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "rgb" for c in calls)


def test_on_frame_skips_empty_thermal(monkeypatch):
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
    receiver._on_frame("cam_01", 1234567890.0, 42, b"\xff\xd8\xff", b"")

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "thermal" for c in calls)


def test_on_frame_calls_pipeline_update_frame(monkeypatch):
    import numpy as np
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    fake_rgb_np = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: fake_rgb_np)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._on_frame("cam_01", 1234567890.0, 42, rgb, b"")

    mock_pipeline.update_frame.assert_called_once_with(
        "cam_01", fake_rgb_np, None, 1234567890.0, 42
    )


def test_on_frame_skips_pipeline_when_decode_fails(monkeypatch):
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    # imdecode returns None (invalid JPEG)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: None)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    with _dummy_zmq_sources():
        receiver = ZMQReceiver()
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._on_frame("cam_01", 1234567890.0, 42, rgb, b"")

    mock_pipeline.update_frame.assert_not_called()
