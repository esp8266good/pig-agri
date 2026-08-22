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
    ok = _run(ntfy_notifier.notify("https://ntfy.example.com/your-topic",
                                   "🚨 錄影碟不可寫", "訊息內容",
                                   priority="urgent", tags="rotating_light,warning"))
    assert ok is True
    # POST 到 base（不含 topic 路徑），topic 進 body
    assert captured["url"] == "https://ntfy.example.com"
    body = captured["json"]
    assert body["topic"] == "your-topic"
    # 標題前面帶主機名：pig / swine 兩個訂閱點都可能被多台機器共用，
    # 不帶機器名就分不出是誰在叫。
    assert body["title"] == f"[{ntfy_notifier._HOSTNAME}] 🚨 錄影碟不可寫"
    assert body["message"] == "訊息內容"
    assert body["priority"] == 5            # urgent → 5
    assert body["tags"] == ["rotating_light", "warning"]


def test_notify_prefixes_hostname_in_title(monkeypatch):
    """標題要帶主機名：ed716 有 pig 與 swine 兩個訂閱點，遷移期間新舊機還會同時
    在跑，通知不標機器就分不出是誰發的。放標題最前面是因為手機通知從尾巴截斷。"""
    captured = {}

    class OkClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            captured["json"] = json
            class R: status_code = 200
            return R()
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", OkClient)
    monkeypatch.setattr(ntfy_notifier, "_HOSTNAME", "ed716-pig")

    assert _run(ntfy_notifier.notify("https://ntfy.example.com/swine", "⚠️ 測試", "m")) is True
    assert captured["json"]["title"] == "[ed716-pig] ⚠️ 測試"
    assert captured["json"]["topic"] == "swine"
    # message 不動：機器身分只掛在標題，訊息內容維持原樣。
    assert captured["json"]["message"] == "m"


def test_notify_title_unchanged_when_hostname_empty(monkeypatch):
    """取不到主機名時不要留一個空的 `[] ` 前綴。"""
    captured = {}

    class OkClient:
        def __init__(self, *a, **k): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, url, json=None, **k):
            captured["json"] = json
            class R: status_code = 200
            return R()
    monkeypatch.setattr(ntfy_notifier.httpx, "AsyncClient", OkClient)
    monkeypatch.setattr(ntfy_notifier, "_HOSTNAME", "")

    assert _run(ntfy_notifier.notify("https://ntfy.example.com/pig", "⚠️ 測試", "m")) is True
    assert captured["json"]["title"] == "⚠️ 測試"
