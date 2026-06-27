import asyncio

import ntfy_notifier


def _run(coro):
    return asyncio.run(coro)


def test_notify_noop_when_url_empty():
    assert _run(ntfy_notifier.notify("", "t", "m")) is False


def test_notify_swallows_network_error(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("net down")
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", BoomClient)
    # 不可拋例外
    assert _run(ntfy_notifier.notify("http://x/pig", "t", "m")) is False


def test_notify_posts_with_headers(monkeypatch):
    captured = {}

    class OkClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, content=None, headers=None):
            captured["url"] = url
            captured["content"] = content
            captured["headers"] = headers
            class R: status_code = 200
            return R()
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", OkClient)
    ok = _run(ntfy_notifier.notify("http://x/pig", "標題", "訊息",
                                   priority="high", tags="warning"))
    assert ok is True
    assert captured["url"] == "http://x/pig"
    assert captured["content"] == "訊息".encode("utf-8")
    assert captured["headers"]["Title"] == "標題"
    assert captured["headers"]["Priority"] == "high"
    assert captured["headers"]["Tags"] == "warning"
