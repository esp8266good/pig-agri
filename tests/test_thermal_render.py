"""Y16 溫度場 → 熱像圖的純函式測試。

這個模組是「溫度」與「看得見的顏色」之間唯一的轉換點，兩個方向都要釘住：

  · y16_to_celsius 錯了，寫進 tracking_logs.thermal_celsius 的體溫就整片偏移，
    而且錯得很安靜（數字照樣有、範圍照樣像體溫）。
  · celsius_to_preview 的「固定溫度範圍」是刻意的設計，不是還沒調完的參數。
    改回每幀算 min/max 會讓同一隻豬在不同幀變成不同顏色，而且畫面裡任何東西
    一動整張圖就跳色、H.264 的 P-frame 全部作廢。這裡用「同一個溫度在不同畫面
    要得到同一個顏色」把那件事釘死，讓改回去的人會看到紅燈而不是只看到變糊。
"""
import cv2
import numpy as np
import pytest

import thermal_render


# ── y16_to_celsius ────────────────────────────────────────────────

def test_y16_to_celsius_known_points():
    """Y16 是 Kelvin x100。0°C 與 100°C 的刻度直接寫死，換算改了會亮。"""
    y16 = np.array([[27315, 37315]], dtype=np.uint16)
    out = thermal_render.y16_to_celsius(y16)
    assert out.shape == (1, 2)
    assert out[0, 0] == pytest.approx(0.0, abs=1e-3)
    assert out[0, 1] == pytest.approx(100.0, abs=1e-3)


def test_y16_to_celsius_pig_body_temperature():
    """豬的體溫落在 38~40°C，這是這條路徑真正在用的範圍。"""
    y16 = np.array([[31165]], dtype=np.uint16)   # 311.65K
    assert thermal_render.y16_to_celsius(y16)[0, 0] == pytest.approx(38.5, abs=1e-3)


def test_y16_to_celsius_returns_float32_not_integer():
    """回 float32：uint16 做減法會 wrap around，低於 273.15K 的像素會變成六萬多。"""
    y16 = np.array([[27000]], dtype=np.uint16)   # 270K，低於冰點
    out = thermal_render.y16_to_celsius(y16)
    assert out.dtype == np.float32
    assert out[0, 0] < 0


def test_y16_to_celsius_does_not_mutate_input():
    y16 = np.array([[31165]], dtype=np.uint16)
    before = y16.copy()
    thermal_render.y16_to_celsius(y16)
    assert np.array_equal(y16, before)


# ── celsius_to_preview：固定溫度範圍 ──────────────────────────────

def _flat(temp_c: float, h: int = 4, w: int = 4) -> np.ndarray:
    return np.full((h, w), temp_c, dtype=np.float32)


def test_same_temperature_gets_same_colour_across_frames():
    """這是固定溫度範圍存在的理由。

    兩張畫面裡其他東西的溫度天差地遠，但都有一塊 38°C。那一塊必須是同一個顏色，
    否則「看得出哪裡熱」這件事就不成立了。改回每幀算 min/max 這條會紅。
    """
    cold = _flat(26.0); cold[0, 0] = 38.0
    hot = _flat(39.5); hot[0, 0] = 38.0
    kw = dict(lo_c=25.0, hi_c=40.0, out_w=4, out_h=4)
    a = thermal_render.celsius_to_preview(cold, **kw)
    b = thermal_render.celsius_to_preview(hot, **kw)
    assert tuple(a[0, 0]) == tuple(b[0, 0])


def test_out_of_range_temperatures_clamp_instead_of_wrapping():
    """低於 lo_c / 高於 hi_c 要夾住。沒夾的話 astype(uint8) 會 wrap：
    50°C（超過上限）會繞回深色，看起來比常溫還冷。"""
    kw = dict(lo_c=25.0, hi_c=40.0, out_w=2, out_h=2)
    freezing = thermal_render.celsius_to_preview(_flat(-40.0, 2, 2), **kw)
    at_lo = thermal_render.celsius_to_preview(_flat(25.0, 2, 2), **kw)
    scorching = thermal_render.celsius_to_preview(_flat(200.0, 2, 2), **kw)
    at_hi = thermal_render.celsius_to_preview(_flat(40.0, 2, 2), **kw)
    assert tuple(freezing[0, 0]) == tuple(at_lo[0, 0])
    assert tuple(scorching[0, 0]) == tuple(at_hi[0, 0])


