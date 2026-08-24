"""熱像對位參數的純函式測試。

這組數字同時決定「熱像畫面上框畫在哪」與「體溫從熱像的哪一塊取樣」，
所以它們不是視覺調整：存錯會讓寫進 DB 的體溫跟著錯，而且錯得很安靜
（數字照樣有、範圍照樣合理，只是取自隔壁那塊）。
"""
import pytest

import thermal_align


def test_missing_or_empty_is_identity():
    """沒有校正過的相機必須跟校正功能出現之前的行為完全一樣。"""
    assert thermal_align.normalize(None) == thermal_align.IDENTITY
    assert thermal_align.normalize({}) == thermal_align.IDENTITY
    assert thermal_align.is_identity(None)


def test_partial_dict_fills_the_rest_with_identity():
    a = thermal_align.normalize({"off_x": 0.1})
    assert a["off_x"] == pytest.approx(0.1)
    assert a["scale_x"] == pytest.approx(1.0)
    assert a["off_y"] == pytest.approx(0.0)


def test_garbage_values_fall_back_instead_of_raising():
    """讀取路徑碰到怪值時要照樣算得出來。

    存進去的東西壞掉是一回事，讓整台相機的框從畫面上消失是另一回事——
    後者比較嚴重，而且看起來像偵測掛了。
    """
    a = thermal_align.normalize({"off_x": "abc", "scale_y": None, "off_y": True})
    assert a == thermal_align.IDENTITY


def test_validate_rejects_out_of_range():
    _, err = thermal_align.validate({"off_x": 0.9})
    assert err is not None
    _, err = thermal_align.validate({"scale_x": 0.0})
    assert err is not None
    _, err = thermal_align.validate({"off_x": 0.1, "scale_y": 1.2})
    assert err is None


def test_map_box_identity_is_plain_proportional_scaling():
    # rgb 1280x720 的右下 1/4，對到 160x120 熱像的右下 1/4
    box = thermal_align.map_box(640, 360, 1280, 720, 1280, 720, 160, 120)
    assert box == pytest.approx((80.0, 60.0, 160.0, 120.0))


def test_map_box_offset_shifts_by_fraction_of_the_image():
    """平移的單位是「整張圖的比例」，不是像素。

    用像素的話同一組參數換一台解析度不同的相機就得重新校正，而鏡頭根本沒動。
    """
    box = thermal_align.map_box(
        0, 0, 640, 360, 1280, 720, 160, 120,
        {"off_x": 0.25, "off_y": 0.5, "scale_x": 1.0, "scale_y": 1.0},
    )
    assert box == pytest.approx((40.0, 60.0, 120.0, 120.0))


def test_map_box_scale_changes_box_size():
    box = thermal_align.map_box(
        0, 0, 640, 360, 1280, 720, 160, 120,
        {"off_x": 0.0, "off_y": 0.0, "scale_x": 0.5, "scale_y": 0.5},
    )
    assert box == pytest.approx((0.0, 0.0, 40.0, 30.0))


def test_map_box_zero_source_size_does_not_divide_by_zero():
    assert thermal_align.map_box(0, 0, 10, 10, 0, 0, 160, 120) == (0.0, 0.0, 0.0, 0.0)
