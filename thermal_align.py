"""熱像對 RGB 的對位參數。

熱像與 RGB 是兩顆分開的鏡頭：視角不一樣、鏡頭位置也差幾公分，所以同一隻豬在
兩張圖上的位置不會重合。bbox 是在 RGB 上算出來的，要拿到熱像上用（畫框、或取
那隻豬的體溫）就得先換算過去。

換算用最簡單的一組參數：先縮放、再平移，x 與 y 各自獨立。

    tx = off_x + nx * scale_x
    ty = off_y + ny * scale_y

座標一律是正規化的 0..1（跟遮罩同一套慣例），換相機解析度不用重新校正。

沒有校正過的相機用 identity（off=0、scale=1），也就是「兩張圖視角完全相同」，
跟校正功能出現之前的行為一模一樣：新增這個機制不會動到任何既有相機的數字。

⚠ 這組參數同時餵給兩個地方：前端在熱像畫面上畫框，以及 `_compute_thermal_celsius`
取那隻豬的體溫。兩邊一定要用同一組，否則畫面上框對得很準、實際取樣的卻是隔壁那塊。

⛔ 沒有做自動校正。RGB 與熱像是不同的成像原理（反射光 vs 輻射），灰階分佈之間
沒有穩定的對應關係，一般的特徵點比對／互相關在這裡不成立；能用的是互資訊配準，
但本場地的條件把它擋死了：熱像只有 160x120、豬體在熱像上是一團沒有內部紋理的
均勻亮區，而夜間 RGB 全黑根本沒有影像可配。校正一次就能用很久（鏡頭固定不動），
不值得為此養一條會安靜給出錯誤結果的自動流程。
"""
from typing import Optional

IDENTITY: dict[str, float] = {
    "off_x": 0.0, "off_y": 0.0, "scale_x": 1.0, "scale_y": 1.0,
}

# 平移的合理範圍。超過半張圖就不是「對位」而是打錯字了，擋下來比默默存進去好。
OFF_MIN, OFF_MAX = -0.5, 0.5
# 縮放下限不設 0：scale=0 會讓每個 bbox 塌成一個點，取樣範圍變空、體溫全變 None。
SCALE_MIN, SCALE_MAX = 0.2, 3.0


def normalize(raw: Optional[dict]) -> dict[str, float]:
    """把任意來源（DB 列、HTTP body）整成四個 float，缺的補 identity。

    這裡只補與轉型，不做範圍檢查：範圍是輸入驗證的事（見 `validate`），
    而讀取路徑碰到早年存進去的怪值時，寧可照樣算也不要讓整台相機的框消失。
    """
    out = dict(IDENTITY)
    if not raw:
        return out
    for k in IDENTITY:
        v = raw.get(k)
        if v is None or isinstance(v, bool):
            continue
        try:
            out[k] = float(v)
        except (TypeError, ValueError):
            continue
    return out


def is_identity(align: Optional[dict]) -> bool:
    a = normalize(align)
    return all(abs(a[k] - IDENTITY[k]) < 1e-9 for k in IDENTITY)


def validate(raw: Optional[dict]) -> tuple[dict[str, float], Optional[str]]:
    """回傳 (參數, 錯誤訊息)。錯誤訊息為 None 代表通過。"""
    a = normalize(raw)
    for k in ("off_x", "off_y"):
        if not (OFF_MIN <= a[k] <= OFF_MAX):
            return a, f"{k} 必須落在 {OFF_MIN}~{OFF_MAX}"
    for k in ("scale_x", "scale_y"):
        if not (SCALE_MIN <= a[k] <= SCALE_MAX):
            return a, f"{k} 必須落在 {SCALE_MIN}~{SCALE_MAX}"
    return a, None


def map_box(
    x1: float, y1: float, x2: float, y2: float,
    rgb_w: float, rgb_h: float,
    dst_w: float, dst_h: float,
    align: Optional[dict] = None,
) -> tuple[float, float, float, float]:
    """把 RGB 像素座標的 bbox 換算成熱像（或任何目標畫布）的像素座標。

    rgb_w/rgb_h 是 bbox 所屬座標系的實際尺寸，dst_w/dst_h 是目標的實際尺寸。
    兩邊都必須傳實際值：把任何一邊寫死正是舊版體溫全錯的根因之一。
    """
    if rgb_w <= 0 or rgb_h <= 0:
        return (0.0, 0.0, 0.0, 0.0)
    a = normalize(align)
    nx1, nx2 = x1 / rgb_w, x2 / rgb_w
    ny1, ny2 = y1 / rgb_h, y2 / rgb_h
    tx1 = (a["off_x"] + nx1 * a["scale_x"]) * dst_w
    tx2 = (a["off_x"] + nx2 * a["scale_x"]) * dst_w
    ty1 = (a["off_y"] + ny1 * a["scale_y"]) * dst_h
    ty2 = (a["off_y"] + ny2 * a["scale_y"]) * dst_h
    return (tx1, ty1, tx2, ty2)
