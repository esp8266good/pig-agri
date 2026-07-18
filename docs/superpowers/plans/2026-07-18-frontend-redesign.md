# 前端整體改版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 依 `docs/superpowers/specs/2026-07-18-frontend-redesign-design.md`，把 `static/index.html`（2627 行單檔）重構為拆檔的響應式介面：桌機雙欄、grid 監看模式、設定 drawer、時間軸互動重做、深色視覺精緻化。

**Architecture:** 漸進式重構——先把 CSS/JS 原樣抽出成獨立檔（行為零變更的 checkpoint），再模組化為 ES modules（共享狀態集中在 `state.js`），之後逐區重做版面與互動。HLS/PDT 同步、bbox overlay、transport 拉桿的邏輯**只搬不改**。

**Tech Stack:** Vanilla JS（ES modules）、CSS custom properties、hls.js（CDN）、零 build step。後端 FastAPI 完全不動。

## Global Constraints

- 分支：`feat/frontend-redesign`（從 master 開）。
- 零 build step；`main.py` 的 static 掛載與所有後端 API 不動。
- 響應式斷點：**1200px**（雙欄⇄單欄）；觸控目標 ≥ 44px（< 1200px 時）。
- 既有 JS 函式「原樣搬移」：只允許改（a）共享變數加 `S.` 前綴（b）DOM 選擇器（c）加 `export`/`import`。邏輯、順序、數值一律不動。
- 每個 js 檔改完必跑 `node --check static/js/<file>.js`。
- 不新增任何外部依賴（字體 Google Fonts 除外，現況已用）。
- 後端 API 契約（本計畫會用到的）：
  - `GET /cameras` → `{cameras: string[]}`
  - `GET /stream/{cam}/live?type=rgb|thermal` → `{url}`
  - `GET /alerts/active?camera_id=X` → `{cache: {cam: {oid: {activity_anomaly, temp_anomaly}}}}`
  - `GET /alerts?camera_id=X&limit=50&unread_only=true` → `{alerts: [...]}`
  - `GET/POST /settings`（loadSettings/saveSettings 現有格式）
- 瀏覽器驗證：啟動 `uv run uvicorn main:app --port 5005`，用 claude-in-chrome（若可用）或請使用者以瀏覽器逐項檢查該 task 的清單。無攝影機資料時至少確認頁面載入無 console error、版面正確。

---

### Task 1: CSS/JS 原樣抽出（行為零變更 checkpoint）

**Files:**
- Create: `static/css/app.css`
- Create: `static/js/app.js`
- Modify: `static/index.html`

**Interfaces:**
- Produces: `static/css/app.css`（原 `<style>` 內容全量）、`static/js/app.js`（原 inline `<script>` 內容全量，仍為傳統 script、非 module，全域函式與 inline onclick 照舊可用）。

- [ ] **Step 1: 開分支**

```bash
git checkout -b feat/frontend-redesign
```

- [ ] **Step 2: 抽出 CSS**

把 `static/index.html` 第 11–756 行（`<style>` 與 `</style>` 之間的全部內容）剪下，存成 `static/css/app.css`（不含 style 標籤本身）。原位置改為：

```html
  <link rel="stylesheet" href="/static/css/app.css">
```

- [ ] **Step 3: 抽出 JS**

把 `<script>`（原 1009 行）與 `</script>`（原 2585 行）之間的全部內容剪下，存成 `static/js/app.js`。原位置改為：

```html
  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script src="/static/js/app.js"></script>
```

注意：`</body>` 後面的書籤編輯 modal 與刪除確認 modal HTML（原 2586 行之後）維持原位不動。

- [ ] **Step 4: 語法驗證**

```bash
node --check static/js/app.js
grep -c "<style>\|<script>" static/index.html   # 應只剩 0 個 <style>
```

Expected: node --check 無輸出（成功）；index.html 內不再有 inline style/script 區塊。

- [ ] **Step 5: 啟動 app 驗證行為不變**

```bash
uv run uvicorn main:app --port 5005
```

瀏覽器開 `http://localhost:5005/`，確認：頁面外觀與改版前完全相同、live 播放正常、分頁可切換、console 無錯誤。

- [ ] **Step 6: Commit**

```bash
git add static/ && git commit -m "refactor(frontend): 抽出 css/app.css 與 js/app.js（行為零變更）"
```

---

### Task 2: ES 模組化＋共享狀態 state.js

**Files:**
- Create: `static/js/state.js`、`static/js/api.js`、`static/js/player.js`、`static/js/timeline.js`、`static/js/panels.js`、`static/js/main.js`
- Delete: `static/js/app.js`（拆完即刪）
- Modify: `static/index.html`（script 標籤、移除所有 inline onclick）

**Interfaces:**
- Consumes: Task 1 的 `app.js` 內容（函式原樣搬移的來源）。
- Produces:
  - `state.js`: `export const S`（全部共享可變狀態）、`export const els`（共用 DOM 參照）、`export function setStatus(msg, cls)`、`export function showToast(msg, duration)`、`export function setSkeleton(visible)`、`export function fmtClock(sec)`
  - `api.js`: `export async function getJSON(url)`、`export async function postJSON(url, body)`、`export async function del(url)`（僅供新程式碼使用；搬移的舊碼保留原本的 fetch 寫法）
  - `player.js`: `export { loadStream, loadVod, switchToLive, setType, startLiveTimers, stopLiveTimers, detachVodListeners, onLiveBtnClick }`
  - `timeline.js`: `export { loadTimeline, clearSelection, closeSlotActionMenu, closeDeleteModal, selectDay }`
  - `panels.js`: `export { switchTab, renderPigStatus, refreshAnomalyMap, updateVodAnomalyMap, refreshNotifications, loadBookmarks, loadSettings, saveSettings, clearPigSelection, closeBookmarkEditModal }`
  - `main.js`: 無 export；負責 init()、事件綁定、pollStorageHealth。

- [ ] **Step 1: 建立 state.js**

原 app.js 開頭的全部 `let` 狀態（原 index.html 1011–1053 行）搬成 `S` 物件屬性；DOM 參照（原 1056–1083 行）搬成 `els`；四個小 UI 工具函式（setStatus、showToast、setSkeleton、fmtClock）原樣搬入並 export。骨架：

