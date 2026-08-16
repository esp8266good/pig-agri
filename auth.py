"""帳號密碼驗證的純函式層（雜湊、session token、登入失敗節流）。

刻意不依賴 FastAPI／DB，只用標準函式庫（`hashlib.scrypt` + `hmac`）——這樣不用
為了「加一道登入」而引進 passlib/bcrypt/itsdangerous 這些新相依，也方便單獨測。

## 憑證放哪裡，以及為什麼不放 DB

帳號、密碼雜湊、開關全部只從 `.env` 讀（`config.Settings`），**不進 `user_settings`
表、不進 `/settings` 的 `ALLOWED_KEYS`**。理由是先有雞後有蛋：`PUT /settings` 本身
就是要被這道驗證保護的端點，如果 `auth_enabled` 是 DB-backed，還沒登入的人就能
先打一發 `PUT /settings {"auth_enabled":"false"}` 把鎖拆掉。環境變數改不了，這條
路才封得死。代價是換密碼要改 `.env` 並重啟。

## Session 為什麼是簽章 cookie 而不是 server-side session 表

服務會重啟（錄影監督者、推論調整都要重啟），server-side session 存記憶體會在
重啟時把所有人踢下線，存 DB 又多一張表。簽章 cookie 是無狀態的：payload 帶
使用者名稱與到期時間，用 HMAC-SHA256 簽，改一個 byte 就驗不過。副作用是簽出去
的 token 在到期前無法個別撤銷——要強制全部登出就換掉 `auth_session_secret`。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

# scrypt 參數。n=2^14 在一般 CPU 上單次約數十毫秒——慢到讓線上暴力破解不划算，
# 又快到不會讓登入請求卡住事件迴圈。改動這裡不會讓舊雜湊失效（參數寫在字串裡）。
_SCRYPT_N = 1 << 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SALT_BYTES = 16
_KEY_LEN = 32

_HASH_PREFIX = "scrypt"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ── 密碼雜湊 ────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    """回傳可直接貼進 `.env` 的雜湊字串：`scrypt$n$r$p$<salt>$<key>`。

    每次呼叫用新的隨機 salt，所以同一個密碼每次結果都不同——這是預期行為，
    不是不穩定。
    """
    salt = os.urandom(_SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt,
        n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_KEY_LEN,
    )
    return "$".join([
        _HASH_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P),
        _b64e(salt), _b64e(key),
    ])


def verify_password(password: str, stored: str) -> bool:
    """比對密碼與雜湊。格式壞掉、空字串、參數不是數字 → 一律 False（不丟例外）。

    比較用 `hmac.compare_digest` 而非 `==`：後者會在第一個不同的 byte 就返回，
    回應時間會洩漏「猜對幾個 byte」。
    """
    if not password or not stored:
        return False
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != _HASH_PREFIX:
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = _b64d(parts[4])
        expected = _b64d(parts[5])
    except (ValueError, TypeError):
        return False
    try:
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=salt,
            n=n, r=r, p=p, dklen=len(expected),
        )
    except ValueError:
        return False   # 參數超出 scrypt 允許範圍
    return hmac.compare_digest(actual, expected)


# ── Session token ───────────────────────────────────────────────────

def make_session_token(username: str, secret: str, ttl_seconds: int,
                       now: float | None = None) -> str:
    """簽出 `<payload_b64>.<sig_b64>`。payload 是 JSON：使用者名稱 + 到期 epoch。"""
    now = time.time() if now is None else now
    payload = json.dumps(
        {"u": username, "exp": int(now + ttl_seconds)},
        separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    body = _b64e(payload)
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                   hashlib.sha256).digest()
    return f"{body}.{_b64e(sig)}"


def verify_session_token(token: str, secret: str,
                         now: float | None = None) -> str | None:
    """驗章 + 檢查到期。通過回使用者名稱，否則 None（不丟例外）。

    先驗簽章再看 payload——順序反過來的話，等於在信任還沒驗證的內容。
    """
    if not token or not secret:
        return None
    body, sep, sig_part = token.partition(".")
    if not sep:
        return None
    expected_sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"),
                            hashlib.sha256).digest()
    try:
        given_sig = _b64d(sig_part)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected_sig, given_sig):
        return None
    try:
        payload = json.loads(_b64d(body))
        exp = int(payload["exp"])
        username = str(payload["u"])
    except (ValueError, TypeError, KeyError):
        return None
    now = time.time() if now is None else now
    if now >= exp:
        return None
    return username


# ── 登入失敗節流 ────────────────────────────────────────────────────

class LoginThrottle:
    """同一來源 IP 連續失敗達 max_attempts 就鎖 lockout_seconds。

    服務是對公網開的，沒有這道的話密碼就等於被線上慢速暴力破解。狀態放記憶體：
    重啟會清空（可接受——重啟本來就不常發生，且攻擊者無法主動觸發重啟），也不必
    為了它多一張表。
    """

    def __init__(self, max_attempts: int = 10, lockout_seconds: int = 900) -> None:
        self._max = max(1, int(max_attempts))
        self._lockout = max(1, int(lockout_seconds))
        # ip → (連續失敗次數, 解鎖時間 epoch)
        self._state: dict[str, tuple[int, float]] = {}

    @property
    def max_attempts(self) -> int:
        return self._max

    @property
    def lockout_seconds(self) -> int:
        return self._lockout

    def reset(self) -> None:
        """清空所有計數（測試用；正式流程靠 record_success 逐一清）。"""
        self._state.clear()

    def is_locked(self, ip: str, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        fails, until = self._state.get(ip, (0, 0.0))
        if until and now < until:
            return True
        if until and now >= until:
            self._state.pop(ip, None)   # 鎖過期，重新開始計數
        return False

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = time.time() if now is None else now
        fails, _ = self._state.get(ip, (0, 0.0))
        fails += 1
        until = now + self._lockout if fails >= self._max else 0.0
        self._state[ip] = (fails, until)

    def record_success(self, ip: str) -> None:
        self._state.pop(ip, None)

    def retry_after(self, ip: str, now: float | None = None) -> int:
        """還要等幾秒才能再試（沒鎖就是 0），給 429 的 Retry-After 用。"""
        now = time.time() if now is None else now
        _, until = self._state.get(ip, (0, 0.0))
        return max(0, int(until - now)) if until else 0
