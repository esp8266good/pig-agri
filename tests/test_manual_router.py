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


def test_manual_says_alignment_changes_recorded_temperature(manual_client):
    """對位校正看起來像「把畫面調好看」，實際上會改變寫進 DB 的體溫。

    使用者不知道這件事的話，會為了畫面順眼隨手拖一下，然後從此量到的是隔壁
    那塊地板的溫度——而且數字照樣有、範圍照樣合理，完全看不出來。
    """
    body = manual_client.get("/manual").text
    assert "體溫的取樣位置也跟著改" in body


def test_manual_explains_no_temperature_at_night(manual_client):
    """夜間沒有體溫是常態不是故障。手冊不講的話現場會當成系統壞了報修。"""
    body = manual_client.get("/manual").text
    assert "晚上豬舍沒開燈時沒有體溫" in body


def test_manual_separates_activity_and_temperature_criteria(manual_client):
    """兩種告警的判準完全不同，而設定頁只有一個「閾值」欄位。

    不寫清楚的話會有人為了「讓活動量更敏感」去調體溫的 σ，調了沒反應，
    然後一路調到體溫誤報。
    """
    body = manual_client.get("/manual").text
    assert "只作用在體溫" in body


def test_manual_explains_the_hidden_box_counter(manual_client):
    """「只畫重點」把正常框全濾掉之後，沒事的時段跟偵測整個掛掉在畫面上
    長得一模一樣。左下角那行字是唯一分得出來的線索。"""
    body = manual_client.get("/manual").text
    assert "已隱藏" in body
    assert "未偵測到豬隻" in body


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
