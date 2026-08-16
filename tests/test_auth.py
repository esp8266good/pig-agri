"""auth.py 的純函式層測試（雜湊、session token、節流）。

不碰 FastAPI；端點與 middleware 的整合測試在 test_auth_router.py。
"""

import time

import pytest

from auth import (
    LoginThrottle,
    hash_password,
    make_session_token,
    verify_password,
    verify_session_token,
)


# ── 密碼雜湊 ────────────────────────────────────────────────────────

def test_hash_password_roundtrip():
    h = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", h) is True
    assert verify_password("wrong password", h) is False


def test_hash_password_uses_fresh_salt_each_time():
    """同一個密碼兩次雜湊必須不同（每次新 salt），否則雜湊值本身就成了
    「密碼相同」的指紋。"""
    a = hash_password("same-password-twice")
    b = hash_password("same-password-twice")
    assert a != b
    assert verify_password("same-password-twice", a)
    assert verify_password("same-password-twice", b)


@pytest.mark.parametrize("stored", [
    "",                      # 未設定 AUTH_PASSWORD_HASH
    "notahash",
    "scrypt$16384$8$1$onlyfourparts",
    "bcrypt$16384$8$1$c2FsdA$aGFzaA",   # 不認得的演算法前綴
    "scrypt$abc$8$1$c2FsdA$aGFzaA",     # 參數不是數字
])
def test_verify_password_rejects_malformed_hash(stored):
    """雜湊字串壞掉時必須回 False 而不是丟例外——.env 打錯字不該讓登入端點
    500，更不該意外放行。"""
    assert verify_password("anything", stored) is False


def test_verify_password_rejects_empty_password():
    """AUTH_PASSWORD_HASH 沒設時，空密碼不可以被當成「符合空雜湊」放行。"""
    assert verify_password("", hash_password("x")) is False
    assert verify_password("", "") is False


# ── Session token ───────────────────────────────────────────────────

def test_session_token_roundtrip():
    tok = make_session_token("farmer", "s3cret-key", ttl_seconds=3600)
    assert verify_session_token(tok, "s3cret-key") == "farmer"


def test_session_token_rejects_wrong_secret():
    """換掉 AUTH_SESSION_SECRET 就是強制全員重新登入的手段，必須真的失效。"""
    tok = make_session_token("farmer", "old-secret", ttl_seconds=3600)
    assert verify_session_token(tok, "new-secret") is None


def test_session_token_rejects_expired():
    tok = make_session_token("farmer", "k", ttl_seconds=10, now=1000.0)
    assert verify_session_token(tok, "k", now=1005.0) == "farmer"
    assert verify_session_token(tok, "k", now=1011.0) is None


def test_session_token_rejects_tampered_payload():
    """改 payload 但沿用舊簽章 → 必須擋下來（否則任何人都能自封使用者名稱）。"""
    tok = make_session_token("farmer", "k", ttl_seconds=3600)
    body, _, sig = tok.partition(".")
    forged = make_session_token("attacker", "wrong-key", ttl_seconds=3600).split(".")[0]
    assert verify_session_token(f"{forged}.{sig}", "k") is None
    assert body != forged


@pytest.mark.parametrize("tok", ["", "nodot", "a.b.c", "!!!.???"])
def test_session_token_rejects_garbage(tok):
    assert verify_session_token(tok, "k") is None


def test_session_token_rejects_empty_secret():
    """secret 沒設時不可以變成「大家都驗得過」。"""
    assert verify_session_token("anything.anything", "") is None


# ── 登入節流 ────────────────────────────────────────────────────────

def test_throttle_locks_after_max_attempts():
    t = LoginThrottle(max_attempts=3, lockout_seconds=60)
    now = 1000.0
    for _ in range(2):
        t.record_failure("1.2.3.4", now=now)
    assert t.is_locked("1.2.3.4", now=now) is False
    t.record_failure("1.2.3.4", now=now)
    assert t.is_locked("1.2.3.4", now=now) is True
    assert t.retry_after("1.2.3.4", now=now) == 60


def test_throttle_unlocks_after_lockout_window():
    t = LoginThrottle(max_attempts=1, lockout_seconds=60)
    t.record_failure("1.2.3.4", now=1000.0)
    assert t.is_locked("1.2.3.4", now=1030.0) is True
    assert t.is_locked("1.2.3.4", now=1061.0) is False


def test_throttle_success_resets_counter():
    t = LoginThrottle(max_attempts=3, lockout_seconds=60)
    t.record_failure("1.2.3.4", now=1000.0)
    t.record_failure("1.2.3.4", now=1000.0)
    t.record_success("1.2.3.4")
    t.record_failure("1.2.3.4", now=1000.0)
    assert t.is_locked("1.2.3.4", now=1000.0) is False


def test_throttle_is_per_ip():
    """一個來源被鎖不能連坐其他人。"""
    t = LoginThrottle(max_attempts=1, lockout_seconds=60)
    t.record_failure("1.2.3.4", now=1000.0)
    assert t.is_locked("1.2.3.4", now=1000.0) is True
    assert t.is_locked("5.6.7.8", now=1000.0) is False


def test_throttle_uses_real_clock_by_default():
    """now 省略時走真實時鐘，不會因為預設值是 0 就永遠算成已解鎖。"""
    t = LoginThrottle(max_attempts=1, lockout_seconds=60)
    t.record_failure("1.2.3.4")
    assert t.is_locked("1.2.3.4") is True
    assert 0 < t.retry_after("1.2.3.4") <= 60
    assert time.time() > 0
