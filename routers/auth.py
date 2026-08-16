"""登入／登出／狀態查詢。憑證只從 .env 讀，不經 DB——理由見 auth.py 模組說明。"""

import hmac
import secrets

from fastapi import APIRouter, HTTPException, Request, Response
from loguru import logger
from pydantic import BaseModel

from auth import LoginThrottle, make_session_token, verify_password, verify_session_token
from auth_middleware import COOKIE_NAME
from config import settings as app_settings

router = APIRouter(prefix="/auth", tags=["auth"])

_throttle = LoginThrottle(
    max_attempts=app_settings.auth_max_attempts,
    lockout_seconds=app_settings.auth_lockout_minutes * 60,
)


def _get_throttle() -> LoginThrottle:
    """取節流器；限額與目前設定不符就重建。

    直接用 import 當下建好的那顆會把 auth_max_attempts／auth_lockout_minutes
    永遠釘在 import 的瞬間——設定改了不生效，而且測試根本測不到節流。
    """
    global _throttle
    want_max = max(1, int(app_settings.auth_max_attempts))
    want_lockout = max(1, int(app_settings.auth_lockout_minutes) * 60)
    if (_throttle.max_attempts, _throttle.lockout_seconds) != (want_max, want_lockout):
        _throttle = LoginThrottle(max_attempts=want_max, lockout_seconds=want_lockout)
    return _throttle


def reset_throttle() -> None:
    """清空登入失敗計數（測試用；也可在需要時手動解鎖）。"""
    _get_throttle().reset()

# auth_session_secret 沒設時用的程序內隨機 secret。刻意不寫回檔案：
# 效果是「服務重啟後所有人要重新登入」，比起因為缺設定就讓 24/7 錄影服務
# 開不起來，這個代價小得多。啟動時會 WARNING 提醒。
_runtime_secret: str = ""


def get_secret() -> str:
    global _runtime_secret
    if app_settings.auth_session_secret:
        return app_settings.auth_session_secret
    if not _runtime_secret:
        _runtime_secret = secrets.token_urlsafe(32)
    return _runtime_secret


def check_auth_config() -> None:
    """啟動時檢查設定完整性。缺帳號或雜湊時是 fail-closed（沒人登得進來，
    不是任何人都進得來），所以只記 ERROR 不中止服務。"""
    if not app_settings.auth_enabled:
        return
    if not app_settings.auth_username or not app_settings.auth_password_hash:
        logger.error(
            "AUTH_ENABLED=true 但 AUTH_USERNAME／AUTH_PASSWORD_HASH 未設定："
            "沒有人能登入（fail-closed）。跑 scripts/make_password_hash.py 產生設定值。"
        )
    if not app_settings.auth_session_secret:
        logger.warning(
            "AUTH_SESSION_SECRET 未設定，改用程序內隨機值："
            "服務每次重啟都會把所有人登出。設一個固定值可避免。"
        )
    if not app_settings.auth_cookie_secure:
        logger.warning(
            "AUTH_COOKIE_SECURE=false：session cookie 會走明文 HTTP 傳送。"
            "僅限內網測試，對公網開放時務必改回 true 並在前面架 TLS。"
        )


def _client_ip(request: Request) -> str:
    """節流用的來源識別。預設不信 X-Forwarded-For——直接對外時任何人都能偽造
    這個 header，讓每次嘗試都算在不同「IP」上、把節流整個繞過去。只有確定
    前面有會覆寫該 header 的反向代理，才把 auth_trust_forwarded_for 打開。"""
    if app_settings.auth_trust_forwarded_for:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


class LoginBody(BaseModel):
    username: str
    password: str


@router.get("/status")
async def auth_status(request: Request):
    """前端開機時問一次：要不要顯示登入畫面。驗證關閉時 authenticated 直接為
    true，前端就完全不會出現登入 UI（現行行為不變）。"""
    if not app_settings.auth_enabled:
        return {"enabled": False, "authenticated": True, "username": None}
    user = verify_session_token(request.cookies.get(COOKIE_NAME, ""), get_secret())
    return {"enabled": True, "authenticated": user is not None, "username": user}


@router.post("/login")
async def login(body: LoginBody, request: Request, response: Response):
    if not app_settings.auth_enabled:
        raise HTTPException(status_code=400, detail="驗證功能未啟用")

    throttle = _get_throttle()
    ip = _client_ip(request)
    if throttle.is_locked(ip):
        wait = throttle.retry_after(ip)
        raise HTTPException(
            status_code=429,
            detail=f"嘗試次數過多，請於 {wait} 秒後再試",
            headers={"Retry-After": str(wait)},
        )

    # 帳號也用 compare_digest：一般 == 會在第一個不同的字元就返回，回應時間
    # 會洩漏「猜對幾個字」。密碼端的等時比較在 auth.verify_password 內。
    user_ok = hmac.compare_digest(
        body.username.encode("utf-8"), app_settings.auth_username.encode("utf-8")
    )
    pass_ok = verify_password(body.password, app_settings.auth_password_hash)
    # 兩個檢查都做完才判斷，避免帳號錯時提早返回而洩漏「帳號存在與否」。
    if not (user_ok and pass_ok and app_settings.auth_username):
        throttle.record_failure(ip)
        logger.warning(f"登入失敗 ip={ip} user={body.username!r}")
        raise HTTPException(status_code=401, detail="帳號或密碼錯誤")

    throttle.record_success(ip)
    ttl = max(1, app_settings.auth_session_hours) * 3600
    response.set_cookie(
        COOKIE_NAME,
        make_session_token(body.username, get_secret(), ttl),
        max_age=ttl,
        httponly=True,                             # JS 讀不到 → XSS 偷不走
        samesite="lax",                            # 擋跨站 POST，但保留從外部連結進站
        secure=app_settings.auth_cookie_secure,
        path="/",
    )
    logger.info(f"登入成功 ip={ip} user={body.username!r}")
    return {"ok": True, "username": body.username}


@router.post("/logout")
async def logout(response: Response):
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}
