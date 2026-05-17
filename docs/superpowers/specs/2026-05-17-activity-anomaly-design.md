# 活動量異常檢測重做 + 體溫偵測開關 設計文件

> 日期：2026-05-17 ｜ 狀態：已與使用者確認，待轉實作計畫

## 背景與問題

核心任務：依活動量判斷豬隻是否需採血——活動量過低 → 標記並由現場人員採血。
計算在 `analysis/scheduler.py`，按 `object_id` 串接 bbox 中心點位移。

健診發現現有活動量告警**實質失效**，四個 bug：

1. **取樣錯誤（致命）**：`current_a = displacements[-1]` 是最後兩幀之間單一一步位移，
   卻拿去跟整視窗位移的 `mean − 3×std` 比。逐幀位移非負右偏，`mean − 3×std`
   幾乎恆為負 → 條件幾乎永不成立 → 告警從不觸發（使用者從沒看過）。
2. **循環基準**：current 與其所屬同一 30 分鐘視窗的 mean/std 比，無歷史/同伴基準。
3. **視窗與目標不符**：`analysis_window_minutes=30`，目標是 1~6 小時低活動量。
4. **狀態閂鎖 + 樣本門檻**：`_rebuild_cache` 把歷史 alert 灌成 `activity_anomaly=True`
   後永不再寫；`min_samples=50` 配 MOT ID 跳號常直接 `continue` 連算都沒算。

附帶需求：開發階段 Thermal 鏡頭常關閉，造成體溫異常誤判，需前端開關暫時關閉體溫偵測。

## 已確認的需求決策

- 活動量基準：**同伴相對**（同時段同欄其他豬的分布），對 ID 跳號/光照最穩健。
- 評估視窗：**可設定，預設 1 小時**，前端下拉限定 {15, 30, 60, 120, 180, 240, 300, 360} 分鐘。
- 判定規則：**低於同伴中位數的比例 + 絕對下限**（夜間全欄休息時不亂標）。
- 告警生命週期：**一隻一次，恢復後才能再告**（不轟炸現場人員、不漏真事件）。
- 演算法：**方案 A — 時間正規化的路徑速率 + 同伴中位數比例**。

## 架構總覽

僅改 `analysis/scheduler.py`（演算法 + 狀態機）、`config.py`（設定欄位）、
`routers/settings.py`（體溫開關 + 設定鍵）、`static/index.html`（設定面板開關）。
資料來源 `tracking_logs`、告警寫入 `health_alerts`、cache 給 `/alerts/active` 與前端
`anomalyMap` 的介面**不變**（前端畫紅框邏輯沿用 `activity_anomaly`/`temp_anomaly` 旗標）。

## §1 活動量指標與異常判定

### 設定（config.py，可由 /settings 線上調整）

| 設定 | 預設 | 說明 |
|---|---|---|
| `analysis_window_minutes` | `60` | 評估視窗；前端下拉限定 {15,30,60,120,180,240,300,360} |
| `analysis_interval_minutes` | `30` | 多久跑一次分析（沿用既有） |
| `activity_low_ratio` | `0.3` | 速率 < 同欄中位數 × 此值 → 候選異常 |
| `activity_recover_ratio` | `0.5` | 速率 > 中位數 × 此值 → 解除（遲滯，須 > low_ratio） |
| `activity_abs_floor` | `2.0` | 同欄中位數速率（px/s）低於此 → 整欄視為休息，不標記 |
| `activity_min_coverage` | `0.5` | 該豬「有軌跡時間跨度 ÷ 視窗長度」需 ≥ 此值才評估 |
| `temp_anomaly_enabled` | `true` | 體溫異常偵測總開關（§3） |

`anomaly_std_threshold`、`anomaly_min_samples` 對活動量不再使用；體溫仍用
`anomaly_std_threshold`，`anomaly_min_samples` 保留給體溫的 `len(temps)` 門檻。

### 每隻豬活動速率（每個 (camera_id, object_id)）

- 取視窗內依 timestamp 排序的 bbox 中心點 `(bb_left+bb_width/2, bb_top+bb_height/2)`。
- `path = Σ 相鄰中心點歐氏距離`；`span = 最後 timestamp − 第一 timestamp`。
- 涵蓋率 `coverage = span ÷ (analysis_window_minutes×60)`；`coverage < activity_min_coverage`
  或 `span < 60` 秒或 `len(centers) < 2` → 跳過此豬（資料太少不評）。
- `activity_rate = path ÷ span`（px/s）。

### 同伴基準與判定（每個 camera 獨立）

- `median_rate = median(該欄所有通過涵蓋率的豬的 activity_rate)`。
- 若通過豬數 < 2 或 `median_rate < activity_abs_floor` → 此 camera 本輪不標記任何豬
  （夜間/資料不足保護），但仍更新 cache 數值欄位。
- 每隻豬：`low = activity_rate < median_rate × activity_low_ratio`。

## §2 告警狀態機與既有 bug 修正

### 狀態（存 `_anomaly_cache[camera_id][object_id]`，擴充現有 dict）

