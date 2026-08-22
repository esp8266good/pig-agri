"""遮罩過濾的邊界行為。

遮罩不塗黑任何影像：detector 照常在完整畫面上跑，只是與遮罩重疊過多的偵測框
被丟掉、不進 ReID 也不進 tracker。設計理由（為什麼不塗黑、為什麼不只看中心點）
見 docs/superpowers/specs/2026-08-22-focus-list-mask-notify-manual-design.md。
"""
import numpy as np
import pytest

from mask_filter import (
    MASK_OVERLAP_THRESHOLD,
    filter_detections,
    overlap_ratio,
    rasterize,
)

# 100x100 的畫面，左半邊被遮住（x 0..0.5）
LEFT_HALF = [{"points": [[0, 0], [0.5, 0], [0.5, 1], [0, 1]], "enabled": True}]


def _det(x1, y1, x2, y2, score=0.9):
    """detector 的輸出格式：前四欄是 xyxy。"""
    return [x1, y1, x2, y2, score, score, 0.0]


def test_rasterize_marks_only_the_polygon():
    m = rasterize(LEFT_HALF, width=100, height=100)
    assert m.shape == (100, 100)
    assert m[50, 10] == 1, "左半邊在遮罩內"
    assert m[50, 90] == 0, "右半邊不在"


def test_rasterize_ignores_disabled_regions():
    """單獨停用一塊區域，是出問題時二分定位用的，必須真的不生效。"""
    regions = [{"points": LEFT_HALF[0]["points"], "enabled": False}]
    assert rasterize(regions, 100, 100).sum() == 0


def test_rasterize_unions_overlapping_regions():
    regions = [
        {"points": [[0, 0], [0.6, 0], [0.6, 1], [0, 1]], "enabled": True},
        {"points": [[0.4, 0], [1, 0], [1, 1], [0.4, 1]], "enabled": True},
    ]
    assert rasterize(regions, 100, 100).sum() == 100 * 100


def test_rasterize_with_no_regions_is_empty():
    assert rasterize([], 100, 100).sum() == 0


def test_rasterize_drops_degenerate_polygons():
    """少於三個頂點畫不出面積，前端誤送也不能讓 raster 爆掉。"""
    assert rasterize([{"points": [[0, 0], [1, 1]], "enabled": True}], 100, 100).sum() == 0


def test_overlap_fully_inside_is_one():
    m = rasterize(LEFT_HALF, 100, 100)
    assert overlap_ratio((10, 10, 40, 40), m) == pytest.approx(1.0, abs=0.02)


def test_overlap_fully_outside_is_zero():
    m = rasterize(LEFT_HALF, 100, 100)
    assert overlap_ratio((60, 10, 90, 40), m) == pytest.approx(0.0, abs=0.02)


def test_overlap_counts_two_regions_together():
    """一隻豬同時壓到兩塊遮罩，合計的重疊才是判準，不是各算各的。"""
    regions = [
        {"points": [[0, 0], [0.3, 0], [0.3, 1], [0, 1]], "enabled": True},
        {"points": [[0.7, 0], [1, 0], [1, 1], [0.7, 1]], "enabled": True},
    ]
    m = rasterize(regions, 100, 100)
    # bbox 橫跨 0..100，兩塊各佔 30 → 合計 60%
    assert overlap_ratio((0, 0, 100, 100), m) == pytest.approx(0.6, abs=0.03)


# ── 門檻 ─────────────────────────────────────────────────────────────
# 60% 寫死，不做成設定：它是一個沒人有直覺的數字，開放出來只會被亂調
# 然後回報「遮罩壞了」。用重疊比例而不是中心點，是因為只看中心點時，
# 一隻豬走到遮罩邊界會閃爍進出，把軌跡切碎 → 活動量算錯。

def test_below_threshold_is_kept():
    m = rasterize(LEFT_HALF, 100, 100)
    # bbox x 0..85：遮住 0..50，重疊 50/85 ≈ 0.59
    dets = np.array([_det(0, 0, 85, 100)], dtype=np.float32)
    assert len(filter_detections(dets, m, scale=1.0)) == 1


def test_above_threshold_is_dropped():
    m = rasterize(LEFT_HALF, 100, 100)
    # bbox x 0..82：重疊 50/82 ≈ 0.61
    dets = np.array([_det(0, 0, 82, 100)], dtype=np.float32)
    assert len(filter_detections(dets, m, scale=1.0)) == 0


def test_threshold_constant_is_sixty_percent():
    assert MASK_OVERLAP_THRESHOLD == 0.6


def test_filter_keeps_original_row_order_and_values():
    m = rasterize(LEFT_HALF, 100, 100)
    dets = np.array([_det(60, 0, 90, 50, 0.7),      # 界外，留
                     _det(0, 0, 40, 50, 0.8),       # 界內，丟
                     _det(70, 50, 95, 90, 0.9)],    # 界外，留
                    dtype=np.float32)
    out = filter_detections(dets, m, scale=1.0)
    assert len(out) == 2
    assert out[0][4] == pytest.approx(0.7)
    assert out[1][4] == pytest.approx(0.9)


def test_filter_applies_detector_scale():
    """dets 是 detector 縮放後的座標，遮罩是原始畫面的座標，比對前要換算回去。"""
    m = rasterize(LEFT_HALF, 100, 100)
    # scale=0.5 → dets 座標是原始的一半；原始 0..40 落在遮罩內
    dets = np.array([_det(0, 0, 20, 25)], dtype=np.float32)
    assert len(filter_detections(dets, m, scale=0.5)) == 0


def test_empty_and_none_detections_pass_through():
    m = rasterize(LEFT_HALF, 100, 100)
    assert filter_detections(None, m, scale=1.0) is None
    assert len(filter_detections(np.zeros((0, 7), dtype=np.float32), m, 1.0)) == 0


def test_no_mask_keeps_everything():
    dets = np.array([_det(0, 0, 40, 50)], dtype=np.float32)
    assert len(filter_detections(dets, None, scale=1.0)) == 1
    empty = rasterize([], 100, 100)
    assert len(filter_detections(dets, empty, scale=1.0)) == 1


def test_zero_area_bbox_is_never_dropped():
    """退化的框不該因為除以零就被當成完全重疊丟掉。"""
    m = rasterize(LEFT_HALF, 100, 100)
    dets = np.array([_det(10, 10, 10, 10)], dtype=np.float32)
    assert len(filter_detections(dets, m, scale=1.0)) == 1
