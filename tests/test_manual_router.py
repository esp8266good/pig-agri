"""操作手冊頁。

手冊刻意不放進登入保護：有人卡在登入頁時，手冊正好是他該看的東西，
而且裡面不含任何場域資料。
"""
import sys
from unittest.mock import MagicMock
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


@pytest.fixture
def manual_client():
    from routers.manual import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app)


def test_manual_renders_markdown_as_html(manual_client):
    resp = manual_client.get("/manual")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    body = resp.text
    assert "<h1>" in body, "Markdown 應該被渲染，不是原樣送出"
    assert "豬隻監測系統操作手冊" in body


def test_manual_contains_both_sections(manual_client):
    body = manual_client.get("/manual").text
    assert "日常操作" in body
    assert "設定說明" in body


def test_manual_warns_about_masking_traffic_areas(manual_client):
    """這條警語是遮罩設計裡唯一會反咬活動量正確性的地方，手冊不能漏。"""
    body = manual_client.get("/manual").text
    assert "走進走出" in body


def test_manual_is_public():
    from auth_middleware import is_public_path
    assert is_public_path("/manual") is True


def test_manual_survives_missing_file(monkeypatch):
    """手冊檔不見了也不該讓頁面 500——它是使用者求助時要看的東西。"""
    import routers.manual as m
    monkeypatch.setattr(m, "MANUAL_PATH", m.MANUAL_PATH.with_name("nope.md"))
    m._CACHE.clear()
    app = FastAPI()
    app.include_router(m.router)
    resp = TestClient(app).get("/manual")
    assert resp.status_code == 200
    assert "手冊" in resp.text
