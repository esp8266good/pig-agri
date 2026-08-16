"""測試的全域前置。

**為什麼要有這支**：`config.settings` 是模組級單例，會讀真實部署的 `.env`。
`AUTH_ENABLED` 一旦在某台機器的 `.env` 打開（遷移到 ed716-pig 之後就是這樣
——那台直接對 LAN 開，沒有 Traefik 擋在前面，一定要有登入），所有沒帶 session
cookie 的端點測試就會全部收到 401，49 個測試一起紅。那不是回歸，是測試沒有
跟部署設定隔離。

同樣的坑在 `test_config.py` 是用 `Settings(_env_file=None)` 解的，但那招只對
「直接 new 一個 Settings」有效；走 `TestClient(app)` 的測試吃的是模組級單例，
只能在這裡壓掉。

真正要驗登入行為的測試（`test_auth_router.py`）自己用 `_auth_config(...)`
把 `auth_enabled` 開回來，跑在這個 fixture 之後，不受影響。
"""

import pytest

import config


@pytest.fixture(autouse=True)
def _auth_disabled_by_default(monkeypatch):
    """預設關閉登入，讓端點測試測的是路由與參數驗證，不是有沒有 cookie。"""
    monkeypatch.setattr(config.settings, "auth_enabled", False, raising=False)
