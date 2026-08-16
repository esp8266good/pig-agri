"""把整個 API 擋在 session cookie 後面的 ASGI middleware。

寫成純 ASGI（而不是 `BaseHTTPMiddleware`）只有一個理由：**要能擋 WebSocket**。
`BaseHTTPMiddleware` 只看 `http` scope，`/ws/tracking/{camera}` 會直接繞過去——
那條連線每秒推送所有豬的即時 bbox，漏掉等於整道鎖白裝。

`settings.auth_enabled` 為 False 時（預設）完全直通，一個 byte 都不碰。
"""

from __future__ import annotations

from starlette.responses import JSONResponse

from auth import verify_session_token

COOKIE_NAME = "pig_session"

# 驗證開啟時仍然公開的路徑。原則是「登入畫面自己要能載入」＋「監控要能探活」。
# /static 整包公開是刻意的：登入頁就是前端 app 的一部分，且裡面沒有任何秘密
# （帳密只在後端比對，JS 只負責把使用者輸入 POST 出去）。真正要保護的是
# /tracking、/alerts、/storage、/stream、/settings 這些資料與破壞性端點。
_PUBLIC_EXACT = frozenset({"/", "/health", "/auth/login", "/auth/logout", "/auth/status"})
_PUBLIC_PREFIX = ("/static/",)


def is_public_path(path: str) -> bool:
    return path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIX)


def read_cookie(scope, name: str) -> str:
    """從原始 ASGI headers 撈 cookie。middleware 拿不到 Starlette 的 Request
    便利物件，只能自己解。"""
    for key, value in scope.get("headers") or []:
        if key != b"cookie":
            continue
        for part in value.decode("latin-1").split(";"):
            k, _, v = part.strip().partition("=")
            if k == name:
                return v
    return ""


class AuthMiddleware:
    def __init__(self, app, settings, secret_getter) -> None:
        self.app = app
        self._settings = settings
        # 用 getter 而不是把 secret 存起來：secret 可能在 app 啟動時才產生
        # （auth_session_secret 沒設時會隨機生一把），middleware 建構得更早。
        self._secret_getter = secret_getter

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        if not self._settings.auth_enabled:
            return await self.app(scope, receive, send)
        if is_public_path(scope.get("path", "")):
            return await self.app(scope, receive, send)

        token = read_cookie(scope, COOKIE_NAME)
        if verify_session_token(token, self._secret_getter()) is not None:
            return await self.app(scope, receive, send)

        if scope["type"] == "websocket":
            # 握手階段直接 close（未 accept 前送 close → 用戶端收到 HTTP 403）。
            await send({"type": "websocket.close", "code": 1008})
            return
        response = JSONResponse({"detail": "Authentication required"}, status_code=401)
        await response(scope, receive, send)