```js
// static/js/state.js — 共享狀態、共用 DOM 參照、微型 UI 工具。不 import 任何模組。
export const S = {
  hls: null, ws: null, wsGeneration: 0, wsRetryTimer: null, wsRetryCount: 0,
  latestBoxes: [], vodStartTs: 0, bboxHistory: [], _dbg: null,
  currentCamera: null, currentType: 'rgb', animFrameId: null, isLive: true,
  currentMonth: null, selectedDay: null, monthHoursSet: new Set(),
  vodDebounceTimer: null, vodFetching: false, anomalyMap: {}, vodAlerts: [],
  showReadAlerts: false, liveAnomalyIntervalId: null, liveHandoffIntervalId: null,
  currentLiveUrl: null, currentObjectIds: new Set(), selectedObjectId: null,
  soloMode: false, sortKey: 'activity', sortDir: 1,
  selectMode: false, selectedHours: new Set(), savedSegmentsMap: new Map(),
  transportDragging: false, dragFrac: 0, seekCommitTimer: null,
  dragSeekPending: false, trackingFetchTimer: null, trackingCache: new Map(),
};
export const MAX_WS_RETRY = 5;
export const WS_RETRY_BASE_MS = 2000;

export const els = {
  video: document.getElementById('video'),
  camSelect: document.getElementById('cam-select'),
  /* …其餘 DOM 參照照原清單逐一列入（statusEl、statusTxt、skeleton、countBadge、
     latencyChip、latencyVal、toastEl、liveBtn、calLabelEl、calGridEl、prevMonthBtn、
     nextMonthBtn、timelineBar、bellBadge、pigStatusBody、alertListEl、transportEl、
     playBtn、timeCurEl、timeDurEl、seekTrack、seekBuffered、seekProgress、
     seekHandle、liveBtnT、liveLabelT）… */
};

let toastTimer = null;
export function setStatus(msg, cls = '') { /* 原樣搬入，statusEl→els.statusEl */ }
export function showToast(msg, duration = 3000) { /* 原樣搬入 */ }
export function setSkeleton(visible) { /* 原樣搬入 */ }
export function fmtClock(sec) { /* 原樣搬入（原 2340 行）*/ }
```

- [ ] **Step 2: 建立 api.js**

```js
// static/js/api.js — 新程式碼用的 fetch 封裝。搬移的舊碼保留原 fetch 寫法不改。
export async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
export async function postJSON(url, body) {
  const res = await fetch(url, { method: 'POST',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
export async function del(url) {
  const res = await fetch(url, { method: 'DELETE' });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}
```

- [ ] **Step 3: 按函式地圖拆模組**

函式搬移地圖（名稱 → 目的模組；行號為原 index.html 行號，供對照 app.js）：

| 模組 | 函式（原樣搬移） |
|---|---|
| `player.js` | connectWS(1983)、getBoxColor(2041)、drawBoxes(2045)、drawDbgHud(2169)、roundRect(2198)、keydown 'd' HUD 綁定(2191)、checkLiveHandoff(2218)、startLiveTimers(2234)、stopLiveTimers(2239)、loadStream(2253)、loadVod(1477)、switchToLive(1544)、onVodTimeUpdate/onVodSeeking/onVodSeeked(1570–1572)、attachVodListeners(1573)、detachVodListeners(1578)、scheduleTrackingFetch(1587)、applyVodBoxes(1614)、pickClosestFrame(1621)、setType(1967)、getSeekRange(2348)、updateTransport(2363)、seekToFraction(2410)、commitDragSeek(2421)、fracFromEvent(2430)、endTransportDrag(2450)、goToLiveEdge(2476)、onLiveBtnClick(2485)、seekTrack/playBtn 的 pointer/keydown/click 綁定(2437–2474) |
| `timeline.js` | localDayStart(1104)、dayHasData(1110)、loadCalendar(1117)、renderCalendar(1133)、prevMonth(1160)、nextMonth(1167)、selectDay(1174)、renderDayBar(1183)、closeSlotActionMenu(1226)、_onSlotMenuOutside(1233)、openSlotActionMenu(1238)、onUnmarkSlot(1267)、loadTimeline(1277)、updateActionBar(1286)、clearSelection(1292)、loadDaySegments(1299)、onRetainClick(1310)、onBookmarkClick(1324)、onDeleteRecClick(1423)、closeDeleteModal(1455)、confirmDeleteRecordings(1459) |
| `panels.js` | refreshAnomalyMap(1637)、updateVodAnomalyMap(1650)、switchTab(1666)、sortPigRows(1676)、onSortHeaderClick(1688)、updateSortIndicators(1694)、togglePigSelection(1702)、renderPigStatus(1707)、renderNotifications(1749)、_refreshBellBadge(1794)、_emptyNotifIfNeeded(1803)、markAlertRead(1809)、onDeleteAlertClick(1826)、onClearReadClick(1837)、refreshNotifications(1856)、loadSettings(1868)、saveSettings(1907)、loadBookmarks(1342)、openBookmarkEditModal(1391)、closeBookmarkEditModal(1398)、saveBookmarkEdit(1403)、clearPigSelection(2246) |
| `main.js` | init(2491)、pollStorageHealth(1946)、camSelect change 綁定(2535)、show-read-toggle 綁定(2526)、排序欄頭與 solo-checkbox 綁定(2568–2575)、select-mode-toggle 綁定(2576，Task 6 會移除)、末尾 `init()` 呼叫 |

機械變換規則（唯三允許的修改）：
1. 共享變數加前綴：`currentCamera` → `S.currentCamera`（`S` 物件內所有屬性同理）。
2. DOM 參照改 `els.`：`video` → `els.video` 等。
3. 模組頂部加 import、被跨模組使用的函式加 `export`。跨模組依賴方向：`main → 全部`；`player → panels`（renderPigStatus、updateVodAnomalyMap、refreshAnomalyMap、clearPigSelection、refreshNotifications）；`timeline → player`（loadVod、switchToLive）；`player → timeline`（loadDaySegments 若 loadVod 用到則 export，否則不需要）。禁止其他新依賴。

- [ ] **Step 4: index.html 移除 inline onclick、換 module script**

`<script src="/static/js/app.js">` 換成：

```html
  <script type="module" src="/static/js/main.js"></script>
```

移除 HTML 中全部 `onclick="..."` 屬性，改在各模組內綁定（綁定歸屬）：

