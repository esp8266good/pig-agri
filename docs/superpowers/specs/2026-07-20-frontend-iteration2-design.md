# 前端迭代 2：grid 時段回放、thermal 無訊號、版面修正（2026-07-20）

## 目標與範圍

2026-07-18 前端改版合併後的第一輪使用回饋，五項需求整併為三個工作區塊：

1. **Grid 時段切換**：grid 模式可像單畫面一樣選日期/小時回放（新功能）。
2. **Thermal 無來源顯示無訊號**：單畫面與 grid 皆然（含小幅後端擴充）。
3. **版面修正**：桌機影片一屏內全可見、右欄面板高度一致、tab-bar 重疊修正。

不採用：每格獨立時段（UI 擁擠、實作最重）、grid 共用 transport 拉桿（多格同步
seek 效能風險，本輪不做）、純前端 thermal 逾時偵測（判定慢且與暫時斷線不可分）。

## 1. Grid 時段切換（共用時間軸同步回放）

採 **grid 專用輕量時段選擇器**，不重用/參數化 `timeline.js`（單攝影機狀態與
保留/書籤/多選/刪除管理功能與多攝影機邏輯攪在一起，風險高；管理操作本來就
屬於單畫面場景）。

- **UI**：grid 檢視底部一條時段列——日期按鈕（popover 月曆）＋24 小時格＋
  「LIVE」按鈕；視覺沿用現有時間軸樣式語彙。**不含**保留/書籤/多選/刪除。
- **資料**：小時格「有錄影」狀態取所有攝影機的**聯集**（並行呼叫既有
  `/stream/{cam}/timeline`）；任一台該小時有錄影即可點。
- **回放**：點小時 → 所有 tile 同步切為該小時 VOD（既有 `/stream/{cam}/vod`，
  靜音自動播放、無拉桿）；該攝影機該小時無錄影的格顯示「無訊號」佔位。grid
  頂部顯示琥珀色回放指示（與單畫面 VOD 橫幅同語彙）＋「回到 LIVE」按鈕。
- **回 LIVE**：點「LIVE」或回放指示按鈕 → 全部 tile 回到即時串流。
- **點格返回單畫面帶時段**：grid 回放 09:00 時點某格 → 單畫面直接載入該攝影機
  09:00 VOD（時間軸同步選中該日/該時）；grid 在 LIVE 則維持現行為（回單畫面 live）。
- **模式記憶**：`localStorage` 只記 `viewMode`，不記回放時段；重整後 grid 回 LIVE。
- **錯誤處理**：沿用 tile 既有機制——單格載入失敗顯示錯誤佔位＋重試，不影響
  其他格；`_gridGen` 競態守衛涵蓋時段切換與 RGB/Thermal 切換造成的重建。

## 2. Thermal 無來源 → 無訊號（單畫面＋grid）

**後端小幅擴充**：`hls_manager._last_seen[(cam, type)]` 已為錄影監督追蹤每台
攝影機×串流型別最近收幀時間。`/cameras` 由純 label 清單擴充為附帶每台攝影機
「近期活躍的串流型別」（活躍視窗沿用錄影監督 `_RECORDING_SEEN_WINDOW` 判定）。
回傳形狀需保持前端易消費（例如 `{cameras:[{id, active_types:[...]}, ...]}`），
既有消費端（前端 `init()`／grid）同步更新；補對應後端測試。

- **單畫面**：切 Thermal 而該攝影機無 thermal 來源 → `#video-wrap` 內顯示
  「無訊號」佔位（灰底＋圖示，與 grid 同語彙），不啟動 HLS、不報錯誤 toast；
  切回 RGB 或換攝影機即恢復。VOD 下 thermal 該時段無錄影（404）同樣顯示無訊號。
- **Grid**：tile 跟隨 header 的 RGB/Thermal 切換（live 與回放皆然）；無 thermal
  來源／該時段無 thermal 錄影的格顯示無訊號佔位、不建播放器。grid 模式下切換
  RGB/Thermal 重建所有 tile（`_gridGen` 守衛）。
- **語意區分**：「無訊號」＝預期狀態（無來源/無錄影，**不給**重試鈕）；
  「連線錯誤」＝異常（給重試鈕）。兩種佔位視覺可區分。

## 3. 版面修正

- **3a. 桌機影片一屏內全可見**：`≥1200px` 時 `#video-wrap` 高度上限依視窗高度
  計算（`max-height: calc(100dvh − header − transport − 時間軸區 − 間距)`），
  維持 4:3 與 `object-fit: contain`，影片縮小時左右黑邊置中。驗收：1080p 螢幕
  「影片＋transport＋整條時間軸」同屏可見不捲動。手機版不變。
- **3b. 右欄面板高度一致**：桌機右欄由 `max-height`＋內容撐高改為**固定高度**
  （`height: calc(100dvh − offset)`），豬隻狀態/通知/書籤三分頁共用同一外框
  高度、內容區各自捲動；內容不足時面板高度不變。
- **3c. tab-bar 重疊修正**：先以 360–1199px 多寬度實測重現，依根因修正——候選：
  sticky tab-bar 背景非完全不透明（捲動內容透出）、`--header-h` 量測時機/斷點
  邊界 top 錯位、與 `#storage-action-bar`/popover 的 z-index 疊層衝突。若根因
  是 header 折行本身，一併將窄寬度下 header 控制項收斂（圖示化/收合）使高度
  恆定，治本不再補償。驗收：多寬度截圖證明任何寬度下 tab-bar 不與 header/內容
  重疊。

## 檔案影響

- `static/js/grid.js`：時段選擇器、VOD tile、RGB/Thermal 跟隨、無訊號佔位。
  若檔案成長過大可拆 `grid-timeline.js`（一檔一責）。
- `static/js/player.js`：單畫面 thermal 無訊號佔位、點格帶時段的 VOD 載入入口。
- `static/js/main.js`：`/cameras` 新回傳形狀消費、grid↔single 帶時段切換串接。
- `static/css/app.css`：3a/3b/3c、無訊號佔位、grid 時段列樣式。
- `main.py`：`/cameras` 擴充；`hls_manager.py` 若需曝露 active types 的查詢介面
  則加最小方法。
- `tests/`：`/cameras` 新形狀的後端測試（沿用 `_dummy_zmq_sources` pattern）。

## 驗證

- 後端：全套 `uv run pytest -p no:cacheprovider` 0 失敗（含 `/cameras` 新測試）。
- 前端實機（headless Chrome 對真實部署，對 DB 僅讀取性互動）：
  - grid 選日/選時同步回放、無錄影格無訊號、回 LIVE、點格帶時段返回單畫面、
    重整後 grid 回 LIVE；
  - 單畫面與 grid 的 thermal 無來源 → 無訊號佔位（以 fetch monkeypatch 模擬
    無 thermal 攝影機，不動真實環境）；
  - 3a/3b 於 1920×1080 驗證同屏可見與三分頁等高；3c 多寬度掃描截圖；
  - console 全程無 error/exception。
