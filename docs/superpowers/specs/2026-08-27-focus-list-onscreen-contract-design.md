# 關注清單改成「現在畫面上該看哪幾隻」

2026-08-27。分支 `fix/focus-list-onscreen`。

## 現況與交接（接手先讀這節）

**接手第一件事**：跑 `./scripts/check_progress.sh`。它的輸出永遠優先於這份文件
裡寫的任何數字。

**現在的狀態**：正式機已經跑在這個分支上（`git checkout fix/focus-list-onscreen`
＋重啟 service），**還沒併回 master**。要回滾就在正式機上 `git checkout master`
再重啟 service。

**在等什麼**：農場夜間關燈，rgb 全黑就偵測不到豬，量不到命中率。
`scripts/focus_hitrate_watch.sh` 已經在正式機上 detach 執行，排在
**2026-08-28 10:00** 起連量三次（間隔 35 分鐘，跨得過一輪分析），結果推 ntfy。

### 改版前的正式機基準（2026-08-27 16:45，rpi5_dual）

| 量測 | 數字 |
|---|---|
| 快取編號數 | 74 |
| 最近 10 秒真的在畫面上 | 21 |
| 關注清單列出的名字 | 10 |
| 其中真的在畫面上 | **1**（#76） |

清單上的編號是 23~111，畫面上的是 129~213，整批是不同世代的。

### 重啟後立刻驗到的兩件事（改版有效的部分）

`status` 仍然是 `not_analyzed`（on-screen 過濾沒有吃掉「首次分析尚未完成」
那個保護），`on_screen_count` 回 23、等於畫面上真實的隻數。這兩個回歸是在
正式機重啟那一刻才浮出來的，修在 `65aef58`。

### 明天要做的決定，以及先講好的判準

**主判準：命中率**（清單列出的名字裡，有幾個現在真的在畫面上）

| 命中率 | 結論 |
|---|---|
| ≥ 90% | 這輪有效，把分支併回 master |
| 50~90% | 有效但有東西沒想到。先查掉下來的那幾個是不是剛好落在 presence 的 10 秒容忍外 |
| < 50% | on-screen 過濾沒生效。查 presence 有沒有在寫（`inference/pipeline.py` 的 `mark_seen`） |

**⚠ 很可能量到的另一種結果：清單根本不給名字。**
2026-08-27 傍晚在正式機量到，最近 15 分鐘有 50 段 tracklet，span 中位數 135 秒、
最長 290 秒，**沒有任何一段達到 300 秒**。而 `activity_min_span_seconds=300` 是
「算不算得出活動量」的絕對門檻：一個都過不了 → median 算不出來 → `herd_ok`
False → 清單一律讓位給「豬群活動量普遍偏低」。

這個數字是黃昏光線變差時量的，可能是天色造成的，所以要在有光的時候重量一次。
`scripts/focus_hitrate.py` 已經會一起印 tracklet span 的分佈。

| 明天量到的 span ≥ 300 秒隻數 | 結論 |
|---|---|
| ≥ 2 | 傍晚那個數字是天色造成的，照主判準走 |
| 0~1（整天都這樣） | 施力點不在關注清單，在「活動量算不出來」。這時要重開一輪討論：`activity_min_span_seconds` 與 `analysis_window_minutes`（正式機是 15 分鐘、不是預設的 60）要怎麼配。⚠ 不要直接調小 min_span：跨度愈短、速率估計的雜訊愈大，而它是採血判斷的唯一依據 |

### 不要動的東西

- 正式機的 `analysis_interval_minutes`（30）：改它不會縮短當前那一輪的
  `asyncio.sleep`，只會讓下一輪起算，白等。
- 正式機上的 `git checkout master`：那會把驗收中的版本換掉，明天的量測就作廢。
- `scripts/focus_hitrate_watch.sh`：正在跑，bash 是逐段讀檔的，改它會讓執行中的
  那份錯亂。要改就先停掉再改再重跑。

## 症狀

使用者的原話：「關注清單和對照組都有 ID，但是畫面上完全不顯示這些 bbox，
只能靠週期性 reset 取得短暫的救贖。」

起因被歸給 MOT：HybridSORT 配 YOLOX 太容易跟丟，ID 暴增。負責 MOT 的成員
要一段時間才會更新演算法，在那之前問有沒有別的辦法。

## 量到的數字

本機歷史資料（`pig-agri-postgres-1`，cam_03，2026-08-15 忙碌時段，三個時間點）：

| 量測 | 數字 |
|---|---|
| 60 分鐘分析視窗裡出現過的 `object_id` | 79 / 80 / 77 |
| 同一瞬間畫面上真的有偵測框的 `object_id` | 27 / 33 / 1 |
| 視窗內但當下不在畫面上的比例 | 約 60~65% |
| tracklet 存活時間中位數 | 1541 秒 |