| 元素 | 綁定位置 | 處理函式 |
|---|---|---|
| `#bell-btn` | main.js | `switchTab('notifications')` + scrollIntoView |
| `.type-btn`（rgb/thermal） | player.js | `setType(btn.dataset.type)` |
| `#prev-month-btn` / `#next-month-btn` | timeline.js | prevMonth / nextMonth |
| `#live-btn` | player.js | switchToLive |
| `#transport-live-btn` | player.js | onLiveBtnClick |
| `#btn-retain` / `#btn-bookmark` / `#btn-delete-rec` / `#btn-clear-sel` | timeline.js | onRetainClick / onBookmarkClick / onDeleteRecClick / clearSelection |
| `.tab-btn`（全部） | panels.js | `switchTab(btn.dataset.tab)`（bookmarks 分頁另呼叫 loadBookmarks） |
| `#save-settings-btn` | panels.js | saveSettings |
| `#clear-read-btn` | panels.js | onClearReadClick |
| 書籤 modal 的儲存/取消、刪除 modal 的確認/取消 | panels.js / timeline.js | saveBookmarkEdit、closeBookmarkEditModal、confirmDeleteRecordings、closeDeleteModal |

- [ ] **Step 5: 語法驗證**

```bash
rm static/js/app.js
for f in static/js/*.js; do node --check "$f" || echo "FAIL: $f"; done
grep -c 'onclick=' static/index.html
```

Expected: 全部檔案 pass；onclick 計數為 0。

- [ ] **Step 6: 瀏覽器全功能 smoke**

啟動 app，逐項確認：live 播放與 bbox、RGB/Thermal 切換、換攝影機、月曆選日、點小時格進 VOD、transport 拖拉、回到 Live、豬隻表排序/選取、通知已讀、書籤列表、設定載入與儲存、按 `d` 開 HUD。console 無錯誤。

- [ ] **Step 7: Commit**

```bash
git add static/ && git commit -m "refactor(frontend): 拆分 ES modules（state/api/player/timeline/panels/main）"
```

---

### Task 3: 設計 token 重整＋SVG 圖示 sprite＋Header

**Files:**
- Modify: `static/css/app.css`（token 區全量替換＋header 區）
- Modify: `static/index.html`（sprite、header DOM、字體 link）

**Interfaces:**
- Produces: 新 token 集（下方 CSS 為準）；icon sprite（`#i-bell`、`#i-gear`、`#i-grid`、`#i-single`、`#i-calendar`、`#i-play`、`#i-pause`、`#i-lock`、`#i-star`、`#i-trash`、`#i-x`、`#i-alert`、`#i-chev-l`、`#i-chev-r`）；`.icon` 通用 class。後續 task 一律用 `<svg class="icon"><use href="#i-xxx"/></svg>`。

- [ ] **Step 1: 替換 token 區**

`app.css` 開頭兩個 `:root` 區塊整段替換為：

```css
:root {
  --font-body: 'DM Sans', 'Noto Sans TC', 'Segoe UI', system-ui, sans-serif;
  --text-xs:   clamp(0.75rem,  0.7rem  + 0.25vw, 0.875rem);
  --text-sm:   clamp(0.875rem, 0.8rem  + 0.35vw, 1rem);
  --text-base: clamp(1rem,     0.95rem + 0.25vw, 1.125rem);
  --text-lg:   clamp(1.125rem, 1rem    + 0.75vw, 1.5rem);
  --space-1: 0.25rem; --space-2: 0.5rem; --space-3: 0.75rem;
  --space-4: 1rem;    --space-6: 1.5rem; --space-8: 2rem;
  --radius-sm: 0.375rem; --radius-md: 0.5rem;
  --radius-lg: 0.75rem;  --radius-full: 9999px;
  --transition: 160ms cubic-bezier(0.16, 1, 0.3, 1);

  /* 深色階（帶冷綠底蘊） */
  --bg:        #0b0e0d;
  --surface:   #101412;
  --surface-2: #161b19;
  --surface-3: #1e2421;
  --border:  rgba(255,255,255,0.08);
  --divider: rgba(255,255,255,0.05);
  --text:       #e6eae8;
  --text-muted: #8b948f;
  --text-faint: #4c554f;

  /* 語意色：base / dim（背景）/ border 三件套 */
  --accent:        #2fc98a;
  --accent-dim:    rgba(47,201,138,0.14);
  --accent-border: rgba(47,201,138,0.3);
  --accent-hover:  #27b57a;
  --vod:        #e8a13c;
  --vod-dim:    rgba(232,161,60,0.14);
  --vod-border: rgba(232,161,60,0.3);
  --error:        #e05252;
  --error-dim:    rgba(224,82,82,0.14);
  --error-border: rgba(224,82,82,0.3);
  --thermal:        #ff8c42;
  --thermal-dim:    rgba(255,140,66,0.14);
  --thermal-border: rgba(255,140,66,0.3);
  --info:        #4da3ff;
  --info-dim:    rgba(77,163,255,0.14);
  --info-border: rgba(77,163,255,0.3);

  --shadow-md: 0 4px 16px rgba(0,0,0,0.4);
  --shadow-lg: 0 12px 40px rgba(0,0,0,0.6);
}
.icon { width: 18px; height: 18px; stroke: currentColor; stroke-width: 1.6;
        fill: none; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0; }
```

- [ ] **Step 2: 全檔清掉舊 token 引用與寫死色碼**

```bash
grep -n "warning\|--surface-1\b\|#2ecc71\|#f39c12\|#e74c3c" static/css/app.css
```

逐一替換：`var(--warning)` → `var(--vod)`；`.storage-pill.ok/.degraded/.down` 的寫死色 → `var(--accent)`/`var(--vod)`/`var(--error)`；`.slot-action-menu` 的 `var(--surface-1)` → `var(--surface-2)`。替換後上述 grep 應為 0 hits（`--vod` 自身除外）。

- [ ] **Step 3: index.html 加字體與 sprite**

字體 link 加上 Noto Sans TC：

```html
  <link href="https://fonts.googleapis.com/css2?family=DM+Sans:wght@300..600&family=Noto+Sans+TC:wght@400;500;700&display=swap" rel="stylesheet">
```

`<body>` 開頭插入 sprite（`display:none`），內含 14 個 `<symbol viewBox="0 0 24 24">`：bell（鈴鐺輪廓+鈴舌）、gear（齒輪）、grid（田字四方格）、single（單一矩形）、calendar（月曆框+雙耳）、play（三角）、pause（雙豎線）、lock（鎖體+弓）、star（五角星）、trash（桶+蓋）、x（交叉）、alert（三角+驚嘆號）、chev-l/chev-r（左右箭頭）。全部 stroke 風格、線寬 1.6，與 `.icon` class 相容。

