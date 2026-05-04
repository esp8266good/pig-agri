import struct
import time
from unittest.mock import MagicMock

from zmq_receiver import ZMQReceiver
import hls_manager as hls_mod


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
    time.sleep(0.15)
    receiver.stop()
    assert receiver._running is False
    assert not receiver._thread.is_alive()


def test_process_frame_feeds_hls_manager(monkeypatch):
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
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    receiver._process_frame([topic, metadata, b"", b"\xff\xd8\xff"])

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "rgb" for c in calls)


def test_process_frame_skips_empty_thermal(monkeypatch):
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    receiver._process_frame([topic, metadata, b"\xff\xd8\xff", b""])

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "thermal" for c in calls)


def test_process_frame_calls_pipeline_update_frame(monkeypatch):
    import numpy as np
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    fake_rgb_np = np.zeros((480, 640, 3), dtype=np.uint8)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: fake_rgb_np)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._process_frame([topic, metadata, rgb, b""])

    mock_pipeline.update_frame.assert_called_once()
    call_args = mock_pipeline.update_frame.call_args[0]
    assert call_args[0] == "cam_01"
    assert call_args[1] is fake_rgb_np


def test_process_frame_skips_pipeline_when_decode_fails(monkeypatch):
    import inference.pipeline as pipeline_mod

    mock_pipeline = MagicMock()
    monkeypatch.setattr(pipeline_mod, "inference_pipeline", mock_pipeline)

    # imdecode returns None (invalid JPEG)
    monkeypatch.setattr("zmq_receiver.cv2.imdecode", lambda arr, flag: None)

    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    receiver._process_frame([topic, metadata, rgb, b""])

    mock_pipeline.update_frame.assert_not_called()
