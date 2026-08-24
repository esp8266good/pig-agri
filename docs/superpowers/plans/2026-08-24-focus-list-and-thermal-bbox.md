# 關注清單不消退、熱像 bbox 位移，以及一起處理掉的五件小事

**日期**：2026-08-24
**狀態**：程式碼完成，測試全綠（539 passed）。**尚未部署到正式機**，DB 要先建一張表。
**前一份**：`2026-08-24-thermal-y16-rollout.md`（熱像改送 Y16 溫度場，已驗證通過）

---

## 0. 接手的第一件事

```
# 1. 正式機的 DB 建新表（additive，整份 init.sql 都是 IF NOT EXISTS / ON CONFLICT，
#    重跑不會動到既有資料）
ssh pig-agri 'cd ~/lobby/pig-agri && docker exec -i pig-agri-postgres-1 \
  psql -U pig -d pig_monitoring' < sql/init.sql

# 2. 部署 + 重啟
ssh pig-agri 'cd ~/lobby/pig-agri && git pull && systemctl --user restart pig-agri-tmux.service'

# 3. 熱像對位要用眼睛校一次（見下面第 3 節），不校也能跑，只是框會偏一點
```

沒建表的後果：`/thermal-align/{cam}` 的 GET 會退回 identity（畫面照樣正常），
PUT 會 500。其餘六項功能完全不碰 DB。

---

## 1. 關注清單上的 id 永遠不消退

### 根因

`analysis/scheduler.py` 的 `_anomaly_cache` 只會長不會縮。MOT 的 ID 會跳號：
同一隻豬被遮擋一次再出來就換一個新號碼，舊號碼從此不會再出現在任何一筆
`tracking_logs` 裡——但它在 cache 裡的那筆 entry 還在，`activity_state` 停在
`alerted`，於是關注清單一直指著一隻畫面上根本不存在的豬，愈積愈長，把真正
該看的擠到看不見的地方。

`_rebuild_cache` 還會在每次重啟時把**歷史上曾經告警過的所有** object_id 重新
建成骨架，等於把這個問題固化下來。

### 排除掉的做法：定時清空

使用者原本的想法是「比照分析間隔或活動量評估窗口定時 reset」。不這樣做，理由是
**定時清空會把遲滯狀態機一起重置**（`activity_state` 回到 `normal`）。真正持續
低活動的豬每一輪都會被當成一個新的異常重新 `write_health_alert`、重新推播，
通知中心會被同一隻豬洗版。那個狀態機（`activity_recover_ratio=0.5`）存在的目的
就是防止這件事。

### 採用的做法：逐出已經不存在的 object_id

判準是「在活動量評估窗口內沒有任何 tracking_log」。這剛好就是 ID 跳號的定義，
而且是自洽的：分析本來就只看得到視窗內出現過的 object_id，視窗外那些不管留多久
都不可能再被更新一次。所以清掉它們不會丟失任何還在更新的判斷，而**還出現在視窗
裡的豬，狀態原封不動**。

| 檔案 | 改動 |
|---|---|
| `analysis/scheduler.py` | entry 多一個 `last_seen`；`_run_analysis` 每輪標記；結尾呼叫 `_prune_stale(window_start)` |
| 同上 | 新增 module-level `_camera_state`（見下） |
| `focus_list.py` | `select_focus` 多收一個 `camera_state`（可選，不傳走舊路徑） |
| `routers/alerts.py` | 把 `get_camera_state().get(camera_id)` 傳進去 |

### 連帶要處理的：entry 被清光之後，狀態訊息會說錯話

以前 `herd_ok` / `analyzed` 這兩個 per-camera 的判斷是寫在 per-object 的 entry
裡的（程式碼自己有註解說這是將就的做法）。逐出開始生效之後，夜間一台相機會一筆
entry 都不剩，這時候光看 entries 分不出「分析過、但全欄都在休息」與「還沒分析
過」，兩種都會落到「目前沒有需要注意的豬」——**把一個保護講成一份保證**。

所以 scheduler 現在每輪額外寫一份 `_camera_state[camera_id] = {analyzed, herd_ok}`，
包含**這一輪完全沒有偵測資料的相機**（夜間全黑、相機斷線也要記，否則會停在幾小時
前那份「一切正常」）。

### 驗證

`tests/test_analysis_scheduler.py` 新增 4 個：逐出消失的 id、不動還在的豬的狀態、
無資料輪次的 camera_state、以及原本那個「temp 停用要清舊紅框」的測試改成斷言
「整筆被逐出」（逐出比清旗更強，要驗的事情照樣成立）。
`tests/test_focus_list.py` 新增 4 個 camera_state 相關。

---

## 2. 熱像的 bbox 全部位移

### 根因（不在熱像，在前端的分母）

`static/js/player.js` 的 `drawBoxes()` 拿 `<video>` 的 `videoWidth/videoHeight`
當 bbox 的分母。bbox 是在 **rgb 原始畫面 1280x720** 上算出來的座標。

- rgb 那條串流剛好就是 1280x720 → 一直看起來是對的，這個 bug 藏了很久。
- 熱像以前被拉伸成 1280x720 → 也剛好對。
- 熱像 08-24 改成原生 640x480 之後 → 同一組座標被除以 640 再乘回畫面寬度，
  等於整片框放大一倍往右下推。

### 修法

後端把 bbox 的座標系尺寸講出來，前端一律換算成 0..1 的比例再畫：