- [ ] **Step 4: Header 重排**

header DOM 換為（保留 `.logo` 原 SVG）：

```html
  <header>
    <div class="logo">…原 SVG＋文字…</div>
    <div class="header-controls">
      <select id="cam-select" class="cam-select" aria-label="選擇攝影機"></select>
      <div class="type-toggle" role="group" aria-label="影像類型">
        <button class="type-btn active" id="btn-rgb" data-type="rgb" aria-pressed="true">RGB</button>
        <button class="type-btn" id="btn-thermal" data-type="thermal" aria-pressed="false">Thermal</button>
      </div>
      <button class="icon-btn" id="view-toggle-btn" aria-label="切換多畫面" title="多畫面監看">
        <svg class="icon"><use href="#i-grid"/></svg></button>
    </div>
    <div class="header-status">
      <button class="icon-btn" id="bell-btn" aria-label="通知中心">
        <svg class="icon"><use href="#i-bell"/></svg><span id="bell-badge" style="display:none">0</span></button>
      <button class="icon-btn" id="settings-btn" aria-label="設定">
        <svg class="icon"><use href="#i-gear"/></svg></button>
      <span id="storage-pill" class="storage-pill" title="儲存狀態" style="display:none">●</span>
    </div>
  </header>
```

原 `.controls` 區塊刪除（內容已併入 header）。CSS：`.icon-btn`（36px 圓形、hover surface-3、badge 絕對定位右上）、`.header-controls`（置中彈性收納、gap space-3、`flex-wrap: wrap`）。`#settings-btn`/`#view-toggle-btn` 綁定先留空殼（`console.debug` 佔位），Task 5/8 接手。

- [ ] **Step 5: 驗證＋Commit**

瀏覽器確認 header 一列收納全部控制項、圖示清晰、窄螢幕換行不破版。

```bash
git add static/ && git commit -m "feat(frontend): 新設計 token、SVG sprite、header 重排"
```

---

### Task 4: 雙欄響應式 shell

**Files:**
- Modify: `static/index.html`（body 結構重組）
- Modify: `static/css/app.css`（layout 區新增）

**Interfaces:**
- Produces: `.layout` / `.left-col` / `.right-col` 結構；右欄分頁只剩 豬隻狀態/通知/書籤（設定分頁的 DOM 移除，內容移到 Task 5 的 drawer）。後續 task 的 DOM 都掛在這結構下。

- [ ] **Step 1: body 結構重組**

```html
<body>
  <svg style="display:none">…sprite…</svg>
  <header>…Task 3 成果…</header>
  <main class="layout">
    <section class="left-col">
      <div class="video-card">…原 video-wrap + transport + video-footer…</div>
      <div id="timeline-section">…原時間軸區…</div>
      <div id="grid-view" hidden></div><!-- Task 8 填入 -->
    </section>
    <aside class="right-col" aria-label="資料面板">
      <div id="bottom-panel"><!-- id 保留避免動到 JS 選擇器 -->
        <div id="tab-bar">…僅 pig-status / notifications / bookmarks 三鈕…</div>
        <div id="tab-pig-status" class="tab-content active">…</div>
        <div id="tab-notifications" class="tab-content">…</div>
        <div id="tab-bookmarks" class="tab-content">…</div>
      </div>
    </aside>
  </main>
  …modals、toast…
</body>
```

`#tab-settings` div 與其 tab 按鈕自此移除（設定欄位 DOM 先原封搬進 Task 5 的 drawer 容器，不刪欄位）。

- [ ] **Step 2: layout CSS**

```css
body { display: block; padding: 0; }
header { position: sticky; top: 0; z-index: 50; width: 100%; max-width: none;
         padding: var(--space-3) var(--space-4); background: var(--surface);
         border-bottom: 1px solid var(--border); }
.layout { display: grid; grid-template-columns: 1fr; gap: var(--space-4);
          padding: var(--space-4); max-width: 1720px; margin: 0 auto; }
.video-card, #timeline-section, #bottom-panel { max-width: none; }
@media (min-width: 1200px) {
  .layout { grid-template-columns: minmax(0, 1fr) 400px; align-items: start; }
  .right-col { position: sticky; top: 72px;
               max-height: calc(100dvh - 88px); display: flex; }
  .right-col #bottom-panel { display: flex; flex-direction: column; width: 100%; }
  .right-col .tab-content.active { overflow-y: auto; flex: 1; }
}
@media (max-width: 1199px) {
  #tab-bar { position: sticky; top: 60px; z-index: 40; }
}
```

- [ ] **Step 3: 驗證＋Commit**

桌機寬度：影片與豬隻表同屏、右欄獨立捲動；縮到 <1200px：單欄堆疊、分頁列吸附。console 無錯誤、原功能全部照常。

```bash
git add static/ && git commit -m "feat(frontend): 桌機雙欄／手機單欄響應式 shell"
```

---

### Task 5: 設定 drawer（五分組）

**Files:**
- Modify: `static/index.html`（drawer DOM）
- Modify: `static/css/app.css`（drawer 樣式）
- Modify: `static/js/panels.js`（drawer 開關、連動停用、驗證、dirty 檢查）
- Modify: `static/js/main.js`（`#settings-btn` 綁定）

**Interfaces:**
- Consumes: Task 2 的 `loadSettings`/`saveSettings`（欄位 id 全部不變：`set-analysis-interval` 等）。
- Produces: `panels.js` 新增 `export function openSettingsDrawer()`、`export function closeSettingsDrawer(force = false)`、`export function initSettingsDrawer()`（main.js 的 init 呼叫一次）。

- [ ] **Step 1: drawer DOM**

`</main>` 後插入；**原設定欄位 DOM 原封搬入對應分組**（id 不變，loadSettings/saveSettings 零修改）：

