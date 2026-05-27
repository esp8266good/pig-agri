# 前端體驗：最後瀏覽攝影機 cache + timeline 月曆日期選擇器 設計

> 狀態：設計已確認，待寫實作計畫。子系統 C（三子系統 A→B→C 的第三塊，獨立於 A/B）。純前端（`static/index.html`），後端與測試套件零改動。

## 1. 動機

兩個前端體驗問題：

1. **每次開頁都從第一個攝影機開始**：操作者常固定看某一隻鏡頭，應記住最後瀏覽的攝影機。
2. **timeline 太擠難點**：現為 168 格（7 天 × 24 小時）週視圖，`timeline-slot` 太窄、手機尤難點擊。但「綜觀哪些日期有錄影」的視覺值得保留。改成：月曆網格（標出有錄影的日）→ 選一天 → 下方顯示該日 24 格（變寬易點）。

## 2. 範圍與決策（brainstorm 已確認）

- **僅改 `static/index.html`**。沿用既有 `GET /stream/{camera_id}/timeline?start_ts&end_ts` 端點查不同範圍，後端零改動。
- **攝影機 cache**：localStorage 只記攝影機（ZMQ topic）；不在清單則退回第一個。RGB/Thermal 不記（預設 RGB）。
- **日期選擇器 = 月曆網格**：有錄影的日亮點；點某日 → 下方 24 小時格。
- **預設**：月曆開在當月，預設選取「今天」（本地午夜）。今天無錄影也顯示空 24 格。
- **與子系統 B 完全相容**：只改「小時的分組顯示」（週 168 格 → 月曆 + 當日 24 格），不改 `hour_ts` 值（仍為 3600 的倍數），B 既存 `saved_segments` 資料無需 migration。

## 3. 功能一：最後瀏覽攝影機 cache

- `camSelect` 的 change handler 內：`localStorage.setItem('lastCamera', currentCamera)`（在設定 `currentCamera = camSelect.value` 之後）。
- `init()` 取得 `cameras` 後，將現有的 `currentCamera = cameras[0]` 改為：
  ```javascript
  const cached = localStorage.getItem('lastCamera');
  currentCamera = (cached && cameras.includes(cached)) ? cached : cameras[0];
  camSelect.value = currentCamera;
  ```
- localStorage 不可用（隱私模式）→ try/catch 包覆，失敗即退回 `cameras[0]`，不影響其餘功能。

## 4. 功能二：月曆 + 當日小時條

### 4.1 移除（現有週視圖）
- `#week-nav` 內的週導覽：`prevWeek`/`nextWeek` 按鈕與 `#week-label`。
- JS：`prevWeek()`、`nextWeek()`、`getWeekStart()`、`formatWeekLabel()`、`updateWeekNavButtons()`、`currentWeekStart` 狀態、168 格版本的 `renderTimeline`。
- **保留**：`#live-btn`（VOD 時顯示）、子系統 B 的 `#select-mode-toggle`、`#storage-action-bar`、`#timeline-bar`（改渲染 24 格）。

### 4.2 新增月曆 DOM（`#calendar`，置於 `#timeline-bar` 之上）
- 表頭：`◀`（onclick prevMonth）、月份標籤 `#calendar-label`（如「2026 年 5 月」）、`▶`（nextMonth）。
- 星期標頭列（日 一 二 三 四 五 六）。
- 日格容器 `#calendar-grid`：每月重繪，含月初空白佔位 + 各日格 `.cal-day`。

### 4.3 狀態
| 變數 | 用途 |
|---|---|
| `currentMonth` | 顯示中月份的第一天 `Date`（本地） |
| `selectedDay` | 選取日的本地午夜 unix 秒 |
| `monthHoursSet` | `Set<number>`：當月有資料的 `hour_ts`（3600 倍數） |

### 4.4 資料流（沿用 `/stream/{cam}/timeline`）
- `loadCalendar()`：以 `currentMonth` 算本地月首/月末 unix（`new Date(y, m, 1)` / `new Date(y, m+1, 1)`），`GET /stream/{currentCamera}/timeline?start_ts={monthStart}&end_ts={monthEnd}` → `hours` 陣列填 `monthHoursSet` → `renderCalendar()`。
- `renderCalendar()`：畫當月格。某日 `dayTs`（本地午夜 unix）有資料 = `monthHoursSet` 內存在落在 `[dayTs, dayTs+86400)` 的 `hour_ts`（迴圈該日 24 個候選 `dayTs+h*3600` 是否在 set）。有資料 → `.has-rec`；`dayTs === selectedDay` → `.day-selected`。點日格 → `selectDay(dayTs)`。
- `selectDay(dayTs)`：`selectedDay = dayTs`；`clearSelection()`（換日清除 B 的小時選取）；`await loadDaySegments()`；`renderDayBar()`；更新月曆 `.day-selected` 標示。
- `loadDaySegments()`：`GET /storage/segments?camera_id={currentCamera}&start_ts={selectedDay}&end_ts={selectedDay+86400}` → 填 `savedSegmentsMap`（沿用 B 的 by-`hour_ts` map；取代 B 原本抓「週範圍」的 `loadSavedSegments`）。
- `renderDayBar()`：在 `#timeline-bar` 畫 24 格（`h` = 0..23，`slotTs = selectedDay + h*3600`）。每格沿用 B 邏輯：
  - `monthHoursSet.has(slotTs)` → `.has-data`（可播放/可選取）。
  - `savedSegmentsMap.get(slotTs)` → `.bookmarked`（有 label）或 `.protected`（無 label）。
  - `selectedHours.has(slotTs)` → `.slot-selected`。
  - 點擊：`selectMode` 開 → 切 `selectedHours` + `.slot-selected` + `updateActionBar()`；關 → 既有播放（`.selected` + `loadVod(slotTs)`）。