| 檔案 | 改動 |
|---|---|
| `inference/pipeline.py` | WS payload 加 `frame_width`/`frame_height`；`update_frame` 記錄每台相機的實際尺寸；新增 `frame_sizes()` |
| `main.py` | `/cameras` 回傳 `frame_sizes` |
| `static/js/player.js` | 新增 `sourceFrameSize()` / `boxToNormalized()`；畫框改用 `renderW/renderH × 正規化座標` |
| `static/js/{main,grid}.js` | 從 `/cameras` 接 `frame_sizes` |

拿不到尺寸時退回 `videoWidth/videoHeight`，也就是舊行為，不會整片框消失。

---

## 3. 熱像與 RGB 的對位校正（手動）

### 為什麼不做自動校正

RGB 與熱像的成像原理不同（反射光 vs 輻射），灰階之間沒有穩定的對應關係，
一般的特徵點比對／互相關在這裡數學上不成立。理論上可用互資訊配準，但本場地
把它擋死了：熱像只有 160x120、豬體在上面是一團沒有內部紋理的均勻亮區，
而夜間 RGB 全黑根本沒有影像可配。鏡頭是鎖死的，校正一次可以用很久，
**不值得養一條會安靜給出錯誤結果的自動流程**。

### 做法

四個數字，先縮放再平移，x/y 各自獨立，座標正規化 0..1（跟遮罩同一套慣例）：

```
tx = off_x + nx * scale_x
ty = off_y + ny * scale_y
```

沒有校正過的相機是 identity，行為與這個功能出現之前完全相同。

**⚠ 這組參數同時餵給兩個地方**：前端在熱像畫面上畫框，以及後端
`_compute_thermal_celsius` 取那隻豬的體溫。所以校正不只是把畫面調好看，
它會改變寫進 DB 的體溫數值。兩邊一定要用同一組，否則畫面上框對得很準、
實際取樣的卻是隔壁那塊。

| 檔案 | 角色 |
|---|---|
| `thermal_align.py` | 純函式：normalize / validate / map_box |
| `sql/init.sql` | `camera_thermal_align` 表（一台相機一列，沒有列＝identity） |
| `db_writer.py` | `query_thermal_aligns` / `upsert_thermal_align` |
| `routers/thermal.py` | GET/PUT `/thermal-align/{camera_id}`，存檔同時 push 進 pipeline |
| `main.py` | 啟動時 `_load_thermal_aligns_into_pipeline()` |
| `static/js/align.js` | 校正 UI：畫面上拖曳平移、按鈕微調縮放 |

### 操作方式

切到熱像的 LIVE 畫面 → 右上「校正對位」→ 直接在畫面上拖曳把框推到豬身上，
或用面板上的方向／±鈕微調 → 儲存並套用。

⚠ 熱像是 4:3、rgb 是 16:9，identity 等於「整張拉去對整張」。真正的視野關係
要靠 scale 修，所以第一次校正時 scale 大概不會停在 1.0。

### 已知限制

- 只有平移＋縮放，沒有旋轉，也沒有處理視差（豬離鏡頭遠近不同時偏移量會變）。
  兩顆鏡頭是並排固定的，深度差在豬舍這個距離下不大，先不處理。
- 校正參數改變之後，**已經寫進 DB 的舊體溫不會回溯修正**。

---

## 4. 其餘四件小事

| # | 問題 | 改法 |
|---|---|---|
| 1 | 關注清單太長把其他資訊擠下去 | `#focus-list` 加 `max-height: 11.5em; overflow-y: auto`（約 5 列）。用 em 不用 px：字級是變數 |
| 2 | 通知中心對「活動量偏低」顯示「偏差 —σ」 | 拆成 `_alertDetail()`。活動量改顯示「2.1 px/s，只有同欄中位數的 25%」；體溫維持 σ |
| 3 | 想一眼看出框對應哪隻豬 | 新增「每個框都標編號」開關（`S.showAllIds`，存 localStorage）。淡框（ghost）不標，那個模式的用意就是讓它們退到背景 |
| 4 | 說明模式說「熱像沒有豬隻方框」 | 改成講事實：框一樣會畫，體溫就是從框裡那塊取的。同時補上兩個新控制項的說明 |

### 順帶確認：「異常閾值」的作用範圍

使用者問得對，`anomaly_std_threshold` **只作用在體溫**：

- 體溫：`abs(current - mean) > threshold * std`（跟這隻豬自己的近期平均比）
- 活動量：`rate < median * activity_low_ratio`（跟同一欄豬的中位數比，跟 σ 無關）

「偏差 —σ」的來源是 `write_health_alert` 對活動量一律寫 `std_value=0.0`，
前端卻兩種都硬套同一個公式。設定頁的標籤已改成「體溫異常閾值」並加一行說明。

---

## 5. 這次沒做的

- **自動對位校正**：理由在第 3 節，不要重新推導。
- **舊體溫回溯修正**：校正參數只影響之後寫入的資料。
- **旋轉／視差校正**：兩顆鏡頭並排固定，先不處理。
- **`thermal_preview_min_c/max_c` 做成 DB-backed**：前一份文件第 5 節列的，還沒做。
- **容量問題**：前一份文件第 5 節，`HLS_RETENTION_DAYS=90` × 每天約 12.5GB 仍然
  大於磁碟容量，數學上不可能滿足。沒動。
- **`cam_02`/`cam_03`/`rpi_sensors` 三台停在 08-20 的 41G**：還沒問過能不能清。
