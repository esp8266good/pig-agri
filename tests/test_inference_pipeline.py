import sys
import asyncio
import threading
import numpy as np
import pytest
from unittest.mock import MagicMock, AsyncMock, patch

for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
    "trackers", "trackers.hybrid_sort_tracker",
    "trackers.hybrid_sort_tracker.hybrid_sort_reid",
    "fast_reid", "fast_reid.fast_reid_interfece",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_pipeline():
    from inference.pipeline import InferencePipeline
    # 走真正的 __init__（它只建 dict／lock，沒有重量級初始化——重的東西都在
    # start()）。原本用 __new__ 逐一手設欄位，每次 pipeline 多一個狀態欄位就
    # 會有一批測試因為 AttributeError 而爆掉，且失敗訊息完全指不到真正的原因。
    p = InferencePipeline()
    p._active = True
    return p


def test_update_frame_stores_latest():
    from inference.pipeline import FrameData
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0, 438190)
    assert "cam_01" in p._latest
    assert p._latest["cam_01"].rgb_np is rgb


def test_update_frame_uses_camera_frame_id():
    """frame_id 必須是擷取端（zmq 封包頭）的真實 frame_id，供 VOD /tracking 同幀群聚
    （pickClosestFrame）與 DB tracking_logs 記錄；HLS bbox 同步已改用 capture_ts，不再依賴 frame_id。"""
    p = _make_pipeline()
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p.update_frame("cam_01", rgb, None, 1.0, 438190)
    p.update_frame("cam_01", rgb, None, 2.0, 438191)
    assert p._latest["cam_01"].frame_id == 438191


def test_process_batch_calls_broadcast():
    from inference.pipeline import FrameData, InferencePipeline
    p = _make_pipeline()

    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [np.ones((2, 7), dtype=np.float32)]

    mock_reid = MagicMock()
    mock_reid.extract.return_value = np.ones((2, 2048), dtype=np.float32)

    mock_tracker_pool = MagicMock()
    mock_tracker_pool.update.return_value = [
        [10.0, 20.0, 50.0, 80.0, 1, 0.9]
    ]

    broadcast_calls = []

    async def mock_broadcast(camera_id, msg):
        broadcast_calls.append((camera_id, msg))

    p._detector = mock_detector
    p._reid = mock_reid
    p._tracker_pool = mock_tracker_pool
    p._broadcast_fn = mock_broadcast

    loop = asyncio.new_event_loop()
    p._event_loop = loop

    from concurrent.futures import ThreadPoolExecutor
    p._executor = ThreadPoolExecutor(max_workers=2)

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    snapshot = {"cam_01": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1)}

    # run _process_batch and let the broadcast future complete
    p._process_batch(snapshot)
    loop.run_until_complete(asyncio.sleep(0.05))
    loop.close()
    p._executor.shutdown(wait=False)

    assert len(broadcast_calls) == 1
    cam, msg = broadcast_calls[0]
    assert cam == "cam_01"
    assert "objects" in msg
    assert msg["objects"][0]["object_id"] == 1


def _make_processing_pipeline(n_cams: int = 1):
    """建好一個能真的跑完 _process_batch 的 pipeline（detector/reid/tracker 皆 mock）。
    n_cams：detector 每輪要回幾組偵測（＝ batch 內的 camera 數）。"""
    from concurrent.futures import ThreadPoolExecutor
    p = _make_pipeline()

    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [
        np.ones((2, 7), dtype=np.float32) for _ in range(n_cams)
    ]

    mock_reid = MagicMock()
    mock_reid.extract.return_value = np.ones((2, 2048), dtype=np.float32)

    mock_tracker_pool = MagicMock()
    mock_tracker_pool.update.return_value = [[10.0, 20.0, 50.0, 80.0, 1, 0.9]]

    broadcast_calls = []

    async def mock_broadcast(camera_id, msg):
        broadcast_calls.append((camera_id, msg))

    p._detector = mock_detector
    p._reid = mock_reid
    p._tracker_pool = mock_tracker_pool
    p._broadcast_fn = mock_broadcast
    p._event_loop = asyncio.new_event_loop()
    p._executor = ThreadPoolExecutor(max_workers=2)
    return p, mock_detector, mock_tracker_pool, broadcast_calls


