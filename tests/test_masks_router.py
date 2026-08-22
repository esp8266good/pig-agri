"""遮罩 API 的驗證與生效路徑。

遮罩是這批功能裡唯一碰推論路徑的東西，所以驗證要嚴：座標超出畫面、頂點太少、
區域數量灌爆，任何一項漏掉都會直接影響偵測結果。
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

SQUARE = [[0.1, 0.1], [0.5, 0.1], [0.5, 0.5], [0.1, 0.5]]


@pytest.fixture
def mask_client():
    with patch.object(database, "get_pool", return_value=AsyncMock()):
        from routers.masks import router
        app = FastAPI()
        app.include_router(router)
        yield TestClient(app)


def test_get_returns_regions(mask_client):
    fake = [{"id": 1, "camera_id": "cam_01", "label": "走道",
             "enabled": True, "points": SQUARE}]
    with patch("routers.masks.query_camera_masks",
               new_callable=AsyncMock, return_value=fake):
        resp = mask_client.get("/masks/cam_01")
    assert resp.status_code == 200
    assert resp.json()["regions"][0]["label"] == "走道"


def test_put_saves_and_pushes_to_pipeline(mask_client):
    """存檔要立刻推進 pipeline，不能等重啟：照 scheduler.reload 的先例。"""
    body = {"regions": [{"label": "走道", "enabled": True, "points": SQUARE}]}
    with patch("routers.masks.replace_camera_masks",
               new_callable=AsyncMock, return_value=1) as mock_save, \
         patch("routers.masks.inference_pipeline") as mock_pipe:
        resp = mask_client.put("/masks/cam_01", json=body)
    assert resp.status_code == 200
    assert mock_save.await_count == 1
    mock_pipe.set_masks.assert_called_once()
    assert mock_pipe.set_masks.call_args[0][0] == "cam_01"


def test_put_accepts_empty_list_to_clear(mask_client):
    with patch("routers.masks.replace_camera_masks",
               new_callable=AsyncMock, return_value=0), \
         patch("routers.masks.inference_pipeline"):
        resp = mask_client.put("/masks/cam_01", json={"regions": []})
    assert resp.status_code == 200


@pytest.mark.parametrize("points,why", [
    ([[0.1, 0.1], [0.5, 0.5]], "少於三個頂點畫不出面積"),
    ([[0.1, 0.1], [1.5, 0.1], [0.5, 0.5]], "x 超出 0..1"),
    ([[0.1, -0.2], [0.5, 0.1], [0.5, 0.5]], "y 為負"),
    ([[0.1], [0.5, 0.1], [0.5, 0.5]], "頂點不是一對數字"),
    ([[0.1, "a"], [0.5, 0.1], [0.5, 0.5]], "頂點不是數字"),
])
def test_put_rejects_bad_points(mask_client, points, why):
    body = {"regions": [{"label": "x", "enabled": True, "points": points}]}
    with patch("routers.masks.replace_camera_masks", new_callable=AsyncMock), \
         patch("routers.masks.inference_pipeline"):
        resp = mask_client.put("/masks/cam_01", json=body)
    assert resp.status_code == 400, why


def test_put_rejects_too_many_regions(mask_client):
    from routers.masks import MAX_REGIONS
    body = {"regions": [{"label": "", "enabled": True, "points": SQUARE}
                        for _ in range(MAX_REGIONS + 1)]}
    with patch("routers.masks.replace_camera_masks", new_callable=AsyncMock), \
         patch("routers.masks.inference_pipeline"):
        resp = mask_client.put("/masks/cam_01", json=body)
    assert resp.status_code == 400


def test_put_rejects_too_many_vertices(mask_client):
    from routers.masks import MAX_VERTICES
    pts = [[i / (MAX_VERTICES + 5), 0.5] for i in range(MAX_VERTICES + 1)]
    body = {"regions": [{"label": "", "enabled": True, "points": pts}]}
    with patch("routers.masks.replace_camera_masks", new_callable=AsyncMock), \
         patch("routers.masks.inference_pipeline"):
        resp = mask_client.put("/masks/cam_01", json=body)
    assert resp.status_code == 400


def test_no_pool_returns_503():
    with patch.object(database, "get_pool", return_value=None):
        from routers.masks import router
        app = FastAPI()
        app.include_router(router)
        resp = TestClient(app).get("/masks/cam_01")
    assert resp.status_code == 503
