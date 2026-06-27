import threading
import struct
import zmq_receiver as zr


def test_on_frame_exception_does_not_break_loop(monkeypatch):
    """on_frame 拋例外時，_source_worker 應 continue 收下一幀而非結束。"""
    calls = {"n": 0}

    def boom(*a, **k):
        calls["n"] += 1
        raise RuntimeError("boom")

    # 偽造一個會吐兩幀後停止的 socket。
    hdr = struct.Struct("dQII")
    import time
    payload = b"topic\x00" + hdr.pack(time.time(), 1, 0, 0)

    class FakeSock:
        def __init__(self):
            self.n = 0
        def setsockopt(self, *a, **k): pass
        def setsockopt_string(self, *a, **k): pass
        def connect(self, *a, **k): pass
        def poll(self, ms): return 1
        def recv(self): return payload
        def close(self): pass

    class FakeCtx:
        def socket(self, *a, **k): return FakeSock()
        def term(self): pass

    monkeypatch.setattr(zr.zmq, "Context", lambda: FakeCtx())
    monkeypatch.setattr(zr.settings, "zmq_warmup_secs", 0.0)
    monkeypatch.setattr(zr.settings, "zmq_stale_ms", 10_000.0)

    running = threading.Event()
    running.set()

    cfg = zr.ZmqSource(name="t", src_host="h", src_port=1,
                       src_topic="topic", label="cam")

    def stop_after():
        # 讓迴圈跑幾輪後停止
        import time as _t
        _t.sleep(0.2)
        running.clear()

    th = threading.Thread(target=stop_after)
    th.start()
    zr._source_worker(cfg, running, boom)
    th.join()

    # 若例外有 break loop，calls 會卡在 1；硬化後應 >= 2。
    assert calls["n"] >= 2
