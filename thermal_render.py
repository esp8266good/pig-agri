"""把 Y16 溫度場轉成看得懂的熱像圖。

擷取端（rpi5 sender，mode=y16_png）送過來的是原生 160x120 的原始溫度矩陣，
不是圖。要餵進 HLS 給人看，得在這裡上色、放大。放在 server 而不是擷取端做，
是因為只有原始溫度算得出每隻豬的體溫；擷取端一旦上色，溫度就再也還原不回來
（turbo colormap 的亮度不單調，綠色比紅色亮，RGB 反推溫度是不可能的）。

⚠ 上色用的是**固定溫度範圍**，不是每幀的 min/max。舊的擷取端做法是每幀重算
1%/99% percentile 再拉滿 0~255，那有兩個後果：
  1. 顏色失去絕對意義。同一隻豬在不同幀是不同顏色，畫面之間無法比較——
     而「看得出哪裡熱」正是熱像唯一的用途。
  2. 畫面裡任何東西一動，拉伸係數就變，整張圖所有像素跟著跳色。對 H.264
     而言等於每幀都是全新畫面，P-frame 一點都省不下來。
固定範圍兩個問題一起解決：實測相鄰幀平均像素差從 1.03 降到 0.23。
"""
from __future__ import annotations

import cv2
import numpy as np

# Y16 是 TLinear：Kelvin x100。
_KELVIN_OFFSET = 273.15
_Y16_SCALE = 100.0


def y16_to_celsius(y16: np.ndarray) -> np.ndarray:
    """Kelvin x100 的 uint16 → 攝氏 float32。"""
    return y16.astype(np.float32) / _Y16_SCALE - _KELVIN_OFFSET


def celsius_to_preview(
    temp_c: np.ndarray,
    *,
    lo_c: float,
    hi_c: float,
    out_w: int,
    out_h: int,
    colormap: int = cv2.COLORMAP_INFERNO,
    overlay: str | None = None,
) -> np.ndarray:
    """攝氏溫度場 → BGR 熱像圖。

    放大用 INTER_LINEAR 而不是 LANCZOS4：從 160x120 放大好幾倍時 Lanczos 會沿著
    溫度邊緣製造 ringing，那些是插值假紋理不是資料，卻要 H.264 花位元去編它。

    colormap 預設 inferno 而非 turbo/jet：後兩者在色輪上繞圈，溫度的一點抖動
    會讓像素在色相上跳很遠，chroma 子採樣完全壓不下來。inferno 的色相單調
    （黑→紫→橘→黃），亮度也單調遞增，順便讓沒有色彩概念的人也讀得出高低。
    """
    span = max(hi_c - lo_c, 1e-6)
    img8 = np.clip((temp_c - lo_c) * 255.0 / span, 0, 255).astype(np.uint8)
    color = cv2.applyColorMap(img8, colormap)
    if (out_w, out_h) != (color.shape[1], color.shape[0]):
        color = cv2.resize(color, (out_w, out_h), interpolation=cv2.INTER_LINEAR)
    if overlay:
        # 疊在圖上而不是溫度矩陣上：在矩陣上畫字等於直接竄改那幾個像素的溫度。
        cv2.rectangle(color, (6, 6), (min(out_w - 6, 470), 30), (0, 0, 0), -1)
        cv2.putText(color, overlay, (11, 24),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 255), 1, cv2.LINE_AA)
    return color


def encode_jpeg(bgr: np.ndarray, quality: int) -> bytes:
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), int(quality)])
    if not ok:
        raise RuntimeError("thermal preview JPEG encode failed")
    return buf.tobytes()