```html
<div id="settings-overlay" hidden></div>
<aside id="settings-drawer" aria-label="系統設定" hidden>
  <div class="drawer-head">
    <h2>系統設定</h2>
    <button class="icon-btn" id="settings-close-btn" aria-label="關閉">
      <svg class="icon"><use href="#i-x"/></svg></button>
  </div>
  <div class="drawer-body">
    <section class="settings-group">
      <h3>異常分析</h3><p class="group-desc">決定多久評估一次活動量、以及判定異常的敏感度。</p>
      …分析間隔、評估視窗、體溫偵測、異常閾值 σ 四個 .settings-field…
    </section>
    <section class="settings-group">
      <h3>錄影排程</h3><p class="group-desc">停錄時段仍可看直播，只是不落地存檔。</p>
      …夜間不錄影開關、起訖時間…
    </section>
    <section class="settings-group">
      <h3>儲存空間</h3><p class="group-desc">空間不足時自動停止落地並推播警告。</p>
      …保留天數、最低可用空間、監控間隔…
    </section>
    <section class="settings-group">
      <h3>推播通知</h3>
      …ntfy 開關、URL、串流重建優先級…
    </section>
    <details class="settings-group advanced"><summary>進階</summary>
      …夜間停 GPU 開關、起訖時間…
    </details>
    <div class="settings-save-row">
      <button id="save-settings-btn">儲存設定</button><span id="settings-status"></span>
    </div>
  </div>
</aside>
```

- [ ] **Step 2: drawer CSS**

```css
#settings-overlay { position: fixed; inset: 0; background: rgba(0,0,0,0.5);
  z-index: 90; opacity: 0; transition: opacity 200ms; }
#settings-overlay.open { opacity: 1; }
#settings-drawer { position: fixed; top: 0; right: 0; bottom: 0; width: min(420px, 100vw);
  background: var(--surface); border-left: 1px solid var(--border); z-index: 91;
  transform: translateX(100%); transition: transform 240ms cubic-bezier(0.16,1,0.3,1);
  display: flex; flex-direction: column; }
#settings-drawer.open { transform: translateX(0); }
.drawer-head { display: flex; align-items: center; justify-content: space-between;
  padding: var(--space-4); border-bottom: 1px solid var(--divider); }
.drawer-body { overflow-y: auto; padding: var(--space-4); flex: 1; }
.settings-group { margin-bottom: var(--space-6); }
.settings-group h3 { font-size: var(--text-sm); margin-bottom: var(--space-1); }
.group-desc { font-size: var(--text-xs); color: var(--text-muted); margin-bottom: var(--space-3); }
.settings-field.disabled { opacity: 0.45; pointer-events: none; }
.settings-field input.invalid { border-color: var(--error); }
.field-error { color: var(--error); font-size: var(--text-xs); }
```

- [ ] **Step 3: panels.js drawer 邏輯**

```js
let _settingsDirty = false;

export function openSettingsDrawer() {
  const d = document.getElementById('settings-drawer');
  const o = document.getElementById('settings-overlay');
  d.hidden = false; o.hidden = false;
  requestAnimationFrame(() => { d.classList.add('open'); o.classList.add('open'); });
  loadSettings().then(() => { _settingsDirty = false; });
}

export function closeSettingsDrawer(force = false) {
  if (_settingsDirty && !force &&
      !confirm('有未儲存的設定變更，確定要關閉嗎？')) return;
  const d = document.getElementById('settings-drawer');
  const o = document.getElementById('settings-overlay');
  d.classList.remove('open'); o.classList.remove('open');
  setTimeout(() => { d.hidden = true; o.hidden = true; }, 240);
  _settingsDirty = false;
}

// 連動停用：總開關 off → 附屬欄位 .disabled
const DEP_FIELDS = {
  'set-recording_schedule_enabled': ['set-recording_off_start', 'set-recording_off_end'],
  'set-ntfy_enabled': ['set-ntfy_url', 'set-ntfy_revive_priority'],
  'set-gpu_off_schedule_enabled': ['set-gpu_off_start', 'set-gpu_off_end'],
  'set-temp-enabled': [],   // select，值為 'true'/'false'，無附屬欄位
};
function syncDepFields() {
  for (const [master, deps] of Object.entries(DEP_FIELDS)) {
    const m = document.getElementById(master);
    const on = m.type === 'checkbox' ? m.checked : m.value === 'true';
    deps.forEach(id => document.getElementById(id)
      .closest('.settings-field').classList.toggle('disabled', !on));
  }
}
// 即時驗證：σ ≥ 1.0、保留天數 1–365、最低空間 ≥1、監控間隔 ≥5
function validateField(el) {
  const min = parseFloat(el.min), max = parseFloat(el.max);
  const v = parseFloat(el.value);
  const bad = el.type === 'number' && el.value !== '' &&
    (isNaN(v) || (isFinite(min) && v < min) || (isFinite(max) && v > max));
  el.classList.toggle('invalid', bad);
  return !bad;
}
export function initSettingsDrawer() {
  const body = document.querySelector('#settings-drawer .drawer-body');
  body.addEventListener('input', e => {
    _settingsDirty = true;
    if (e.target.matches('input[type="number"]')) validateField(e.target);
    syncDepFields();
  });
  document.getElementById('settings-close-btn')
    .addEventListener('click', () => closeSettingsDrawer());
  document.getElementById('settings-overlay')
    .addEventListener('click', () => closeSettingsDrawer());
  document.getElementById('save-settings-btn').addEventListener('click', async () => {
    const nums = [...body.querySelectorAll('input[type="number"]')];
    if (!nums.every(validateField)) return;
    await saveSettings();
    _settingsDirty = false;
  });
}
```

`saveSettings` 本體不動；`loadSettings` 結尾補一行 `syncDepFields()`。main.js：`#settings-btn` click → `openSettingsDrawer()`；main.js 的 init 呼叫 `initSettingsDrawer()`；Esc 鍵關閉 drawer。

- [ ] **Step 4: 驗證＋Commit**

`node --check`；瀏覽器：齒輪開 drawer、五分組與說明可見、關 ntfy 開關 → URL 欄變暗、σ 填 0.5 → 標紅且不能存、改值後直接關 → 出現確認、儲存成功顯示狀態。手機寬度 drawer 全螢幕。

```bash
git add static/ && git commit -m "feat(frontend): 設定改為右側 drawer，五分組＋連動停用＋即時驗證"
```

---

### Task 6: 時間軸互動重做

**Files:**
- Modify: `static/index.html`（日期按鈕＋popover 容器；移除 select-toggle）
- Modify: `static/css/app.css`（小時格、popover、action bar）
- Modify: `static/js/timeline.js`（popover 開關、長按/shift 多選）
- Modify: `static/js/main.js`（移除 select-mode-toggle 綁定）

