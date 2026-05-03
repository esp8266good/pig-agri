import time
from zmq_receiver import ZMQReceiver


def test_receiver_starts_thread():
    receiver = ZMQReceiver()
    receiver.start()
    assert receiver._running is True
    assert receiver._thread is not None
    assert receiver._thread.is_alive()
    receiver.stop()


def test_receiver_thread_is_daemon():
    receiver = ZMQReceiver()
    receiver.start()
    assert receiver._thread.daemon is True
    receiver.stop()


def test_receiver_stops_cleanly():
    receiver = ZMQReceiver()
    receiver.start()
    time.sleep(0.15)  # 等一個 poll 週期（100ms）走完
    receiver.stop()
    assert receiver._running is False
    assert not receiver._thread.is_alive()


import struct
from unittest.mock import patch, MagicMock


def test_process_frame_feeds_hls_manager(monkeypatch):
    import hls_manager as hls_mod
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    thermal = b"\xff\xd8\xff" + b"\x00" * 5
    receiver._process_frame([topic, metadata, rgb, thermal])

    mock_manager.feed.assert_any_call("cam_01", "rgb", rgb)
    mock_manager.feed.assert_any_call("cam_01", "thermal", thermal)


def test_process_frame_skips_empty_rgb(monkeypatch):
    import hls_manager as hls_mod
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    receiver._process_frame([topic, metadata, b"", b"\xff\xd8\xff"])

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "rgb" for c in calls)
