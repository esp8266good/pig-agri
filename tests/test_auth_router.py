"""登入端點 + AuthMiddleware 的整合測試。

最重要的兩件事：
  1. AUTH_ENABLED=false（預設）時行為與加這道驗證之前**完全一樣**——
     沒有任何端點因此變成 401。
  2. AUTH_ENABLED=true 時 WebSocket 也要被擋。`/ws/tracking/{camera}` 會推送
     所有豬的即時 bbox，漏掉它整道鎖等於白裝，而 BaseHTTPMiddleware 正好
     擋不到 WS——這是 auth_middleware 寫成純 ASGI 的理由。
"""

import sys
from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()

from auth import hash_password  # noqa: E402

_PASSWORD = "a-very-long-test-password"


@contextmanager
def _dummy_zmq_sources():
    from config import ZmqSource, settings as _cfg
    _orig = _cfg.zmq_sources
    _cfg.zmq_sources = [ZmqSource(
        name="t", src_host="127.0.0.1", src_port=5555,
        src_topic="t", label="cam_01",
    )]
    try:
        yield
    finally:
        _cfg.zmq_sources = _orig


@contextmanager
def _auth_config(**overrides):
    """暫時覆寫真實部署 .env 的 auth 設定（部署上是關閉的，測試要兩種都涵蓋）。"""
    from config import settings as _cfg
    saved = {k: getattr(_cfg, k) for k in overrides}
    for k, v in overrides.items():
        setattr(_cfg, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(_cfg, k, v)


@contextmanager
def _make_client():
    import inference.pipeline as pipeline_mod
    import analysis.scheduler as scheduler_mod
    with _dummy_zmq_sources():
        with (
            patch("database.connect", new_callable=AsyncMock),
            patch("database.disconnect", new_callable=AsyncMock),
            patch("database.get_pool", return_value=None),
            patch("zmq_receiver.zmq_receiver.start"),
            patch("zmq_receiver.zmq_receiver.stop"),
            patch.object(pipeline_mod.inference_pipeline, "start"),
            patch.object(pipeline_mod.inference_pipeline, "stop"),
            patch("hls_manager.hls_manager.stop_all"),
            patch.object(scheduler_mod.Scheduler, "start", new_callable=AsyncMock),
            patch.object(scheduler_mod.Scheduler, "stop", new_callable=AsyncMock),
        ):
            from main import app
            with TestClient(app) as c:
                yield c


@contextmanager
def _client_auth_on(**extra):
    cfg = dict(
        auth_enabled=True,
        auth_username="farmer",
        auth_password_hash=hash_password(_PASSWORD),
        auth_session_secret="test-secret-please-ignore",
        auth_session_hours=12,
        auth_cookie_secure=False,      # TestClient 走 http
        auth_max_attempts=10,
        auth_lockout_minutes=15,
        auth_trust_forwarded_for=False,
    )
    cfg.update(extra)
    with _auth_config(**cfg):
        # 節流狀態是模組級的，測試之間要清乾淨才不會互相影響
        import routers.auth as auth_router
        auth_router.reset_throttle()
        with _make_client() as c:
            yield c


# ── 關閉時：行為完全不變 ────────────────────────────────────────────

def test_disabled_leaves_all_endpoints_open():
    with _auth_config(auth_enabled=False):
        with _make_client() as c:
            assert c.get("/health").status_code == 200
            assert c.get("/cameras").status_code == 200
            # DB 不可用 → 503，重點是「不是 401」：驗證沒把它擋掉
            assert c.get("/settings").status_code != 401


def test_disabled_status_reports_authenticated():
    """驗證關閉時前端不該顯示登入 UI。"""
    with _auth_config(auth_enabled=False):
        with _make_client() as c:
            data = c.get("/auth/status").json()
            assert data == {"enabled": False, "authenticated": True, "username": None}


def test_disabled_login_returns_400():
    with _auth_config(auth_enabled=False):
        with _make_client() as c:
            assert c.post("/auth/login", json={
                "username": "farmer", "password": _PASSWORD}).status_code == 400


# ── 開啟時：擋住未登入 ──────────────────────────────────────────────

@pytest.mark.parametrize("path", [
    "/cameras",
    "/settings",
    "/alerts",
    "/alerts/active",
    "/storage/health",
    "/storage/bookmarks",
    "/tracking/cam_01",
    "/stream/cam_01/live",
])
def test_enabled_blocks_unauthenticated_requests(path):
    with _client_auth_on() as c:
        assert c.get(path).status_code == 401


@pytest.mark.parametrize("path", ["/health", "/auth/status"])
def test_enabled_keeps_public_paths_open(path):
    """探活與登入畫面自己要能用，否則沒人登得進來。"""
    with _client_auth_on() as c:
        assert c.get(path).status_code == 200


def test_enabled_blocks_websocket():
    """WS 推送即時 bbox，必須跟 HTTP 一起擋。"""
    from starlette.websockets import WebSocketDisconnect
    with _client_auth_on() as c:
        with pytest.raises((WebSocketDisconnect, Exception)):
            with c.websocket_connect("/ws/tracking/cam_01"):
                pass


def test_enabled_allows_websocket_after_login():
    with _client_auth_on() as c:
        assert c.post("/auth/login", json={
            "username": "farmer", "password": _PASSWORD}).status_code == 200
        with c.websocket_connect("/ws/tracking/cam_01") as ws:
            assert ws is not None


# ── 登入流程 ────────────────────────────────────────────────────────

def test_login_success_sets_httponly_cookie_and_unlocks_api():
    with _client_auth_on() as c:
        resp = c.post("/auth/login", json={
            "username": "farmer", "password": _PASSWORD})
        assert resp.status_code == 200
        assert resp.json()["username"] == "farmer"
        raw = resp.headers["set-cookie"].lower()
        assert "httponly" in raw          # JS 讀不到 → XSS 偷不走
        assert "samesite=lax" in raw
        assert c.get("/cameras").status_code == 200
        assert c.get("/auth/status").json()["authenticated"] is True


def test_login_marks_cookie_secure_when_configured():
    with _client_auth_on(auth_cookie_secure=True) as c:
        resp = c.post("/auth/login", json={
            "username": "farmer", "password": _PASSWORD})
        assert "secure" in resp.headers["set-cookie"].lower()


@pytest.mark.parametrize("body", [
    {"username": "farmer", "password": "wrong-password"},
    {"username": "intruder", "password": _PASSWORD},
    {"username": "", "password": ""},
])
def test_login_rejects_bad_credentials(body):
    with _client_auth_on() as c:
        assert c.post("/auth/login", json=body).status_code == 401


def test_login_fails_closed_when_credentials_unset():
    """AUTH_ENABLED=true 但沒設帳密時，必須是「沒人進得來」而不是「誰都進得來」。"""
    with _client_auth_on(auth_username="", auth_password_hash="") as c:
        assert c.post("/auth/login", json={
            "username": "", "password": ""}).status_code == 401
        assert c.get("/cameras").status_code == 401


def test_logout_clears_session():
    with _client_auth_on() as c:
        c.post("/auth/login", json={"username": "farmer", "password": _PASSWORD})
        assert c.get("/cameras").status_code == 200
        assert c.post("/auth/logout").status_code == 200
        assert c.get("/cameras").status_code == 401


def test_forged_cookie_is_rejected():
    """自己編一個 cookie 值不能過關。"""
    with _client_auth_on() as c:
        c.cookies.set("pig_session", "ZmFrZQ.ZmFrZXNpZw")
        assert c.get("/cameras").status_code == 401


def test_login_throttles_after_repeated_failures():
    """服務對公網開放，沒有節流的話密碼等於被線上慢速暴力破解。"""
    with _client_auth_on(auth_max_attempts=3) as c:
        for _ in range(3):
            assert c.post("/auth/login", json={
                "username": "farmer", "password": "nope"}).status_code == 401
        # 鎖上之後，連正確密碼也要等
        resp = c.post("/auth/login", json={
            "username": "farmer", "password": _PASSWORD})
        assert resp.status_code == 429
        assert int(resp.headers["retry-after"]) > 0


def test_auth_keys_not_settable_via_settings_endpoint():
    """憑證與開關絕不可以是 DB-backed：/settings 正是要被這道驗證保護的端點，
    若能從那裡改，未登入的人就能先把鎖拆掉再進來。"""
    from routers.settings import ALLOWED_KEYS
    for k in ("auth_enabled", "auth_username", "auth_password_hash",
              "auth_session_secret", "auth_cookie_secure",
              "auth_trust_forwarded_for"):
        assert k not in ALLOWED_KEYS