**Interfaces:**
- Consumes: Task 2 的 renderCalendar/selectDay/renderDayBar/updateActionBar/clearSelection（原樣邏輯）。
- Produces: `S.selectMode` 改由長按/shift 進入（不再有 checkbox）；`timeline.js` 新增內部函式 `enterSelectMode()`、`exitSelectMode()`（`clearSelection` 內呼叫 exit）。

- [ ] **Step 1: 日期按鈕＋popover**

`#timeline-section` 開頭改為：

```html
    <div id="timeline-controls">
      <button id="date-btn" aria-haspopup="dialog" aria-expanded="false">
        <svg class="icon"><use href="#i-calendar"/></svg>
        <span id="date-btn-label">—</span>
        <svg class="icon" style="width:14px;height:14px"><use href="#i-chev-r" transform="rotate(90 12 12)"/></svg>
      </button>
      <button id="live-btn" style="display:none">● Live</button>
    </div>
    <div id="calendar" class="cal-popover" hidden>…原月曆內容不動…</div>
```

timeline.js：`#date-btn` click → toggle `#calendar` hidden＋`aria-expanded`；`selectDay()` 結尾補：更新 `#date-btn-label`（`YYYY/MM/DD` 格式）＋收合 popover；點 popover 外部收合。CSS：`.cal-popover { position: absolute; z-index: 60; box-shadow: var(--shadow-lg); border: 1px solid var(--border); }`（`#timeline-controls` 設 `position: relative`）。移除 `<label class="select-toggle">` 與 main.js 對應綁定。

- [ ] **Step 2: 小時格重繪**

```css
#timeline-bar { height: auto; gap: 3px; background: none; }
.timeline-slot { height: 40px; border-radius: var(--radius-sm);
  display: flex; align-items: center; justify-content: center;
  font-size: 0.68rem; color: var(--text-faint); background: var(--surface-2);
  font-variant-numeric: tabular-nums; }
.timeline-slot.has-data { background: var(--surface-3); color: var(--text-muted);
  cursor: pointer; border: 1px solid var(--border); }
.timeline-slot.has-data:hover { border-color: var(--accent-border); color: var(--text); }
.timeline-slot.selected { background: var(--accent); color: #04120c; font-weight: 600; }
.timeline-slot.slot-selected { outline: 2px solid var(--accent); outline-offset: -2px; }
@media (max-width: 1199px) {
  .timeline-slot { height: 44px; }
  .cal-day { padding: 13px 0; }   /* 月曆日期格觸控目標 ≥44px */
}
```

`renderDayBar` 建 slot 處加一行：`slot.textContent = String(h).padStart(2, '0');`（h 為 0–23 小時序；標記 marker 的 append 邏輯不動，數字在 marker 之前）。

- [ ] **Step 3: 長按／shift 多選**

timeline.js 新增，並改 slot 綁定（原 click 播放邏輯保留為「非多選模式」分支）：

```js
let _longPressTimer = null;

function enterSelectMode() {
  S.selectMode = true;
  document.getElementById('timeline-bar').classList.add('selecting');
  updateActionBar();
}
function exitSelectMode() {
  S.selectMode = false;
  document.getElementById('timeline-bar').classList.remove('selecting');
}
// renderDayBar 內每個 has-data slot 的綁定改為：
//   slot.addEventListener('pointerdown', () => {
//     _longPressTimer = setTimeout(() => { _longPressTimer = null;
//       enterSelectMode(); toggleHourSelection(slot, hourTs); }, 500);
//   });
//   slot.addEventListener('pointerup', (e) => {
//     if (_longPressTimer === null) return;        // 長按已觸發，此次不當 click
//     clearTimeout(_longPressTimer); _longPressTimer = null;
//     if (e.shiftKey && !S.selectMode) enterSelectMode();
//     if (S.selectMode) toggleHourSelection(slot, hourTs);
//     else /* 原本的播放（loadVod）邏輯 */;
//   });
//   slot.addEventListener('pointerleave', () => {
//     clearTimeout(_longPressTimer); _longPressTimer = null; });
function toggleHourSelection(slot, hourTs) {
  if (S.selectedHours.has(hourTs)) { S.selectedHours.delete(hourTs);
    slot.classList.remove('slot-selected'); }
  else { S.selectedHours.add(hourTs); slot.classList.add('slot-selected'); }
  if (S.selectedHours.size === 0) exitSelectMode();
  updateActionBar();
}
```

`clearSelection()` 原樣保留、結尾加 `exitSelectMode()`。`#storage-action-bar` CSS 改為固定底部滑出（`position: fixed; bottom: 0; left: 0; right: 0; transform: translateY(100%); transition: transform 200ms;`，`.visible` 時 `translateY(0)`；內部按鈕配 icon：lock/star/trash/x）。

- [ ] **Step 4: 驗證＋Commit**

`node --check`；瀏覽器：日期按鈕開合月曆、選日自動收合並更新標籤；小時格顯示數字、四種狀態可辨識；長按（觸控模擬）與 shift+點擊進多選、底部操作列滑出、保留/書籤/刪除流程照舊；點格播放（非多選）照舊。

```bash
git add static/ && git commit -m "feat(frontend): 時間軸重做——日期 popover、小時格狀態分層、長按多選"
```

---

### Task 7: VOD 橫幅＋豬隻狀態表強化

**Files:**
- Modify: `static/index.html`（banner DOM）
- Modify: `static/css/app.css`
- Modify: `static/js/player.js`（loadVod/switchToLive 顯隱 banner）
- Modify: `static/js/panels.js`（renderPigStatus 活動量橫條）

**Interfaces:**
- Consumes: `S.vodStartTs`（loadVod 設定）、renderPigStatus 的列渲染迴圈。
- Produces: `#vod-banner` 元素；`.activity-bar` 樣式。

- [ ] **Step 1: banner**

`.video-card` 內、`#video-wrap` 之前：

```html
    <div id="vod-banner" hidden>
      <svg class="icon"><use href="#i-play"/></svg>
      <span id="vod-banner-text">回放中</span>
      <button id="vod-banner-live-btn">回到直播</button>
    </div>
```

```css
#vod-banner { display: flex; align-items: center; gap: var(--space-2);
  padding: var(--space-2) var(--space-4); background: var(--vod-dim);
  color: var(--vod); border-bottom: 1px solid var(--vod-border);
  font-size: var(--text-xs); font-weight: 500; }
#vod-banner[hidden] { display: none; }
#vod-banner button { margin-left: auto; padding: 2px 10px;
  border: 1px solid var(--vod-border); border-radius: var(--radius-full);
  color: var(--vod); }
#vod-banner button:hover { background: var(--vod); color: #140d02; }
```