- `loadTimeline()`（**保留此名**，供 B 既有 refresh 呼叫點與 `init`/`camSelect` 使用）：`await loadCalendar(); await selectDay(selectedDay);`。

### 4.5 月份導覽
- `prevMonth()` / `nextMonth()`：`currentMonth` ±1 月，`clearSelection()`，`loadCalendar()`（不自動換 selectedDay；selectedDay 維持，使用者點新月的日才換）。
- 月份範圍邊界：可往前到約 retention 天數前、往後不超過當月（沿用現有「未來不可選」精神；以「今天」為上界）。`▶` 在 `currentMonth` 已是當月時 disabled。

## 5. 與子系統 B 的整合（不破壞已交付功能）

- `renderTimeline`（168 週格）由 `renderDayBar`（24 日格）取代，但每格的 B 行為（markers / `selectedHours` / select-mode 點擊 vs `loadVod`）逐一保留。
- `loadVod(hour_ts)`、`savedSegmentsMap`（keyed by `hour_ts`）、`selectedHours`、`clearSelection`、`updateActionBar`、`onRetainClick`/`onBookmarkClick`/`onDeleteRecClick`/`confirmDeleteRecordings`/書籤面板 全部不改邏輯——它們只依賴 `hour_ts`（3600 倍數），與 24 格 day bar 一致。
- B 的 `loadSavedSegments`（抓週範圍）改名/改為 `loadDaySegments`（抓選取日範圍）；所有呼叫點（`loadTimeline`）一併更新。
- B 的 `clearSelection` 重置點：原 `prevWeek`/`nextWeek` 改為 `prevMonth`/`nextMonth`；新增 `selectDay`（換日清選取）；`camSelect`（已有，不變）。
- **`hour_ts` 相容性**：B 既存 `saved_segments.hour_ts` 是舊週視圖存的 3600 倍數；C 的 24 格 `slotTs = selectedDay + h*3600`（selectedDay 為本地午夜、UTC+8 偏移為整小時 → 本地午夜 unix 亦為 3600 倍數）也是 3600 倍數，且本地時鐘小時邊界一致。兩者 `hour_ts` 值域相同，**無需 migration**。

## 6. 預設 / 邊界 / 錯誤處理

- 開頁：`currentMonth` = 含今天的月、`selectedDay` = 今天本地午夜（`new Date()` setHours(0,0,0,0)）。
- 今天無錄影 → 24 格皆非 has-data（仍顯示、可切到 live 看當下）；月曆今天格無 `.has-rec`。
- 切攝影機（`camSelect`）：重新 `loadTimeline()`（重載當前月的 availability + 當前 selectedDay）；`clearSelection()`（已有）；cache 攝影機。
- 切 RGB↔Thermal / Live↔VOD：沿用既有重置；timeline 結構不受 type 影響（timeline endpoint 只看 rgb 目錄，與現狀一致）。
- localStorage 不可用：try/catch，cache 失效不影響功能。
- timeline fetch 失敗：沿用既有 `catch` 靜默；月曆顯示無資料、24 格無 has-data。

## 7. 測試策略

- `static/index.html` 無 JS 測試框架（與既有前端改動一致）。
- `node --check`（抽出主 inline `<script>` 區塊；注意：inline `<script>` 起始行會隨 HTML 增長位移，抽取時用 `grep -n "^  <script>"` 找起始行，勿寫死行號）確認 JS 語法。
- 後端零改動 → Python 測試套件維持綠燈（`uv run pytest`，4 既有 ZMQ_SOURCES 失敗無關）。
- **瀏覽器驗收清單**（使用者執行）：
  1. cache：選某攝影機 → 重整頁面 → 仍是該攝影機；cache 的攝影機不在清單 → 退回第一個。
  2. 月曆：有錄影的日亮點；點月 ◀▶ 切月、availability 正確；未來月 ▶ 在當月時 disabled。
  3. 選日 → 下方 24 格出現、變寬易點；has-data 格可播放（VOD）。
  4. B 相容：24 格上保留/書籤 🔒/★ 標記正確；選取模式多選 + 操作列（保留/書籤/刪除）正常；換日/切月/切攝影機清除選取。
  5. 書籤面板點連結 → 跳該 hour_ts VOD（月曆/選取日需同步到該日，或至少 VOD 正常載入）。
  6. 預設今天、今天無資料也不報錯；手機畫面格子夠大可點。

## 8. 非目標（YAGNI）

- 月曆日格顯示保留/書籤 day 級標記（🔒/★ 維持在小時格 + 書籤面板）。
- 一次載入跨多月 availability（每次只查顯示中的月）。
- 日期/選取日偏好持久化（只 cache 攝影機）。
- DST 時區處理（部署固定 UTC+8 整小時偏移）。
- 後端新端點或 timeline 端點改動（沿用現有 start_ts/end_ts 查詢）。
- 點書籤連結時自動把月曆翻到該日（VOD 直接載入即可；月曆同步為加分，非必要）。