def test_hotter_is_brighter():
    """inferno 的亮度單調遞增（黑→紫→橘→黃），沒有色彩概念的人也讀得出高低。
    換成 turbo/jet 這條會紅：那兩個的綠比紅亮。"""
    kw = dict(lo_c=25.0, hi_c=40.0, out_w=2, out_h=2)
    lums = [
        float(cv2.cvtColor(
            thermal_render.celsius_to_preview(_flat(t, 2, 2), **kw),
            cv2.COLOR_BGR2GRAY,
        )[0, 0])
        for t in (26.0, 30.0, 34.0, 39.0)
    ]
    assert lums == sorted(lums)
    assert lums[-1] > lums[0]


def test_zero_width_range_does_not_divide_by_zero():
    """lo_c == hi_c 是設定打錯就會發生的事，不可以炸掉整條熱像。"""
    out = thermal_render.celsius_to_preview(
        _flat(30.0), lo_c=30.0, hi_c=30.0, out_w=4, out_h=4
    )
    assert out.shape == (4, 4, 3)


# ── celsius_to_preview：尺寸與疊字 ────────────────────────────────

def test_resizes_to_requested_output_size():
    """擷取端送的是 160x120，餵進 HLS 的是放大後的畫面。"""
    out = thermal_render.celsius_to_preview(
        _flat(30.0, 120, 160), lo_c=25.0, hi_c=40.0, out_w=640, out_h=480
    )
    assert out.shape == (480, 640, 3)


def test_no_resize_when_size_already_matches():
    out = thermal_render.celsius_to_preview(
        _flat(30.0, 8, 8), lo_c=25.0, hi_c=40.0, out_w=8, out_h=8
    )
    assert out.shape == (8, 8, 3)


def test_overlay_draws_on_the_image_not_on_the_temperature_field():
    """疊字畫在上色後的圖上。畫在溫度矩陣上等於竄改那幾個像素的溫度，
    而那些像素之後還要拿來算體溫。"""
    temp = _flat(30.0, 120, 160)
    before = temp.copy()
    plain = thermal_render.celsius_to_preview(
        temp, lo_c=25.0, hi_c=40.0, out_w=640, out_h=480
    )
    labelled = thermal_render.celsius_to_preview(
        temp, lo_c=25.0, hi_c=40.0, out_w=640, out_h=480, overlay="25-40C"
    )
    assert np.array_equal(temp, before)          # 輸入沒被動到
    assert not np.array_equal(plain, labelled)   # 疊字真的畫上去了
    # 疊字只佔左上角那一條，畫面下半部不受影響。
    assert np.array_equal(plain[200:, :], labelled[200:, :])


def test_overlay_box_stays_inside_a_narrow_output():
    """out_w 比疊字底板還窄時不可以畫到畫面外（cv2 不會擋，只會靜靜裁掉）。"""
    out = thermal_render.celsius_to_preview(
        _flat(30.0, 120, 160), lo_c=25.0, hi_c=40.0,
        out_w=160, out_h=120, overlay="25-40C",
    )
    assert out.shape == (120, 160, 3)


# ── encode_jpeg ───────────────────────────────────────────────────

def test_encode_jpeg_returns_jpeg_bytes():
    bgr = np.zeros((16, 16, 3), dtype=np.uint8)
    data = thermal_render.encode_jpeg(bgr, 80)
    assert isinstance(data, bytes)
    assert data[:2] == b"\xff\xd8"          # JPEG SOI
    assert cv2.imdecode(np.frombuffer(data, np.uint8),
                        cv2.IMREAD_COLOR).shape == (16, 16, 3)


def test_encode_jpeg_quality_changes_size():
    """品質參數真的有傳進去（傳錯的話兩個大小會一樣）。"""
    rng = np.random.default_rng(0)
    bgr = rng.integers(0, 255, (64, 64, 3), dtype=np.uint8)
    assert len(thermal_render.encode_jpeg(bgr, 95)) > \
           len(thermal_render.encode_jpeg(bgr, 20))


def test_encode_jpeg_propagates_cv2_error_on_bad_input():
    """空影像讓 cv2 自己丟 cv2.error，不要吞掉改回空 bytes：
    空 bytes 會變成一段空白畫面被餵進 HLS，而 ffmpeg 對壞掉的 JPEG 是靜靜跳過的。"""
    with pytest.raises(cv2.error):
        thermal_render.encode_jpeg(np.zeros((0, 0, 3), dtype=np.uint8), 80)


def test_encode_jpeg_raises_when_imencode_reports_failure(monkeypatch):
    """cv2 回 ok=False（沒有丟例外，只是說失敗）時也要丟出來。
    這是模組裡那行 raise RuntimeError 唯一會走到的路徑。"""
    monkeypatch.setattr(
        thermal_render.cv2, "imencode", lambda *a, **k: (False, None)
    )
    with pytest.raises(RuntimeError, match="JPEG encode failed"):
        thermal_render.encode_jpeg(np.zeros((4, 4, 3), dtype=np.uint8), 80)