player.js：`loadVod(startTs)` 內（成功載入處）：

```js
  const b = document.getElementById('vod-banner');
  const d = new Date(startTs * 1000);
  const hh = String(d.getHours()).padStart(2, '0');
  document.getElementById('vod-banner-text').textContent =
    `回放中：${d.getMonth() + 1}/${d.getDate()} ${hh}:00–${hh}:59`;
  b.hidden = false;
```

`switchToLive()` 開頭：`document.getElementById('vod-banner').hidden = true;`。`#vod-banner-live-btn` 綁 `switchToLive`（player.js 內一次性綁定）。

- [ ] **Step 2: 豬隻表**

CSS：

```css
.anomaly-row td { background: none; box-shadow: inset 3px 0 0 var(--error); }
.anomaly-cell { color: var(--error); font-weight: 600; }
.activity-bar { height: 3px; border-radius: 2px; background: var(--accent);
  opacity: 0.7; margin-top: 3px; max-width: 72px; }
.anomaly-row .activity-bar { background: var(--error); }
```

panels.js `renderPigStatus()`：渲染活動量儲存格處，在數值下加

```js
    const maxRate = Math.max(...rows.map(r => r.activity ?? 0), 1e-9);
    // 每列：
    const bar = document.createElement('div');
    bar.className = 'activity-bar';
    bar.style.width = `${Math.max(4, Math.round((r.activity ?? 0) / maxRate * 100))}%`;
    activityTd.appendChild(bar);
```

（`rows`/`r.activity` 對齊 renderPigStatus 既有變數名；實作時以現檔為準。）

- [ ] **Step 3: 驗證＋Commit**

點小時格進 VOD → 琥珀橫幅出現、文字正確、按「回到直播」橫幅消失且回 live。豬隻表：活動量下有相對長度橫條、異常列左紅邊＋紅字、不再整列紅底。

```bash
git add static/ && git commit -m "feat(frontend): VOD 回放橫幅＋豬隻表活動量橫條與異常列重繪"
```

---

### Task 8: Grid 監看模式

**Files:**
- Create: `static/js/grid.js`
- Modify: `static/index.html`（grid 容器已在 Task 4 建立）
- Modify: `static/css/app.css`
- Modify: `static/js/main.js`（view-toggle 綁定＋localStorage）

**Interfaces:**
- Consumes: `GET /cameras`、`GET /stream/{cam}/live?type=rgb`、`GET /alerts/active?camera_id=X`、全域 `Hls`（CDN）。
- Produces: `grid.js` `export async function enterGrid()`、`export function leaveGrid()`。main.js 管理 `viewMode`（`'single' | 'grid'`，存 localStorage key `viewMode`）。

- [ ] **Step 1: grid.js 完整實作**

```js
// static/js/grid.js — 多畫面純監看。每格：live RGB（靜音、無 bbox），
// 資訊列：名稱＋追蹤數＋LIVE 狀態；有異常告警 → 紅框＋角標。
// 離開時銷毀全部播放器。點格 → main.js 切回單畫面選該攝影機。
import { getJSON } from './api.js';

let _players = [];        // [{hls, video}]
let _anomalyTimer = null;
let _onPickCamera = null; // main.js 注入的 callback

export function bindGridPick(fn) { _onPickCamera = fn; }

export async function enterGrid() {
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = false;
  const { cameras } = await getJSON('/cameras');
  root.style.setProperty('--grid-cols',
    cameras.length <= 2 ? 2 : cameras.length <= 4 ? 2 : 3);
  for (const cam of cameras) buildTile(root, cam);
  _anomalyTimer = setInterval(refreshTileBadges, 30000);
  refreshTileBadges();
}

export function leaveGrid() {
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = true;
}

async function buildTile(root, cam) {
  const tile = document.createElement('div');
  tile.className = 'grid-tile';
  tile.dataset.cam = cam;
  tile.innerHTML = `
    <video muted playsinline></video>
    <div class="tile-info">
      <span class="tile-name"></span>
      <span class="tile-count" title="追蹤中豬隻數"></span>
      <span class="tile-live"><span class="status-dot"></span>LIVE</span>
    </div>
    <span class="tile-alert" hidden><svg class="icon"><use href="#i-alert"/></svg></span>`;
  tile.querySelector('.tile-name').textContent = cam;
  tile.addEventListener('click', () => _onPickCamera && _onPickCamera(cam));
  root.appendChild(tile);
  try {
    const live = await getJSON(`/stream/${cam}/live?type=rgb`);
    const video = tile.querySelector('video');
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
                            maxBufferLength: 20 });
      hls.loadSource(live.url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) tileError(tile, cam); });
      _players.push({ hls, video });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = live.url; video.play().catch(() => {});
      _players.push({ hls: null, video });
    }
  } catch (_) {
    tileOffline(tile);
  }
}

function tileOffline(tile) {
  tile.classList.add('offline');
  tile.querySelector('.tile-live').innerHTML = '無訊號';
}

function tileError(tile, cam) {
  tileOffline(tile);
  if (tile.querySelector('.tile-retry')) return;
  const btn = document.createElement('button');
  btn.className = 'tile-retry';
  btn.textContent = '重試';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    tile.classList.remove('offline'); btn.remove();
    tile.querySelector('.tile-live').innerHTML =
      '<span class="status-dot"></span>LIVE';
    await rebuildTilePlayer(tile, cam);
  });
  tile.appendChild(btn);
}

async function rebuildTilePlayer(tile, cam) {
  const video = tile.querySelector('video');
  const old = _players.find(p => p.video === video);
  if (old) { try { old.hls?.destroy(); } catch (_) {} _players = _players.filter(p => p !== old); }
  const root = document.getElementById('grid-view');
  tile.remove();
  await buildTile(root, cam);
}

async function refreshTileBadges() {
  for (const tile of document.querySelectorAll('.grid-tile:not(.offline)')) {
    const cam = tile.dataset.cam;
    try {
      const data = await getJSON(`/alerts/active?camera_id=${cam}`);
      const cache = data.cache?.[cam] ?? {};
      const entries = Object.values(cache);
      tile.querySelector('.tile-count').textContent = `${entries.length} 隻`;
      const hasAnomaly = entries.some(e => e.activity_anomaly || e.temp_anomaly);
      tile.classList.toggle('alerting', hasAnomaly);
      tile.querySelector('.tile-alert').hidden = !hasAnomaly;
    } catch (_) {}
  }
}
```