def test_process_batch_skips_frozen_frame_reprocess():
    """凍結畫面防護：同一 camera 的 frame_id 沒前進時（ZMQ 送幀停滯，_latest 卡住舊幀），
    不可對同一幀重跑 detector／重複寫 DB／重播 WS——否則會把單一幀灌進 DB 上百萬列，
    汙染活動量計算。root cause 見 docs/handoff-tracking-gap-2026-07-20.md。"""
    from inference.pipeline import FrameData
    p, mock_detector, _pool, broadcast_calls = _make_processing_pipeline()

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    frozen = FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1111387)

    # 連續兩輪 loop 拿到同一張凍結幀（frame_id 不變）
    p._process_batch({"cam_01": frozen})
    p._process_batch({"cam_01": frozen})
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    # 只有第一輪該跑 detector 與 broadcast；第二輪凍結幀不得重跑
    assert mock_detector.infer.call_count == 1
    assert len(broadcast_calls) == 1


class _FakeClock:
    """可控的 monotonic 時鐘。停滯門檻改用牆鐘後，測試不能再靠「呼叫幾次」
    來推進狀態，得自己決定時間走多久。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_process_batch_ignores_stale_ticks_within_threshold():
    """慢速相機的常態就是停滯。門檻內的停滯 tick 一律完全跳過 tracker，
    不得餵空偵測——空偵測會把 hit_streak 打回 0，min_hits(3) 永遠達不到，
    該相機就整天吐不出任何已確認軌跡（cam_02 曾連續 23 小時零筆）。"""
    from inference.pipeline import FrameData, STALE_SECONDS_BEFORE_AGING
    clock = _FakeClock()
    p, _detector, mock_pool, _bc = _make_processing_pipeline()
    p._clock = clock

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    frozen = FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1111387)

    p._process_batch({"cam_01": frozen})            # fresh：真實 dets
    # 模擬 3Hz 迴圈、相機 0.3fps：門檻內狂敲很多次停滯 tick
    for _ in range(30):
        clock.advance(STALE_SECONDS_BEFORE_AGING / 40)
        p._process_batch({"cam_01": frozen})
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    assert mock_pool.update.call_count == 1   # 只有 fresh 那輪


def test_process_batch_ages_out_after_sustained_stall():
    """真的停滯超過門檻秒數後才餵空偵測，讓殘留 track 正常 age out。"""
    from inference.pipeline import FrameData, STALE_SECONDS_BEFORE_AGING
    clock = _FakeClock()
    p, _detector, mock_pool, _bc = _make_processing_pipeline()
    p._clock = clock

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    frozen = FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1111387)

    p._process_batch({"cam_01": frozen})            # fresh：真實 dets
    assert mock_pool.update.call_count == 1

    clock.advance(STALE_SECONDS_BEFORE_AGING + 0.1)
    p._process_batch({"cam_01": frozen})            # 超過門檻 → 餵空 age out
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    assert mock_pool.update.call_count == 2
    stale_dets = mock_pool.update.call_args_list[1].args[1]
    assert stale_dets is None or len(stale_dets) == 0


def test_process_batch_rate_limits_age_out():
    """進入 age-out 後不得以迴圈速率狂餵：兩次之間至少隔
    STALE_AGE_OUT_INTERVAL 秒，否則又回到「空偵測淹掉真偵測」的老問題。"""
    from inference.pipeline import (
        FrameData, STALE_SECONDS_BEFORE_AGING, STALE_AGE_OUT_INTERVAL,
    )
    clock = _FakeClock()
    p, _detector, mock_pool, _bc = _make_processing_pipeline()
    p._clock = clock

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    frozen = FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1111387)

    p._process_batch({"cam_01": frozen})
    clock.advance(STALE_SECONDS_BEFORE_AGING + 0.1)

    # 一秒內以 10Hz 連敲十次，只該有一次 age-out
    for _ in range(10):
        p._process_batch({"cam_01": frozen})
        clock.advance(STALE_AGE_OUT_INTERVAL / 10)
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    # 1 次 fresh + 1 次 age-out（第一敲），其餘九敲被限速擋掉
    assert mock_pool.update.call_count == 2


def test_new_frame_resets_stall_timer():
    """幀來得慢但持續（例如 4 秒一張）→ 每張新幀都把停滯計時歸零，
    永遠不該進入 age-out。這正是遠端相機的常態。"""
    from inference.pipeline import FrameData
    clock = _FakeClock()
    p, _detector, mock_pool, _bc = _make_processing_pipeline(n_cams=1)
    p._clock = clock

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    for i in range(6):
        p._process_batch({"cam_01": FrameData(
            rgb_np=rgb, thermal_np=None, ts=float(i), frame_id=1000 + i)})
        # 每張新幀之間隔 4 秒，其間迴圈以 3Hz 空轉
        for _ in range(12):
            clock.advance(4.0 / 12)
            p._process_batch({"cam_01": FrameData(
                rgb_np=rgb, thermal_np=None, ts=float(i), frame_id=1000 + i)})
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    # 六張真幀 → 六次 tracker update，一次空偵測都不該有
    assert mock_pool.update.call_count == 6
    for call in mock_pool.update.call_args_list:
        dets = call.args[1]
        assert dets is not None and len(dets) > 0


def test_process_batch_drains_all_futures_when_one_camera_raises():
    """某支 camera 的 tracker update 丟例外時，其餘 camera 的 future 仍必須被
    result() 取回並照常發佈。否則例外會中斷收集迴圈、後面那些 future 被遺留，
    下一輪 loop 對同一支再送一次 update → 違反 tracker_pool 的
    「每 camera 同時至多一個 update」不變式（tracker 內部狀態無鎖）。"""
    from inference.pipeline import FrameData
    p, _detector, mock_pool, broadcast_calls = _make_processing_pipeline(n_cams=2)

    def _update(cam, dets, img_info, img_size, id_feature):
        if cam == "cam_01":
            raise RuntimeError("tracker boom")
        return [[10.0, 20.0, 50.0, 80.0, 7, 0.9]]

    mock_pool.update.side_effect = _update

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p._process_batch({
        "cam_01": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=11),
        "cam_02": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=22),
    })
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    # 兩支都送出並取回了 update；只有健康的那支發佈
    assert mock_pool.update.call_count == 2
    assert [cam for cam, _msg in broadcast_calls] == ["cam_02"]


def test_process_batch_skips_on_exception():
    from inference.pipeline import FrameData
    p = _make_pipeline()
    mock_detector = MagicMock()
    mock_detector.infer.side_effect = RuntimeError("GPU error")
    mock_detector.test_size = (736, 1280)
    p._detector = mock_detector
    p._reid = MagicMock()
    p._tracker_pool = MagicMock()
    p._broadcast_fn = AsyncMock()
    p._event_loop = asyncio.new_event_loop()

    from concurrent.futures import ThreadPoolExecutor
    p._executor = ThreadPoolExecutor(max_workers=1)

    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    snapshot = {"cam_01": FrameData(rgb_np=rgb, thermal_np=None, ts=1.0, frame_id=1)}

    # should not raise
    p._process_batch(snapshot)
    p._event_loop.close()
    p._executor.shutdown(wait=False)


def test_compute_thermal_celsius_returns_mean_of_region():
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    thermal = np.zeros((120, 160), dtype=np.float32)
    thermal[10:20, 10:20] = 38.5
    # bbox 在 640×480 的 rgb 座標；換算到 160×120 → (10,10)-(20,20)
    result = _compute_thermal_celsius(thermal, 40.0, 40.0, 80.0, 80.0, 640, 480)
    assert result == pytest.approx(38.5)


def test_compute_thermal_celsius_uses_real_frame_sizes_not_hardcoded():
    """回歸：換算係數必須來自實際尺寸，不能寫死。

    正式機的組合是 rgb 1280×720 配熱像 160×120。舊版把兩邊都硬編在預設參數裡
    （rgb 當 640×480、熱像當 160×120），於是係數錯一倍、取樣還被 clamp 在圖的
    左上角，每隻豬拿到的都是同一塊背景。舊測試剛好餵 640×480 的 bbox，
    所以它一直是綠的——這個測試特地用真實尺寸，讓同樣的錯誤再也躲不過去。
    """
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    thermal = np.zeros((120, 160), dtype=np.float32)
    # 熱像的右下角一塊：對應 rgb 畫面右下角的豬。
    thermal[90:120, 120:160] = 39.0
    # rgb 1280×720 上、右下角的 bbox
    result = _compute_thermal_celsius(thermal, 960.0, 540.0, 1280.0, 720.0, 1280, 720)
    assert result == pytest.approx(39.0)


def test_compute_thermal_celsius_applies_alignment():
    """對位參數要真的改變取樣的位置。

    熱像與 rgb 是兩顆分開的鏡頭，等比例換算過去還是會偏。這裡把 bbox 往右下
    推 1/4 張圖，取到的就該是被推到的那一塊，不是原來那一塊。
    """
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    thermal = np.zeros((120, 160), dtype=np.float32)
    thermal[30:60, 40:80] = 39.0     # 熱像上 y 25~50%、x 25~50% 的那一塊

    # rgb 1280x720 上左上角 1/4 的 bbox：不校正時對到熱像的左上角 1/4（全 0）
    assert _compute_thermal_celsius(
        thermal, 0.0, 0.0, 320.0, 180.0, 1280, 720
    ) == pytest.approx(0.0)

    # 往右下各推 1/4 張圖之後，正好落在那塊 39.0 上
    aligned = _compute_thermal_celsius(
        thermal, 0.0, 0.0, 320.0, 180.0, 1280, 720,
        {"off_x": 0.25, "off_y": 0.25, "scale_x": 1.0, "scale_y": 1.0},
    )
    assert aligned == pytest.approx(39.0)


def test_compute_thermal_celsius_returns_none_when_alignment_pushes_out_of_view():
    """校正把這隻豬推出熱像視野時回 None，不要夾回邊界。

    夾回去等於把一塊牆壁的溫度當成牠的體溫報上去——那正是舊 bug 的形狀，
    數字有、範圍也合理，只是跟那隻豬無關。
    """
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    thermal = np.full((120, 160), 30.0, dtype=np.float32)
    result = _compute_thermal_celsius(
        thermal, 1200.0, 700.0, 1280.0, 720.0, 1280, 720,
        {"off_x": 0.5, "off_y": 0.5, "scale_x": 1.0, "scale_y": 1.0},
    )
    assert result is None


def test_payload_carries_source_frame_size():
    """WS payload 要帶 bbox 的座標系尺寸。

    前端不能拿 <video> 的 videoWidth 當分母：熱像那條串流是 640x480 而 bbox 是
    rgb 1280x720 的座標，除錯了框會整片位移（這就是熱像 bbox 位移的根因）。
    """
    from inference.pipeline import FrameData, InferencePipeline
    import numpy as np
    p = InferencePipeline()
    sent = []

    async def fake_broadcast(cam, payload):
        sent.append((cam, payload))

    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [np.ones((1, 7), dtype=np.float32)]
    p._detector = mock_detector
    p._reid = MagicMock()
    p._reid.extract.return_value = None
    p._tracker_pool = MagicMock()
    p._tracker_pool.update.return_value = []
    from concurrent.futures import ThreadPoolExecutor
    p._executor = ThreadPoolExecutor(max_workers=1)
    p._event_loop = asyncio.new_event_loop()
    p._broadcast_fn = fake_broadcast

    rgb = np.zeros((720, 1280, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    # broadcast 是 run_coroutine_threadsafe 丟進 loop 的，要讓 loop 跑一下才會執行。
    p._event_loop.run_until_complete(asyncio.sleep(0.05))
    p._event_loop.close()
    p._executor.shutdown(wait=False)

    assert sent, "沒有送出任何 payload"
    _, payload = sent[0]
    assert payload["frame_width"] == 1280
    assert payload["frame_height"] == 720


def test_update_frame_records_source_frame_size():
    from inference.pipeline import InferencePipeline
    import numpy as np
    p = InferencePipeline()
    p.update_frame("cam_01", np.zeros((720, 1280, 3), dtype=np.uint8), None, 1.0, 1)
    assert p.frame_sizes()["cam_01"] == [1280, 720]


def test_compute_thermal_celsius_returns_none_when_no_thermal():
    from inference.pipeline import _compute_thermal_celsius
    result = _compute_thermal_celsius(None, 0.0, 0.0, 50.0, 50.0, 640, 480)
    assert result is None


def test_compute_thermal_celsius_rejects_colour_image():
    """舊擷取端送的是上色後的 BGR 圖，對它取平均得到的是顏色不是溫度。

    turbo/jet 的亮度不單調（綠色比紅色亮），所以那個數字跟溫度連單調關係都
    沒有。寧可回 None 讓體溫欄位空著，也不要回報一個看起來很像溫度的假數字。
    """
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    bgr = np.full((120, 160, 3), 200, dtype=np.uint8)
    assert _compute_thermal_celsius(bgr, 0.0, 0.0, 640.0, 480.0, 640, 480) is None


def test_compute_thermal_celsius_clamps_bbox_to_image_bounds():
    import numpy as np
    from inference.pipeline import _compute_thermal_celsius
    thermal = np.full((120, 160), 30.0, dtype=np.float32)
    # bbox 超出 rgb 畫面邊界
    result = _compute_thermal_celsius(thermal, 0.0, 0.0, 800.0, 600.0, 640, 480)
    assert result == pytest.approx(30.0)


def test_set_active_false_skips_detector():
    from inference.pipeline import FrameData, InferencePipeline
    import numpy as np
    p = InferencePipeline()
    mock_detector = MagicMock()
    mock_detector.test_size = (736, 1280)
    mock_detector.infer.return_value = [np.ones((1, 7), dtype=np.float32)]
    p._detector = mock_detector
    p._reid = MagicMock()
    p._tracker_pool = MagicMock()

    p.set_active(False)
    rgb = np.zeros((480, 640, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    mock_detector.infer.assert_not_called()

    # 恢復 active 後會呼叫 detector（驗證 gate 不是永久關）
    p.set_active(True)
    # detector 真的被叫到即可（後續 reid/tracker 為 MagicMock，不深究結果）
    try:
        p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                              ts=1.0, frame_id=1)})
    except Exception:
        pass
    mock_detector.infer.assert_called()


# ── 遮罩過濾 ─────────────────────────────────────────────────────────
# 遮罩是唯一碰推論路徑的功能。這裡驗的是「有沒有真的接上」，
# 過濾本身的邊界正確性由 tests/test_mask_filter.py 負責。

_LEFT_HALF = [{"label": "走道", "enabled": True,
               "points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]]}]


def _pipeline_with_dets(dets):
    """detector 回指定的偵測框，其餘照 _make_processing_pipeline。"""
    import numpy as np
    p, mock_detector, mock_tracker_pool, _ = _make_processing_pipeline()
    mock_detector.infer.return_value = [np.asarray(dets, dtype=np.float32)]
    return p, mock_tracker_pool


def _dets_given_to_tracker(mock_tracker_pool):
    return mock_tracker_pool.update.call_args[0][1]


def test_masked_detection_never_reaches_tracker():
    import numpy as np
    from inference.pipeline import FrameData
    # test_size=(736,1280)、畫面 100x100 → scale=7.36，dets 用縮放後座標
    inside = [10 * 7.36, 10 * 7.36, 40 * 7.36, 40 * 7.36, 0.9, 0.9, 0.0]
    outside = [60 * 7.36, 10 * 7.36, 90 * 7.36, 40 * 7.36, 0.9, 0.9, 0.0]
    p, pool = _pipeline_with_dets([inside, outside])
    p.set_masks("cam_01", _LEFT_HALF)
    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    given = _dets_given_to_tracker(pool)
    assert len(given) == 1, "遮罩內的偵測不該進 tracker"
    assert given[0][0] > 300, "留下來的應該是界外那一個"


def test_global_switch_off_disables_filtering():
    """遮罩把真的豬吃掉時的一鍵復原：總開關關掉就完全不過濾。"""
    import numpy as np
    from inference.pipeline import FrameData
    inside = [10 * 7.36, 10 * 7.36, 40 * 7.36, 40 * 7.36, 0.9, 0.9, 0.0]
    p, pool = _pipeline_with_dets([inside])
    p.set_masks("cam_01", _LEFT_HALF)
    p.set_mask_enabled(False)
    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    assert len(_dets_given_to_tracker(pool)) == 1


def test_mask_of_another_camera_does_not_leak():
    import numpy as np
    from inference.pipeline import FrameData
    inside = [10 * 7.36, 10 * 7.36, 40 * 7.36, 40 * 7.36, 0.9, 0.9, 0.0]
    p, pool = _pipeline_with_dets([inside])
    p.set_masks("cam_99", _LEFT_HALF)
    rgb = np.zeros((100, 100, 3), dtype=np.uint8)
    p._process_batch({"cam_01": FrameData(rgb_np=rgb, thermal_np=None,
                                          ts=1.0, frame_id=1)})
    assert len(_dets_given_to_tracker(pool)) == 1


def test_raster_is_cached_and_invalidated_on_change():
    from inference.pipeline import InferencePipeline
    p = InferencePipeline()
    p.set_masks("cam_01", _LEFT_HALF)
    first = p._mask_for("cam_01", 100, 100)
    assert p._mask_for("cam_01", 100, 100) is first, "同一組遮罩不該每幀重畫"
    p.set_masks("cam_01", [{"label": "x", "enabled": True,
                            "points": [[0.6, 0], [1, 0], [1, 1], [0.6, 1]]}])
    assert p._mask_for("cam_01", 100, 100) is not first, "改遮罩後快取要失效"


def test_no_mask_configured_returns_none():
    from inference.pipeline import InferencePipeline
    p = InferencePipeline()
    assert p._mask_for("cam_01", 100, 100) is None
    p.set_masks("cam_01", [])
    assert p._mask_for("cam_01", 100, 100) is None