⚠ 這份資料只到 2026-08-17 搬機為止，正式機的數字可能更差。

## 根因

不在 MOT，在**兩個範圍對不起來**：

- 關注清單的挑選範圍是「過去一小時出現過的 `object_id`」（`_anomaly_cache`）
- 畫框的比對範圍是「這一幀 WS 送來的 `object_id`」

清單只列 6 個名字，從 80 個裡挑，其中約 62% 在畫面上不存在。平均會有 4 個
名字點下去畫面上一個框都不會亮。這不是偶發，是結構性的。

tracklet 存活中位數 26 分鐘其實不算短，所以「調 tracker 參數」不是施力點。

兩個放大器：

1. `analysis/scheduler.py` 的 `entry["last_seen"] = now` 寫的是分析當下的牆鐘，
   不是這個編號最後一次真的出現的時間。於是死掉的編號要多撐 1~2 輪才逐得掉，
   實際壽命 `window + 2×interval` ≈ 120 分鐘。使用者說的「週期性的短暫救贖」
   就是這一輪逐出。
2. `player.js` 的 `focus` 顯示模式下，沒配對到就完全不畫，一般框又被濾掉，
   於是整片空白。

## 詞彙上的根

`CONTEXT.md` 把關注清單定義成「一份豬隻列表」，但成員是 `object_id`，而
`object_id` 是 tracklet 不是豬。整份設計把兩者當成同一件事。已補上 tracklet /
在畫面上 / 離開畫面三個詞條。

## 定案

清單的合約改成「**現在畫面上**該去看哪幾隻」。使用者拿到名字之後的下一個動作
是走進豬舍找那隻豬，一個指不到任何框的編號對他沒有用處。

- lowest 與 reference 的排名只在畫面上的豬裡面做
- 異常離開畫面就退到「最近消失」（10 分鐘內、最多 5 個、預設收合）
- 但它仍然算「還沒解除的警報」，不會因為編號死了就改列「最低」那三隻
- 採血的權威紀錄是 `health_alerts`（通知中心），不因編號跳號而消失

## 實作（四個 commit）

1. `last_seen` 改記「最後一次真的出現」（`logs[-1]["timestamp"]`）。
   測試的 `_track`／`_thermal_track` 一併改到真實 unix 時間軸上：舊的 0~120
   假時間戳配上真實的 `window_start`，會讓每隻豬在寫進 cache 的同一輪就被逐出。
2. `presence.py`：由 pipeline 每一幀寫入 `camera_id → {object_id: capture_ts}`。
   「在畫面上」給 10 秒容忍。
3. `select_focus` 多收 `on_screen` / `gone_seconds`，回傳 `recent` 與
   `on_screen_count`；`routers/alerts.py` 接線；scheduler 每輪 log 一行
   「快取 N 個編號、畫面上 K 個」。
4. 前端兩段清單、兩種空清單文案、debug HUD 多一行 `focus=N/K gone=M`。

## 否掉的做法

- **調 tracker 參數止血**：tracklet 中位數已經 26 分鐘，不是瓶頸。
  `reid_revive_thresh` 調鬆更會把兩隻長得像的豬合併成同一個編號（見 CLAUDE.md）。
- **在 app 層縫合 tracklet 成「豬隻 session」**：這是根治，但 MOT 團隊改架構時
  很可能會一起處理，現在做一份之後大概率要拆掉。等真的要決定時值得一份 ADR。
- **縮短分析視窗到 20~30 分鐘**：`activity_min_span_seconds=300` 的合格門檻下
  合格豬會變少，`len(rates) >= 2` 撐不住就整台相機掉進 `herd_low`，清單反而
  更常一片空白。
- **前端自己記「最後看到這個編號」**：`bboxHistory` 是 1000 筆上限而且重整頁面
  就歸零，使用者一按 F5「最近消失」就無從算起。
- **讓 `/alerts/focus` 每次查 `tracking_logs` 的 `max(timestamp)`**：那張表一億筆，
  而清單是輪詢的。

## 還沒做的

- 正式機沒有量過命中率。改完之後看 `[cam] 關注快取 N 個編號，畫面上 K 個`
  那行 log，或按 `d` 看 HUD 的 `focus=N/K`。
- 「最近消失」點下去只是切到通知中心，沒有捲到那一筆。
- 10 秒容忍與 10 分鐘保留都是模組常數，不是 DB-backed 設定。要調就改
  `presence.DEFAULT_HOLD_SECONDS` 與 `focus_list.RECENT_GONE_SECONDS`。
