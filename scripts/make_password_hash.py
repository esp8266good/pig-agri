#!/usr/bin/env python3
"""產生 .env 要用的登入設定（密碼雜湊 + session secret）。

用法：
    uv run python scripts/make_password_hash.py

會互動式問帳號密碼，印出可直接貼進 .env 的區塊。密碼用 getpass 讀，
不會顯示在畫面上，也不會進 shell history（不要用命令列參數傳密碼）。
"""

import getpass
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from auth import hash_password  # noqa: E402


def main() -> int:
    print("=== pig-agri 登入設定產生器 ===\n")
    username = input("帳號：").strip()
    if not username:
        print("帳號不可空白。", file=sys.stderr)
        return 1

    password = getpass.getpass("密碼：")
    if len(password) < 12:
        # 服務是對公網開的，短密碼撐不住線上猜測（節流只是拖慢，不是擋死）。
        print("密碼至少 12 個字元。", file=sys.stderr)
        return 1
    if password != getpass.getpass("再輸入一次："):
        print("兩次輸入不一致。", file=sys.stderr)
        return 1

    print("\n把以下內容加進 .env，然後重啟服務：\n")
    print("AUTH_ENABLED=true")
    print(f"AUTH_USERNAME={username}")
    print(f"AUTH_PASSWORD_HASH={hash_password(password)}")
    print(f"AUTH_SESSION_SECRET={secrets.token_urlsafe(32)}")
    print("\n⚠ AUTH_COOKIE_SECURE 預設 true，代表 cookie 只在 HTTPS 下送出。")
    print("  服務對公網開放時請先架好 TLS；純 HTTP 測試才暫時設 false。")
    print("⚠ 換掉 AUTH_SESSION_SECRET 會讓所有人重新登入（這也是強制登出的做法）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
