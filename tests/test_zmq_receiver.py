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