新增 `activity_state ∈ {"normal","alerted"}`，取代會被 `_rebuild_cache` 閂死的
`activity_anomaly` bool 寫法。每輪對每隻通過涵蓋率的豬：

```
若 activity_state == "normal":
    若 low:  write_health_alert(metric="activity") ; activity_state = "alerted"
若 activity_state == "alerted":
    若 activity_rate > median_rate × activity_recover_ratio:
        activity_state = "normal"      # 恢復，不寫 DB，僅清狀態
```

`activity_anomaly`（前端紅框旗標）= `activity_state == "alerted"`，續放 cache，
`/alerts/active`、前端 `anomalyMap` 介面不變。同理體溫加 `temp_state`。

### _rebuild_cache 修正

不再從歷史 alert 推斷 state。重啟一律 `activity_state="normal"`、`temp_state="normal"`，
`activity_anomaly=False`、`temp_anomaly=False`，由下一輪分析重新判定。理由：重啟後豬
可能已恢復或 ID 已變，沿用舊 alert 反而錯；漏掉的真事件 DB 歷史仍可查。
（保留 `_rebuild_cache` 函式以建立 cache 骨架，但不再灌 `True`。）

### 去重

狀態機天然保證單一 episode 一筆；移除 `if not entry["activity_anomaly"]` 脆弱判斷。

### ID 跳號殘留風險

已用「同伴相對 + 速率正規化 + 涵蓋率門檻」吸收（一隻被切兩段 → 兩段速率仍接近、
仍跟同伴比）。軌跡縫合列為 CLAUDE.md 未來安全網，本 spec 不做（YAGNI）。

## §3 體溫異常偵測開關

### 後端

- `config.py` 加 `temp_anomaly_enabled: bool = True`。
- `routers/settings.py`：`ALLOWED_KEYS` 加 `"temp_anomaly_enabled"`；GET 無 DB
  fallback dict 加此鍵（值為 `str(app_settings.temp_anomaly_enabled).lower()` →
  `"true"`/`"false"`）；PUT 後若 `temp_anomaly_enabled` 在 updates，連同既有
  `analysis_interval_minutes`/`anomaly_std_threshold` 一併呼叫 `scheduler.reload(...)`。
- `Scheduler.reload()` 簽名加 `temp_anomaly_enabled: bool`，存 `self._temp_enabled`；
  `__init__` 由 `settings.temp_anomaly_enabled` 初始化。
- `_run_analysis` 體溫整段包在 `if self._temp_enabled:`。**關閉時**：跳過體溫計算，
  並把 cache 每個 entry 的 `temp_anomaly=False`、`temp_state="normal"`，讓前端殘留
  體溫標記立即消失。
- 持久化沿用 `upsert_settings`（DB `settings` 表字串存）；字串 ↔ bool 轉換規則：
  寫入存 `"true"`/`"false"`；讀取 `value.strip().lower() == "true"` 為 True，其餘 False。

### 前端（static/index.html 設定面板 .settings-form）

- 加一個 checkbox：label「體溫異常偵測」，預設勾選。
- `GET /settings` 載入時依回傳字串設 `checkbox.checked`；`saveSettings()` 送 PUT 時
  以 `checkbox.checked ? "true" : "false"` 加入 body。樣式沿用 `.settings-field`。
- 評估視窗（`analysis_window_minutes`）前端改為 `<select>` 限定上述 8 個選項。

## §4 錯誤處理與測試

### 錯誤處理

- `median` 前過濾空清單；通過豬數 < 2 或 `median_rate` 為 0 / < floor → 該 camera
  本輪整批不標記，不丟例外。
- `span`/分母為 0 防護（跳過該豬）。
- `_run_analysis` 既有 try/except 維持；per-row 防護使單一 camera/豬出錯不影響其他。

### 測試（pytest，沿用 tests/test_analysis_scheduler.py mock pool 模式）

- 速率計算：已知軌跡 → 預期 px/s。
- 涵蓋率門檻：樣本足但時間跨度不足 → 跳過。
- 同伴判定：一隻明顯低於中位數 → 標記；夜間全欄低（median < floor）→ 全不標記；
  通過豬數 < 2 → 不標記。
- 狀態機：low→寫一筆；持續 low→不重複寫；恢復（> recover_ratio×median）→ 清狀態；
  恢復後再 low→寫新一筆。
- `_rebuild_cache`：重啟後 state 一律 normal，不被歷史 alert 閂死。
- 體溫開關：`temp_anomaly_enabled=false` → 體溫區塊跳過且 cache `temp_anomaly`
  被清 False、`temp_state="normal"`；settings router PUT 該鍵觸發 `reload`。
- settings router：`temp_anomaly_enabled` 在 ALLOWED_KEYS；bool↔字串轉換正確；
  GET 無 DB fallback 含該鍵。
- 既有測試全綠（既有 ZMQ_SOURCES env gap collection error 非本次回歸，不在範圍）。

## 不做（YAGNI）

- 軌跡縫合安全網（CLAUDE.md 未來選項）。
- 自身歷史基準、絕對門檻、dwell/idle 佔比演算法（方案 B/C，已否決）。
- config.py OS env 源技術債（待辦 #12，與本 spec 無關）。