- [ ] **Step 2: grid CSS**

```css
#grid-view { display: grid; gap: var(--space-3);
  grid-template-columns: repeat(var(--grid-cols, 2), 1fr); }
#grid-view[hidden] { display: none; }
.grid-tile { position: relative; background: var(--surface); overflow: hidden;
  border: 1px solid var(--border); border-radius: var(--radius-lg);
  aspect-ratio: 4/3; cursor: pointer;
  transition: border-color var(--transition); }
.grid-tile:hover { border-color: var(--accent-border); }
.grid-tile video { width: 100%; height: 100%; object-fit: contain; background: #000; }
.grid-tile.alerting { border-color: var(--error); box-shadow: 0 0 0 1px var(--error); }
.tile-info { position: absolute; left: 0; right: 0; bottom: 0;
  display: flex; align-items: center; gap: var(--space-2);
  padding: var(--space-1) var(--space-3);
  background: linear-gradient(transparent, rgba(0,0,0,0.75));
  font-size: var(--text-xs); }
.tile-name { font-weight: 600; }
.tile-count { color: var(--text-muted); }
.tile-live { margin-left: auto; color: var(--accent); display: inline-flex;
  align-items: center; gap: 4px; font-weight: 600; letter-spacing: 0.05em; }
.grid-tile.offline .tile-live { color: var(--text-faint); }
.grid-tile.offline video { opacity: 0.2; }
.tile-alert { position: absolute; top: var(--space-2); right: var(--space-2);
  color: var(--error); }
.tile-retry { position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  padding: var(--space-1) var(--space-4); background: var(--surface-3);
  border: 1px solid var(--border); border-radius: var(--radius-full); }
@media (max-width: 700px) { #grid-view { grid-template-columns: 1fr !important; } }
```

- [ ] **Step 3: main.js 模式切換**

```js
import { enterGrid, leaveGrid, bindGridPick } from './grid.js';

let viewMode = 'single';

async function setViewMode(mode) {
  if (mode === viewMode) return;
  viewMode = mode;
  try { localStorage.setItem('viewMode', mode); } catch (_) {}
  const singleEls = [document.querySelector('.video-card'),
                     document.getElementById('timeline-section')];
  const toggleIcon = document.querySelector('#view-toggle-btn use');
  if (mode === 'grid') {
    stopLiveTimers();
    if (S.hls) { S.hls.destroy(); S.hls = null; }   // 單畫面播放器停掉省資源
    singleEls.forEach(el => el.hidden = true);
    toggleIcon.setAttribute('href', '#i-single');
    await enterGrid();
  } else {
    leaveGrid();
    singleEls.forEach(el => el.hidden = false);
    toggleIcon.setAttribute('href', '#i-grid');
    loadStream();
    loadTimeline();
    startLiveTimers();
  }
}

bindGridPick(cam => {
  S.currentCamera = cam;
  els.camSelect.value = cam;
  try { localStorage.setItem('lastCamera', cam); } catch (_) {}
  setViewMode('single');
});
document.getElementById('view-toggle-btn')
  .addEventListener('click', () => setViewMode(viewMode === 'grid' ? 'single' : 'grid'));
// init() 尾端：
//   const savedMode = (() => { try { return localStorage.getItem('viewMode'); } catch (_) { return null; } })();
//   if (savedMode === 'grid') setViewMode('grid');
```

（上行三元切換為 `setViewMode(viewMode === 'grid' ? 'single' : 'grid')`——實作時照此寫。）

- [ ] **Step 4: 驗證＋Commit**

`node --check static/js/grid.js static/js/main.js`；瀏覽器：田字鈕進 grid（單畫面與時間軸隱藏、播放器銷毀）、每格播 live＋名稱/隻數/LIVE 點、斷線攝影機顯示無訊號佔位、點格回單畫面且選中該攝影機、重新整理後停留在 grid 模式、切回單畫面全功能正常。

```bash
git add static/ && git commit -m "feat(frontend): grid 多畫面純監看模式"
```

---

### Task 9: 收尾——全站打磨、測試、文件

**Files:**
- Modify: `static/css/app.css`（微調收尾）
- Modify: `CLAUDE.md`（前端慣例更新）
- Test: 全套 pytest

- [ ] **Step 1: 全站一致性掃描**

```bash
grep -n "style=\"" static/index.html          # 目標：inline style 清到 0（sprite 的 display:none 與 badge 顯隱例外）
grep -n "#[0-9a-fA-F]\{6\}\|#[0-9a-fA-F]\{3\}\b" static/css/app.css | grep -v ":root"  # 寫死色碼盤點
```

殘餘 inline style 移入 app.css；寫死色碼換 token（video 黑底 `#000`、選中格深色文字等刻意者保留並加註解）。確認 `prefers-reduced-motion` 區塊涵蓋新增的 drawer/action bar/popover 過場。

- [ ] **Step 2: 後端測試全綠**

```bash
uv run pytest -p no:cacheprovider
```

Expected: 0 failed（本計畫不碰後端；若有失敗即為意外回歸，先修再繼續）。

- [ ] **Step 3: 全功能驗證清單（瀏覽器實測）**

live 播放與 bbox 對齊、按 `d` HUD、RGB/Thermal、換攝影機、VOD 回放（橫幅/PDT 對齊/transport 拖拉/回直播）、時間軸（選日/選時/長按多選/保留/書籤/刪除）、grid（進出/點格/斷線佔位/重試）、設定 drawer（分組/連動/驗證/dirty 提示/儲存）、通知（已讀/清空/badge）、書籤列表與編輯、RWD 三檔（≥1200 / 700–1199 / ≤480）、`prefers-reduced-motion` 模擬。

- [ ] **Step 4: 更新 CLAUDE.md**

「前端 inline `<script>` 語法檢查」一條改為：

```markdown
- **前端已拆檔**（2026-07-18 改版）：`static/index.html`（結構）＋`static/css/app.css`＋
  `static/js/{state,api,player,timeline,panels,grid,main}.js`（ES modules，零 build）。
  JS 改完直接 `node --check static/js/<file>.js`；共享狀態集中在 `state.js` 的 `S` 物件。
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "chore(frontend): 改版收尾——樣式一致性、驗證清單、CLAUDE.md 更新"
```

完成後使用 superpowers:finishing-a-development-branch 決定合併方式。
