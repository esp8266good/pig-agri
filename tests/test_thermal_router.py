"""熱像對位 API。

這條路徑會改到寫進 DB 的體溫數值（同一組參數決定體溫從熱像的哪一塊取樣），
所以驗證要嚴，而且存檔一定要同時推進 pipeline——不推的話畫面上立刻對了、
體溫卻要等到下次重啟才跟上。
"""
import sys
from unittest.mock import AsyncMock, MagicMock, patch
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

import database

VALID = {"off_x": 0.05, "off_y": -0.02, "scale_x": 1.1, "scale_y": 0.95}


@pytest.fixture
def align_client():
    with patch.object(database, "get_pool", return_value=AsyncMock()):
        from routers.thermal import router
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)


def test_get_returns_identity_for_uncalibrated_camera(align_client):
    """沒校正過的相機回 identity，不是 404。

    前端拿它直接乘進座標，回錯誤的話那台相機的框會整片消失。
    """
    with patch("routers.thermal.query_thermal_aligns",
               new_callable=AsyncMock, return_value={}):
        resp = align_client.get("/thermal-align/cam_01")
    assert resp.status_code == 200
    assert resp.json()["align"] == {
        "off_x": 0.0, "off_y": 0.0, "scale_x": 1.0, "scale_y": 1.0}


def test_get_returns_stored_values(align_client):
    with patch("routers.thermal.query_thermal_aligns",
               new_callable=AsyncMock, return_value={"cam_01": VALID}):
        resp = align_client.get("/thermal-align/cam_01")
    assert resp.json()["align"]["scale_x"] == pytest.approx(1.1)


def test_get_falls_back_to_pipeline_when_db_is_down(align_client):
    """DB 掛掉時讓前端至少畫得對，比整個面板變成錯誤訊息有用。"""
    with patch("routers.thermal.query_thermal_aligns",
               new_callable=AsyncMock, side_effect=RuntimeError("db down")):
        resp = align_client.get("/thermal-align/cam_01")
    assert resp.status_code == 200
    assert "align" in resp.json()


def test_put_saves_and_pushes_into_pipeline(align_client):
    """存檔要同時做兩件事：寫 DB，以及推進 pipeline。

    只寫 DB 的話畫面上的框立刻對了，但體溫還是照舊的位置取樣，要等重啟才跟上
    ——而且中間那段時間兩者不一致，沒有任何跡象看得出來。
    """
    with patch("routers.thermal.upsert_thermal_align",
               new_callable=AsyncMock) as up, \
         patch("routers.thermal.inference_pipeline") as pipe:
        resp = align_client.put("/thermal-align/cam_01", json=VALID)
    assert resp.status_code == 200
    up.assert_awaited_once()
    pipe.set_thermal_align.assert_called_once()
    assert pipe.set_thermal_align.call_args[0][0] == "cam_01"


@pytest.mark.parametrize("bad", [
    {"off_x": 0.9},          # 平移超過半張圖：不是對位，是打錯字
    {"off_y": -0.8},
    {"scale_x": 0.0},        # 縮放為 0 會讓每個 bbox 塌成一個點，體溫全變 None
    {"scale_y": 5.0},
])
def test_put_rejects_out_of_range(align_client, bad):
    payload = dict(VALID)
    payload.update(bad)
    with patch("routers.thermal.upsert_thermal_align",
               new_callable=AsyncMock) as up, \
         patch("routers.thermal.inference_pipeline"):
        resp = align_client.put("/thermal-align/cam_01", json=payload)
    assert resp.status_code == 400
    up.assert_not_awaited()
