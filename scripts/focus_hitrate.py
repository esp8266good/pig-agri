#!/usr/bin/env python3
"""量關注清單的命中率：清單上的編號，有幾個現在真的在畫面上。

這是 2026-08-27「關注清單只列現在畫面上的豬」那次改版的驗收指標。
改版前的正式機實測是 10 個名字裡只有 1 個在畫面上（rpi5_dual）。

⚠ 農場夜間關燈，rgb 全黑就偵測不到豬，這支腳本會量到一片零。
   要在有光的時候跑（約 06:30~17:00）。

用法（在跑 app 的那台機器上）：
    python3 scripts/focus_hitrate.py --camera rpi5_dual
"""
import argparse
import json
import sys
import time
import urllib.request


def _get(base, path):
    with urllib.request.urlopen(f"{base}{path}", timeout=10) as r:
        return json.load(r)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://localhost:5005")
    ap.add_argument("--camera", default="rpi5_dual")
    # 「現在在畫面上」的定義要跟 presence.DEFAULT_HOLD_SECONDS 同一個尺度。
    ap.add_argument("--window", type=float, default=10.0)
    a = ap.parse_args()

    try:
        _get(a.base, "/cameras")
    except Exception as e:
        print(f"app 連不上：{e}")
        return 2

    now = time.time()
    logs = _get(a.base, f"/tracking/{a.camera}?start={now - a.window}&end={now}")["logs"]
    on = {lg["object_id"] for lg in logs}
    focus = _get(a.base, f"/alerts/focus?camera_id={a.camera}")
    cache = _get(a.base, f"/alerts/active?camera_id={a.camera}")["cache"].get(a.camera, {})

    ids = [i["object_id"] for i in focus["items"]]
    hits = [i for i in ids if i in on]
    recent = [(i["object_id"], round(i["gone_seconds"], 1)) for i in focus["recent"]]

    print(f"camera            {a.camera}")
    print(f"status            {focus['status']}")
    print(f"快取編號數        {len(cache)}")
    print(f"最近 {a.window:g} 秒畫面上   {len(on)}")
    print(f"on_screen_count   {focus['on_screen_count']}  （後端數的，要接近上一行）")
    print(f"清單列出          {len(ids)}  {ids}")
    print(f"其中在畫面上      {len(hits)}  {hits}")
    print(f"最近消失          {recent}")

    # tracklet 活多久。activity_min_span_seconds（預設 300）是「算不算得出活動量」
    # 的絕對門檻：跨度不到就回 None，那隻豬既不當基準也不會被判定為異常。
    # 一個都過不了門檻時，median 算不出來 → herd_ok False → 清單一律讓位給
    # 「豬群活動量普遍偏低」，這時命中率量到的是零，但根因不在這次改版。
    win = _get(a.base, "/settings")
    win_min = float(win.get("analysis_window_minutes") or 15)
    logs2 = _get(
        a.base,
        f"/tracking/{a.camera}?start={now - win_min * 60}&end={now}",
    )["logs"]
    spans: dict[int, list] = {}
    for lg in logs2:
        spans.setdefault(lg["object_id"], []).append(lg["timestamp"])
    vals = sorted(max(v) - min(v) for v in spans.values())
    print()
    print(f"分析視窗          {win_min:g} 分鐘")
    print(f"視窗內 tracklet   {len(vals)}")
    if vals:
        print(f"span 中位數       {vals[len(vals) // 2]:.0f} 秒（最長 {vals[-1]:.0f}）")
        ok300 = sum(1 for v in vals if v >= 300)
        print(f"span >= 300 秒    {ok300}  （算得出活動量的隻數；0 就整欄不評估）")

    if focus["status"] != "ok":
        print("\n判定：清單沒在給名字（首次分析未完成，或全欄活動量偏低）。")
        print("      不是壞掉，但也還量不到命中率，等下一輪分析或等天亮。")
        return 0
    if not ids:
        print("\n判定：清單是空的。畫面上有豬但都沒事，或還沒偵測到豬。")
        return 0
    rate = len(hits) / len(ids)
    print(f"\n命中率            {len(hits)}/{len(ids)} = {rate:.0%}")
    print("判定：改版前是 1/10 = 10%。100% 才是這次改版該有的樣子——")
    print("      清單本來就只從畫面上的豬裡挑，掉下來的每一個都要有解釋")
    print("      （最可能是那一隻剛好在這 10 秒內離開畫面）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
