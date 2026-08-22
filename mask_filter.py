"""遮罩：丟掉與指定區域重疊過多的偵測框。

遮罩**不改變任何影像**。錄影、直播、回放看到的畫面完全不受影響：
detector 照常在完整畫面上跑，只是輸出的偵測框若與遮罩重疊過多就被丟棄，
不進 ReID、也不進 tracker。

為什麼不把遮罩區塗黑再送進 detector：塗黑會製造一條假的高對比邊緣，
YOLO 有機會在邊界產生新的假偵測；而且遮罩區旁邊的真豬會少掉一部分身體的
視覺 context，ReID feature 跟著變髒。省下的計算量不是這裡的目標。

為什麼用重疊比例而不是只看 bbox 中心點：只看中心點時，一隻豬走到遮罩邊界
會閃爍進出，把軌跡切碎，而軌跡完整性直接決定活動量算得對不對。
"""
from typing import Optional

import cv2
import numpy as np

# 偵測框與遮罩重疊超過這個比例就丟棄。
# 刻意寫死不做成設定：這是一個沒人有直覺的數字，開放出來只會被亂調，
# 然後回報「遮罩壞了」。需要調整時改這裡再重啟。
MASK_OVERLAP_THRESHOLD: float = 0.6


def rasterize(regions: list[dict], width: int, height: int) -> np.ndarray:
    """把多邊形區域畫成一張 0/1 的遮罩圖。

    regions 的 points 是正規化的 0..1 座標（換相機解析度不用重畫），
    這裡才乘上實際畫面尺寸。停用的區域不畫——單獨關掉一塊來二分定位問題，
    是這個設計的重點之一。
    """
    mask = np.zeros((height, width), dtype=np.uint8)
    polys = []
    for region in regions or []:
        if not region.get("enabled", True):
            continue
        pts = region.get("points") or []
        if len(pts) < 3:          # 少於三個頂點畫不出面積
            continue
        arr = np.array(
            [[float(x) * width, float(y) * height] for x, y in pts],
            dtype=np.float32,
        )
        polys.append(np.round(arr).astype(np.int32))
    # 一定要一個一個畫。cv2.fillPoly 一次收多個多邊形時走的是 even-odd 規則，
    # 兩塊遮罩重疊的部分會互相抵消變成沒遮到——正好是「走道和牆角各畫一塊、
    # 邊界稍微交疊」這種最常見的畫法會踩到的坑。
    for poly in polys:
        cv2.fillPoly(mask, [poly], 1)
    return mask


def overlap_ratio(bbox, mask: np.ndarray) -> float:
    """bbox（原始畫面像素座標的 x1,y1,x2,y2）落在遮罩內的面積比例。"""
    h, w = mask.shape[:2]
    x1 = max(0, min(w, int(round(bbox[0]))))
    y1 = max(0, min(h, int(round(bbox[1]))))
    x2 = max(0, min(w, int(round(bbox[2]))))
    y2 = max(0, min(h, int(round(bbox[3]))))
    area = (x2 - x1) * (y2 - y1)
    if area <= 0:
        # 退化的框：沒有面積就談不上重疊，一律保留而不是除以零。
        return 0.0
    return float(mask[y1:y2, x1:x2].sum()) / float(area)


def filter_detections(
    dets: Optional[np.ndarray],
    mask: Optional[np.ndarray],
    scale: float,
    threshold: float = MASK_OVERLAP_THRESHOLD,
) -> Optional[np.ndarray]:
    """丟掉與遮罩重疊超過門檻的偵測框。

    dets 是 detector 輸出、座標在縮放後的空間；遮罩是原始畫面的座標，
    所以比對前要先 `/ scale` 換算回去（與 pipeline 餵給 ReID 的換算一致）。
    沒有遮罩、或遮罩全空時原樣回傳，不做任何多餘計算。
    """
    if dets is None or len(dets) == 0:
        return dets
    if mask is None or not mask.any():
        return dets
    keep = [
        i for i in range(len(dets))
        if overlap_ratio(dets[i, :4] / scale, mask) <= threshold
    ]
    if len(keep) == len(dets):
        return dets
    return dets[keep]
