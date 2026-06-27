import asyncio

import ntfy_notifier


def _run(coro):
    return asyncio.run(coro)


def test_notify_noop_when_url_empty():
    assert _run(ntfy_notifier.notify("", "t", "m")) is False


def test_notify_noop_when_url_has_no_topic():
    # 結尾無路徑段（只有 host）→ 切不出 topic → no-op
    assert _run(ntfy_notifier.notify("https://ntfy.example.com", "t", "m")) is False


def test_notify_swallows_network_error(monkeypatch):
    class BoomClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("net down")
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", BoomClient)
    # 不可拋例外
    assert _run(ntfy_notifier.notify("http://x/pig", "t", "m")) is False


def test_notify_posts_json_with_unicode(monkeypatch):
    """JSON 發佈：unicode 標題/訊息走 UTF-8 JSON body（非 header），
    且 POST 到 server base、topic 放 payload。重現並驗證 ascii-header bug 已除。"""
    captured = {}

    class OkClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            captured["url"] = url
            captured["json"] = json
            class R: status_code = 200
            return R()
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", OkClient)
    ok = _run(ntfy_notifier.notify("https://ntfy.ed716.duckdns.org/pig",
                                   "🚨 錄影碟不可寫", "訊息內容",
                                   priority="urgent", tags="rotating_light,warning"))
    assert ok is True
    # POST 到 base（不含 topic 路徑），topic 進 body
    assert captured["url"] == "https://ntfy.ed716.duckdns.org"
    body = captured["json"]
    assert body["topic"] == "pig"
    assert body["title"] == "🚨 錄影碟不可寫"
    assert body["message"] == "訊息內容"
    assert body["priority"] == 5            # urgent → 5
    assert body["tags"] == ["rotating_light", "warning"]
