# 前端迭代 2 Implementation Plan（grid 時段回放、thermal 無訊號、版面修正）

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** grid 模式支援共用時間軸同步回放；thermal 無來源時單畫面與 grid 皆顯示「無訊號」；修正桌機影片過大、右欄面板高度不一、tab-bar 重疊三項版面問題。

**Architecture:** 後端 `/cameras` 以向後相容方式附帶 `active_types`（讀 `hls_manager._last_seen`）。前端維持 ES modules 零 build：grid.js 新增輕量時段選擇器（不重用 timeline.js），`_gridGen` 守衛涵蓋所有重建路徑；player.js 加無訊號佔位與 grid 型別切換 hook（CustomEvent，維持依賴方向）。

**Tech Stack:** FastAPI、hls.js、vanilla ES modules、pytest、headless Chrome CDP。

**Spec:** `docs/superpowers/specs/2026-07-20-frontend-iteration2-design.md`

## Global Constraints

- ES modules、零 build step；依賴方向：`state.js`/`api.js` 不 import 其他模組；`grid.js` 只 import `state.js`/`api.js`；`player.js` 不得 import `grid.js`（跨界用 CustomEvent，`main.js` 串接）。
- 禁止掛 `window`；事件一律 `addEventListener`；動態使用者/DB 字串走 `textContent`。
- 既有 HLS/PDT/bbox/transport 邏輯行為不變（本輪允許的行為新增：無訊號佔位、setType 的 grid 分支）。
- 「無訊號」＝預期狀態（無來源/無錄影，**不給**重試鈕）；「連線錯誤」＝異常（給重試鈕）。兩種佔位視覺可區分。
- `localStorage` 只記 `viewMode`/`lastCamera`，不記 grid 回放時段。
- 每個改動的 js 檔 `node --check` 通過；全套 `uv run pytest -p no:cacheprovider` 0 失敗（基準 278 passed＋本計畫新增）。
- Headless 驗證對真實部署（`uv run uvicorn main:app --port 18321`）：**不要讀 `.env`**（權限會拒）；對真實 DB 僅讀取性互動。
- 回應/commit 訊息/report 用繁體中文。

---

### Task 1: 後端 `/cameras` 擴充 active_types＋前端消費

**Files:**
- Modify: `hls_manager.py`（`desired_recording_keys` 附近，~L693）
- Modify: `main.py:248-250`
- Modify: `static/js/state.js`（S 物件＋新 helper）
- Modify: `static/js/main.js`（init 的 `/cameras` 消費）
- Test: `tests/test_main.py`

**Interfaces:**
- Produces: `GET /cameras` → `{"cameras": ["cam_01", ...], "active_types": {"cam_01": ["rgb"], ...}}`（`cameras` 維持字串陣列——**向後相容**，既有前端解構不變；spec 的 `{cameras:[{id,...}]}` 僅為示例，此形狀更安全）。
- Produces: `hls_manager.active_types_map(cameras: list[str]) -> dict[str, list[str]]`。
- Produces: `state.js` 匯出 `hasActiveType(cam, type)`：`S.cameraActiveTypes` 無該 cam 資料時回 `true`（**fail-open**：資訊缺失不得誤封鎖實際正常的串流）。

- [ ] **Step 1: 寫失敗測試**（`tests/test_main.py`，加在 `test_cameras_returns_list` 之後）

```python
def test_cameras_includes_active_types(client):
    import time
    import hls_manager as hm
    with patch.dict(hm.hls_manager._last_seen,
                    {("cam_01", "rgb"): time.time(),
                     ("cam_01", "thermal"): time.time() - 9999},
                    clear=True):
        resp = client.get("/cameras")
    assert resp.status_code == 200
    data = resp.json()
    assert data["cameras"] == ["cam_01"]          # 原形狀不變
    assert data["active_types"] == {"cam_01": ["rgb"]}   # thermal 已過期不算活躍
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest -p no:cacheprovider tests/test_main.py::test_cameras_includes_active_types -v`
Expected: FAIL（`KeyError: 'active_types'`）

- [ ] **Step 3: 實作**——`hls_manager.py` 在 `desired_recording_keys` 後加：

```python
    def active_types_map(self, cameras: list[str]) -> dict[str, list[str]]:
        """每台攝影機近期（_RECORDING_SEEN_WINDOW 秒內）有送幀的串流型別。
        供 /cameras 曝露給前端判斷 thermal 是否有來源（無來源 → 無訊號佔位）。"""
        now = time.time()
        return {
            cam: [stype for stype in ("rgb", "thermal")
                  if (seen := self._last_seen.get((cam, stype))) is not None
                  and now - seen <= _RECORDING_SEEN_WINDOW]
            for cam in cameras
        }
```

`main.py` 的 `list_cameras` 改為：

```python
@app.get("/cameras", tags=["system"])
async def list_cameras():
    cameras = [s.label for s in app_settings.zmq_sources]
    return {"cameras": cameras,
            "active_types": hls_manager.active_types_map(cameras)}
```

（`main.py` 已有 `from hls_manager import hls_manager`——確認 import 存在，沒有就補。）

- [ ] **Step 4: 跑測試確認通過＋全套無回歸**

Run: `uv run pytest -p no:cacheprovider tests/test_main.py -v` → 全 PASS
Run: `uv run pytest -p no:cacheprovider -q` → 0 failed

- [ ] **Step 5: 前端消費**——`static/js/state.js` 的 `S` 物件加一行（`trackingCache: new Map(),` 之後）：

```js
  cameraActiveTypes: {}, viewMode: 'single',
```

並在 `els` 定義之前加匯出 helper：

```js
// /cameras 的 active_types 判斷：無該 cam 資料時 fail-open（回 true），
// 資訊缺失不得誤封鎖實際正常的串流。
export function hasActiveType(cam, type) {
  const t = S.cameraActiveTypes[cam];
  return !Array.isArray(t) || t.includes(type);
}
```

`static/js/main.js` 的 `init()` 中 `/cameras` 消費改為：

```js
    const { cameras, active_types } = await res.json();
    S.cameraActiveTypes = active_types || {};
```

（下一行 `if (cameras.length === 0)` 不變。）

- [ ] **Step 6: 驗證＋commit**

Run: `node --check static/js/state.js && node --check static/js/main.js`
```bash
git add hls_manager.py main.py tests/test_main.py static/js/state.js static/js/main.js
git commit -m "feat: /cameras 附帶 active_types（近期送幀的串流型別），前端存入 S.cameraActiveTypes"
```

---

### Task 2: 版面 3a/3b——桌機影片一屏內全可見＋右欄面板等高

**Files:**
- Modify: `static/css/app.css`（`@media (min-width: 1200px)` 區塊 ~L142-148、`#video-wrap` ~L234）

**Interfaces:**
- Consumes: 既有 `--header-h`（main.js ResizeObserver 即時量測）。
- Produces: 無 JS 介面，純 CSS。

- [ ] **Step 1: 3a——`#video-wrap` 高度上限**。在 `@media (min-width: 1200px)` 區塊內加：

```css
      /* 3a: 影片高度上限依視窗高度計——保證影片＋transport＋footer＋整條時間軸
         同屏可見（offset ≈ header 60 + transport 44 + footer 44 + 時間軸區 120
         + layout padding/gap 48 ≈ 316，取 320）。aspect-ratio 讓位給 max-height，
         video 的 object-fit:contain 自動左右黑邊置中。 */
      #video-wrap { max-height: calc(100dvh - 320px); min-height: 240px; }
```

- [ ] **Step 2: 3b——右欄固定高度**。同區塊內，把既有

```css
      .right-col { position: sticky; top: 72px;
                   max-height: calc(100dvh - 88px); display: flex; }
```

改為：

```css
      /* 3b: max-height → height 固定，三分頁共用同一外框高度、內容各自捲動，
         內容少的分頁（通知/書籤）不再縮得比豬隻狀態短。top 改跟 --header-h
         （header 在 1200~1300px 可能折行，寫死 72px 會滑進 header 底下）。 */
      .right-col { position: sticky; top: calc(var(--header-h, 60px) + 12px);
                   height: calc(100dvh - var(--header-h, 60px) - 28px); display: flex; }
      .right-col #bottom-panel { min-height: 0; }
```

（既有 `.right-col #bottom-panel { display:flex; flex-direction:column; width:100%; }` 與 `.right-col .tab-content.active { overflow-y:auto; flex:1; }` 保留不動；`min-height:0` 讓 flex 子項可收縮出捲軸。）

- [ ] **Step 3: headless 驗證（1920×1080）**

啟動 `uv run uvicorn main:app --port 18321`，headless Chrome viewport 1920×1080：
- `#storage-action-bar` 以上（video-card 底、transport、`#timeline-bar` 24 格）全部 `getBoundingClientRect().bottom <= window.innerHeight`，無需捲動。
- 三個分頁輪流 `switchTab`，`#bottom-panel` 的 `offsetHeight` 三者相等。
- console 無 error。

- [ ] **Step 4: commit**

```bash
git add static/css/app.css
git commit -m "fix(frontend): 桌機影片一屏內全可見、右欄三分頁固定等高（3a/3b）"
```

---

### Task 3: 版面 3c——tab-bar 多寬度重疊診斷與修正

**Files:**
- Modify: `static/css/app.css`（sticky/z-index 相關；依診斷結果）
- Test: headless 多寬度截圖掃描

**Interfaces:** 無新介面；不得改動 tab 切換 JS 行為。

- [ ] **Step 1: 重現**。headless Chrome 以寬度 360/480/700/900/1100/1199/1200/1440/1920 逐一載入並捲動，量測：
  - `header` 與 `#tab-bar` 的 `getBoundingClientRect()` 是否相交；
  - `#tab-bar` 與其下方第一列內容是否相交（sticky 蓋內容）；
  - 逐寬度截圖存 scratchpad 供比對。

- [ ] **Step 2: 依根因修正**。已知候選（Task 2 已修 `.right-col top:72px` 這條；其餘依實測）：
  - `<1200px` 的 `#tab-bar` sticky `top: var(--header-h)`：ResizeObserver 首次量測前的 fallback 60px 在已折行 header 下錯位——確認 `syncHeaderHeight()` 於 `DOMContentLoaded` 後即時執行（main.js 已有立即呼叫，驗證即可）。
  - `#tab-bar` sticky 時內容從半透明處透出：`#tab-bar` 已是 `background: var(--surface-2)`（不透明），若截圖顯示透出，檢查是否 `border-radius`/`overflow` 造成圓角縫隙，補 `#bottom-panel { border-radius }` 與 sticky 的視覺銜接（例如 sticky 時 tab-bar 補 `box-shadow: 0 1px 0 var(--border)`）。
  - z-index 疊層：`#tab-bar` z-40 必須 < header z-50、< drawer z-90/91、< `#storage-action-bar` z-80——如衝突調整 z-40 的相對層級，不動其他元件的值。
- 若實測發現根因是 header 折行本身（`--header-h` 跳動），把 `.controls` 在 `<700px` 收斂（`.cam-select` 縮寬 `max-width: 40vw`、隱藏按鈕文字只留圖示），使 header 高度恆定；**只在確認此根因時才做**。

- [ ] **Step 3: 修正後重掃全部寬度**，貼截圖結果進 report；任何寬度下 header/tab-bar/內容三者無相交。

- [ ] **Step 4: commit**

```bash
git add static/css/app.css
git commit -m "fix(frontend): tab-bar 多寬度 sticky 重疊修正（3c，附診斷結果）"
```

---

### Task 4: 單畫面無訊號佔位（thermal 無來源＋VOD 404）

**Files:**
- Modify: `static/index.html`（sprite 加 symbol、`#video-wrap` 加佔位）
- Modify: `static/js/state.js`（els 加參照）
- Modify: `static/js/player.js`（`loadStream`/`loadVod`）
- Modify: `static/css/app.css`（`.no-signal` 樣式）

**Interfaces:**
- Consumes: Task 1 的 `hasActiveType(cam, type)`（自 `state.js` import）。
- Produces: `els.noSignal`；player.js 內部 `showNoSignal(msg)` / `hideNoSignal()`（不匯出）。

- [ ] **Step 1: sprite 加 `i-nosignal` symbol**（`static/index.html` sprite 區，與其他 symbol 並列；video-off 線條風格與現有 stroke 系一致）：

```html
  <symbol id="i-nosignal" viewBox="0 0 24 24" fill="none" stroke="currentColor"
          stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
    <path d="M16 16v1a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V9a2 2 0 0 1 2-2h1"/>
    <path d="M10 7h4a2 2 0 0 1 2 2v2l5-3v8l-2.4-1.44"/>
    <line x1="2" y1="2" x2="22" y2="22"/>
  </symbol>
```

`#video-wrap` 內（`#skeleton` 之後）加：

```html
          <div id="no-signal" class="no-signal" hidden aria-hidden="true">
            <svg class="icon"><use href="#i-nosignal"/></svg>
            <span id="no-signal-text">無訊號</span>
          </div>
```

- [ ] **Step 2: CSS**（`app.css`，放 video 區附近）：

```css
    /* 無訊號佔位：預期狀態（無來源/該時段無錄影），無重試鈕——與「連線錯誤」區分 */
    .no-signal { position: absolute; inset: 0; display: flex; flex-direction: column;
      align-items: center; justify-content: center; gap: var(--space-2);
      background: var(--surface-2); color: var(--text-faint);
      font-size: var(--text-sm); letter-spacing: 0.06em; z-index: 6; }
    .no-signal[hidden] { display: none; }
    .no-signal .icon { width: 40px; height: 40px; }
```

`state.js` 的 `els` 加：

```js
  noSignal:     document.getElementById('no-signal'),
  noSignalText: document.getElementById('no-signal-text'),
```

- [ ] **Step 3: `player.js`**。頂部 import 改為含 `hasActiveType`：

```js
import { S, els, MAX_WS_RETRY, WS_RETRY_BASE_MS, setStatus, showToast, setSkeleton, fmtClock, hasActiveType } from './state.js';
```

加兩個模組內 helper（`loadVod` 之前）：

```js
function showNoSignal(msg) {
  els.noSignalText.textContent = msg;
  els.noSignal.hidden = false;
  setSkeleton(false);
}
function hideNoSignal() { els.noSignal.hidden = true; }
```

`loadStream()`：`els.video.src = '';` 之後、`setSkeleton(true)` 之前插入：

```js
  hideNoSignal();
  // thermal 無來源：後端 /cameras active_types 判定（fail-open）。不建 HLS、不報錯誤 toast。
  if (S.currentType === 'thermal' && !hasActiveType(S.currentCamera, 'thermal')) {
    showNoSignal('無訊號（此攝影機無熱成像來源）');
    setStatus('Thermal 無訊號', '');
    connectWS(S.currentCamera);   // WS 仍照常（bbox 資料與畫面型別無關）
    return;
  }
```

`loadVod()`：`els.video.src = '';` 之後加 `hideNoSignal();`；並把「建 Hls」段改為**先探測 m3u8**（404 → 無訊號）：

```js
  (async () => {
    try {
      const probe = await fetch(vodUrl);
      // 競態守衛：探測期間使用者已回 live 或切了別的時段 → 放棄，
      // 不得在新狀態上覆蓋 S.hls / 佔位。
      if (S.isLive || S.vodStartTs !== startTs) return;
      if (probe.status === 404) {
        showNoSignal('無訊號（此時段無錄影）');
        setStatus('該時段無錄影', '');
        return;
      }
    } catch (_) { /* 探測失敗交給 hls.js 原錯誤路徑 */ }
    if (S.isLive || S.vodStartTs !== startTs) return;
    if (Hls.isSupported()) {
      // …（原本的 new Hls / loadSource(vodUrl) / MANIFEST_PARSED / ERROR /
      //    attachVodListeners() 整段原樣移入此處，縮排調整，一行不改）
    }
  })();
```

（`loadStream` 正常路徑與 `switchToLive`→`loadStream` 都會先 `hideNoSignal()`，佔位不會殘留。）

- [ ] **Step 4: 驗證**

Run: `node --check static/js/player.js && node --check static/js/state.js`
Headless（真實 app）：以 fetch monkeypatch 讓 `/cameras` 回傳某 cam 的 `active_types` 不含 thermal → 切 Thermal 顯示佔位、無 HLS 請求、console 無錯誤；切回 RGB 恢復。VOD：monkeypatch `/stream/*/vod` 回 404 → 佔位顯示「此時段無錄影」。`uv run pytest -p no:cacheprovider -q` → 0 failed。

- [ ] **Step 5: commit**

```bash
git add static/index.html static/js/state.js static/js/player.js static/css/app.css
git commit -m "feat(frontend): 單畫面無訊號佔位——thermal 無來源與 VOD 該時段無錄影"
```

---

### Task 5: grid 跟隨 RGB/Thermal＋tile 無訊號

**Files:**
- Modify: `static/js/grid.js`
- Modify: `static/js/player.js`（`setType` 加 grid 分支）
- Modify: `static/js/main.js`（`S.viewMode` 遷移＋typechange 監聽）
- Modify: `static/css/app.css`（`.grid-tile.nosignal`）

**Interfaces:**
- Consumes: Task 1 `hasActiveType`、`S.cameraActiveTypes`、`S.viewMode`。
- Produces: grid.js 內部 `tileNoSignal(tile, msg)`；player.js `setType` 於 grid 模式 dispatch `document` CustomEvent `'pigagri:grid-type-change'`（player 不 import grid，main.js 監聽後重建）。

- [ ] **Step 1: `main.js` 把模組區域變數 `viewMode` 全面替換為 `S.viewMode`**（宣告 `let viewMode` 刪除；`setViewMode` 開頭 `if (mode === S.viewMode) return; S.viewMode = mode;`；`#view-toggle-btn` handler 改讀 `S.viewMode`）。

- [ ] **Step 2: `player.js` 的 `setType`** 尾段改為：

```js
  if (S.viewMode === 'grid') {
    // grid 模式：單畫面播放器已銷毀，不 loadStream/switchToLive；
    // 由 main.js 監聽此事件重建所有 tile（player 不得 import grid）。
    document.dispatchEvent(new CustomEvent('pigagri:grid-type-change'));
    return;
  }
  if (!S.isLive) {
    switchToLive();
  } else {
    loadStream();
  }
```

`main.js` 加監聽（`bindGridPick` 附近）：

```js
// grid 模式下切 RGB/Thermal：重建所有 tile（enterGrid 內建 _gridGen 守衛與
// /cameras 重抓——順便刷新 active_types）。
document.addEventListener('pigagri:grid-type-change', () => {
  if (S.viewMode === 'grid') enterGrid();
});
```

- [ ] **Step 3: `grid.js`**。import 改為：

```js
import { getJSON } from './api.js';
import { S, hasActiveType } from './state.js';
```

`enterGrid` 的 `/cameras` 消費同步存 active_types：

```js
    let cameras;
    try {
      const data = await getJSON('/cameras');
      cameras = data.cameras;
      S.cameraActiveTypes = data.active_types || {};
    } catch (e) {
```

`buildTile` 的 live URL 改 `?type=${S.currentType}`；在 `root.insertBefore(tile, beforeNode);` 之後、`try {` 之前加：

```js
  // 該型別無來源（如 thermal 攝影機沒裝）：預期狀態，無訊號佔位、不建播放器、無重試鈕。
  if (!hasActiveType(cam, S.currentType)) { tileNoSignal(tile); return; }
```

加 `tileNoSignal`（`tileOffline` 之後）：

```js
// 「無訊號」＝預期狀態（無來源/該時段無錄影）：不給重試鈕，與 tileError（異常）區分。
function tileNoSignal(tile) {
  destroyTilePlayer(tile);
  tileOffline(tile);
  tile.classList.add('nosignal');
}
```

CSS（grid 區塊）：

```css
    .grid-tile.nosignal { cursor: default; }
    .grid-tile.nosignal .tile-live { color: var(--text-faint); }
```

（`.offline` 樣式沿用；`.nosignal` 不附 `.tile-retry`。tile 的點格切換 click 保留——點無訊號格仍可進該攝影機單畫面。）

- [ ] **Step 4: 驗證**

`node --check` 三檔。Headless：grid 模式切 Thermal → 全部 tile 重建為 thermal（真實環境無 thermal 來源的攝影機顯示無訊號、無重試鈕）；切回 RGB 恢復；重建期間快速 toggle 進出 grid 無殘留播放器（`_gridGen` 守衛）；console 無錯誤。pytest 0 failed。

- [ ] **Step 5: commit**

```bash
git add static/js/grid.js static/js/player.js static/js/main.js static/css/app.css
git commit -m "feat(frontend): grid 跟隨 RGB/Thermal 切換，無來源 tile 顯示無訊號"
```

---

### Task 6: grid 時段選擇器＋同步 VOD 回放

**Files:**
- Modify: `static/index.html`（`#grid-view` 前後加回放橫幅與時段列）
- Modify: `static/js/grid.js`（時段狀態、月曆/日列渲染、VOD tile）
- Modify: `static/css/app.css`（grid 時段列、回放橫幅、月曆選擇器共用化）

**Interfaces:**
- Consumes: 既有 `/stream/{cam}/timeline`、`/stream/{cam}/vod`、Task 5 的 `tileNoSignal`。
- Produces: `grid.js` 匯出 `getGridPlaybackHour(): number|null`（null＝LIVE；Task 7 用）；內部 `setGridPlayback(hourTs|null)`。

- [ ] **Step 1: markup**。`static/index.html` 把 `<div id="grid-view" hidden></div>` 區塊改為：

```html
      <div id="grid-playback-banner" hidden>
        <svg class="icon"><use href="#i-play"/></svg>
        <span id="grid-playback-text"></span>
        <button id="grid-live-btn">回到 LIVE</button>
      </div>
      <div id="grid-view" hidden></div>
      <div id="grid-timeline" hidden>
        <div id="grid-timeline-controls">
          <button id="grid-date-btn" aria-haspopup="dialog" aria-expanded="false">
            <svg class="icon"><use href="#i-calendar"/></svg>
            <span id="grid-date-btn-label">—</span>
            <svg class="icon icon-sm"><use href="#i-chev-r" transform="rotate(90 12 12)"/></svg>
          </button>
          <div id="grid-calendar" class="cal-popover" hidden>
            <div class="cal-header">
              <button class="cal-nav-btn" id="grid-prev-month" aria-label="上一月">&#8249;</button>
              <span id="grid-cal-label"></span>
              <button class="cal-nav-btn" id="grid-next-month" aria-label="下一月">&#8250;</button>
            </div>
            <div class="cal-weekdays"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
            <div id="grid-cal-grid" class="cal-grid"></div>
          </div>
        </div>
        <div id="grid-timeline-bar" role="list" aria-label="Grid 回放時段（所有攝影機聯集）"></div>
      </div>
```

- [ ] **Step 2: CSS 月曆選擇器共用化**。`app.css` 把 id 選擇器改為群組（值不變）：

```css
    #calendar-header, .cal-header { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-1); }
    #calendar-weekdays, .cal-weekdays, #calendar-grid, .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
    #calendar-weekdays span, .cal-weekdays span { text-align: center; font-size: 11px; color: var(--text-muted); padding: 2px 0; }
```

（沿用 `.cal-popover`/`.cal-day`/`.cal-nav-btn` 既有 class 樣式。`#calendar-header` 若另有子選擇器規則，同步群組化。）新增 grid 時段列與橫幅樣式：

```css
    /* grid 時段列：沿用 timeline-slot 視覺，但唯讀（無保留/書籤/多選標記） */
    #grid-timeline { display: flex; flex-direction: column; gap: var(--space-2); margin-top: var(--space-3); }
    #grid-timeline[hidden] { display: none; }
    #grid-timeline-controls { position: relative; display: flex; align-items: center; gap: var(--space-2); }
    #grid-date-btn { display: inline-flex; align-items: center; gap: var(--space-2);
      padding: var(--space-1) var(--space-3); background: var(--surface-2);
      border: 1px solid var(--border); border-radius: var(--radius-full); }
    #grid-timeline-bar { display: grid; grid-template-columns: repeat(24, 1fr); gap: 2px; }
    .grid-slot { height: 32px; display: flex; align-items: center; justify-content: center;
      font-size: var(--text-xs); color: var(--text-faint); background: var(--surface-2);
      border-radius: var(--radius-sm); }
    .grid-slot.has-data { color: var(--text-muted); cursor: pointer; }
    .grid-slot.has-data:hover { background: var(--surface-3); color: var(--text); }
    .grid-slot.playing { background: var(--vod-dim); color: var(--vod);
      border: 1px solid var(--vod-border); font-weight: 600; }
    #grid-playback-banner { display: flex; align-items: center; gap: var(--space-2);
      margin-bottom: var(--space-2); padding: var(--space-2) var(--space-4);
      background: var(--vod-dim); color: var(--vod);
      border: 1px solid var(--vod-border); border-radius: var(--radius-lg);
      font-size: var(--text-xs); font-weight: 500; }
    #grid-playback-banner[hidden] { display: none; }
    #grid-live-btn { margin-left: auto; padding: 2px 10px;
      border: 1px solid var(--vod-border); border-radius: var(--radius-full);
      color: var(--vod); background: none; cursor: pointer; }
    #grid-live-btn:hover { background: var(--vod); color: #140d02; } /* 同 vod-banner hover */
    .grid-tile .tile-vod { margin-left: auto; color: var(--vod); font-weight: 600;
      letter-spacing: 0.05em; }
    @media (max-width: 700px) { .grid-slot { height: 40px; } }
```

- [ ] **Step 3: `grid.js` 時段狀態與渲染**。模組頂部加：

```js
// ── grid 時段回放（共用時間軸，所有攝影機聯集）──────────────
let _cams = [];               // enterGrid 抓到的攝影機清單（rebuild 重用）
let _gridHourTs = null;       // null = LIVE；否則為回放小時的 epoch ts
let _gridDay = null;          // 選中日 00:00 epoch
let _gridMonth = null;        // Date（該月 1 日）
let _unionHours = new Set();  // 該月「任一攝影機有錄影」的小時 ts 聯集

function localDayStart(date) {
  const d = new Date(date); d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

export function getGridPlaybackHour() { return _gridHourTs; }
```

`enterGrid` 改為：抓到 cameras 後存 `_cams = cameras;`，初始化時段（僅首次）：

```js
  if (_gridDay === null) {
    const today = new Date();
    _gridDay = localDayStart(today);
    _gridMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  }
```

tiles 迴圈與 badge timer 之後加（並把 `enterGrid` 既有的
`_anomalyTimer = setInterval(...)`＋`refreshTileBadges()` 兩行包進
`if (_gridHourTs === null) { ... }`——回放模式下不輪詢 live badge）：

```js
  document.getElementById('grid-timeline').hidden = false;
  updateGridPlaybackUI();
  loadGridCalendar(gen);   // 非同步：抓齊聯集後 renderGridDayBar()
```

`leaveGrid` 加：

```js
  document.getElementById('grid-timeline').hidden = true;
  document.getElementById('grid-playback-banner').hidden = true;
  setGridCalOpen(false);
```

（`_gridHourTs` **不**重設——同一 session 進出 grid 保留時段；重整後模組重載自然回 LIVE，符合 spec「localStorage 不記時段」。）

- [ ] **Step 4: 聯集月曆與日列**（新函式，全部放 `refreshTileBadges` 之前）：

```js
async function loadGridCalendar(gen) {
  const y = _gridMonth.getFullYear(), m = _gridMonth.getMonth();
  const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
  const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
  const sets = await Promise.all(_cams.map(async cam => {
    try {
      const { hours } = await getJSON(
        `/stream/${cam}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
      return hours;
    } catch (_) { return []; }
  }));
  if (gen !== _gridGen) return;
  _unionHours = new Set(sets.flat());
  renderGridCalendar();
  renderGridDayBar();
}

function gridDayHasData(dayTs) {
  for (let h = 0; h < 24; h++) if (_unionHours.has(dayTs + h * 3600)) return true;
  return false;
}

function renderGridCalendar() {
  const y = _gridMonth.getFullYear(), m = _gridMonth.getMonth();
  document.getElementById('grid-cal-label').textContent = `${y} 年 ${m + 1} 月`;
  const grid = document.getElementById('grid-cal-grid');
  grid.innerHTML = '';
  const firstDow = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  for (let i = 0; i < firstDow; i++) {
    const e = document.createElement('div');
    e.className = 'cal-day empty';
    grid.appendChild(e);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const dayTs = Math.floor(new Date(y, m, d).getTime() / 1000);
    const cell = document.createElement('div');
    cell.className = 'cal-day in-month';
    cell.textContent = d;
    if (gridDayHasData(dayTs)) cell.classList.add('has-rec');
    if (dayTs === _gridDay) cell.classList.add('day-selected');
    cell.addEventListener('click', () => {
      _gridDay = dayTs;
      renderGridCalendar(); renderGridDayBar(); updateGridDateLabel();
      setGridCalOpen(false);
    });
    grid.appendChild(cell);
  }
  const thisMonthFirst = new Date();
  thisMonthFirst.setDate(1); thisMonthFirst.setHours(0, 0, 0, 0);
  document.getElementById('grid-next-month').disabled =
    new Date(y, m, 1) >= thisMonthFirst;
}

function renderGridDayBar() {
  const bar = document.getElementById('grid-timeline-bar');
  bar.innerHTML = '';
  for (let h = 0; h < 24; h++) {
    const slotTs = _gridDay + h * 3600;
    const hasData = _unionHours.has(slotTs);
    const slot = document.createElement('div');
    slot.className = 'grid-slot' + (hasData ? ' has-data' : '');
    slot.setAttribute('role', 'listitem');
    slot.textContent = String(h).padStart(2, '0');
    slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
    if (slotTs === _gridHourTs) slot.classList.add('playing');
    if (hasData) slot.addEventListener('click', () => setGridPlayback(slotTs));
    bar.appendChild(slot);
  }
  updateGridDateLabel();
}

function updateGridDateLabel() {
  const d = new Date(_gridDay * 1000);
  document.getElementById('grid-date-btn-label').textContent =
    `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
}
```

Popover 開闔與月份切換（模組頂層一次性綁定，放檔尾）：

```js
// ── grid 月曆 popover（一次性綁定；元素常駐 DOM，hidden 控制顯示）──────
function setGridCalOpen(open) {
  const pop = document.getElementById('grid-calendar');
  const btn = document.getElementById('grid-date-btn');
  pop.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  if (open) setTimeout(() => document.addEventListener('click', _onGridCalOutside), 0);
  else document.removeEventListener('click', _onGridCalOutside);
}
function _onGridCalOutside(e) {
  const pop = document.getElementById('grid-calendar');
  const btn = document.getElementById('grid-date-btn');
  if (!pop.contains(e.target) && !btn.contains(e.target)) setGridCalOpen(false);
}
document.getElementById('grid-date-btn').addEventListener('click', () => {
  setGridCalOpen(document.getElementById('grid-calendar').hidden);
});
document.getElementById('grid-prev-month').addEventListener('click', () => {
  _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() - 1, 1);
  loadGridCalendar(_gridGen);
});
document.getElementById('grid-next-month').addEventListener('click', () => {
  _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() + 1, 1);
  loadGridCalendar(_gridGen);
});
document.getElementById('grid-live-btn').addEventListener('click', () => setGridPlayback(null));
```

- [ ] **Step 5: 回放切換與 VOD tile**。

```js
// 切回放時段（null = 回 LIVE）：重建所有 tile。badge 輪詢只在 LIVE 跑。
function setGridPlayback(hourTs) {
  if (hourTs === _gridHourTs) return;
  _gridHourTs = hourTs;
  const gen = ++_gridGen;   // 使進行中的 buildTile await 過期
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  for (const cam of _cams) buildTile(root, cam, null, gen);
  if (_gridHourTs === null) {
    _anomalyTimer = setInterval(refreshTileBadges, 30000);
    refreshTileBadges();
  }
  renderGridDayBar();
  updateGridPlaybackUI();
}

function updateGridPlaybackUI() {
  const banner = document.getElementById('grid-playback-banner');
  if (_gridHourTs === null) { banner.hidden = true; return; }
  const dt = new Date(_gridHourTs * 1000);
  const hh = String(dt.getHours()).padStart(2, '0');
  document.getElementById('grid-playback-text').textContent =
    `回放中：${dt.getMonth() + 1}/${dt.getDate()} ${hh}:00–${hh}:59`;
  banner.hidden = false;
}
```

`buildTile` 的 try 區塊改為依模式取 URL（原 live 邏輯保留在 else）：

```js
  try {
    let url;
    if (_gridHourTs !== null) {
      // VOD tile：該攝影機該時段無錄影 → 404 → 無訊號（預期狀態，無重試鈕）
      const vodUrl = `/stream/${cam}/vod?start=${_gridHourTs}&end=${_gridHourTs + 3600}&type=${S.currentType}`;
      const probe = await fetch(vodUrl);
      if (gen !== _gridGen) return;
      if (probe.status === 404) { tileNoSignal(tile); return; }
      if (!probe.ok) throw new Error(`HTTP ${probe.status}`);
      url = vodUrl;
    } else {
      const live = await getJSON(`/stream/${cam}/live?type=${S.currentType}`);
      if (gen !== _gridGen) return;
      url = live.url;
    }
    const video = tile.querySelector('video');
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
                            maxBufferLength: 20 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) tileError(tile, cam); });
      _players.push({ hls, video });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url; video.play().catch(() => {});
      _players.push({ hls: null, video });
    }
  } catch (_) {
```

（catch 與 gen 檢查維持原樣。）tile 資訊列在回放模式改顯示琥珀「回放」：`buildTile` 的 `tile.innerHTML` 之後加：

```js
  if (_gridHourTs !== null) {
    const liveEl = tile.querySelector('.tile-live');
    liveEl.classList.remove('tile-live'); liveEl.classList.add('tile-vod');
    liveEl.textContent = '回放';
    tile.querySelector('.tile-count').textContent = '';
  }
```

（注意：`tileOffline`/`tileError` 內的 `.tile-live` 選擇器要改為 `.tile-live, .tile-vod` 皆可命中：`tile.querySelector('.tile-live, .tile-vod')`。）

- [ ] **Step 6: 驗證**

`node --check static/js/grid.js`。Headless（真實錄影資料）：進 grid → 時段列顯示、聯集 has-data 正確；點有錄影小時 → 全 tile 同步回放、無錄影攝影機 tile 無訊號、橫幅顯示時段、badge 輪詢停止；「回到 LIVE」→ 全 tile 回 live、橫幅消失、badge 恢復；換日期/換月正常；回放中快速 toggle 進出 grid 無殘留播放器；console 無錯誤。pytest 0 failed。

- [ ] **Step 7: commit**

```bash
git add static/index.html static/js/grid.js static/css/app.css
git commit -m "feat(frontend): grid 共用時間軸同步回放——聯集時段列、VOD tile、回到 LIVE"
```

---

### Task 7: 點格帶時段返回單畫面＋整合驗證

**Files:**
- Modify: `static/js/main.js`（`setViewMode` opts、`bindGridPick`）
- Test: 全功能 headless 驗證清單

**Interfaces:**
- Consumes: Task 6 `getGridPlaybackHour()`（grid.js import 清單加入）、既有 `loadVod`（player.js）、`loadTimeline`/`localDayStart`（timeline.js）。

- [ ] **Step 1: `setViewMode` 支援帶時段返回**。簽名改 `async function setViewMode(mode, opts = {})`，single 分支改為：

```js
  } else {
    leaveGrid();
    singleEls.forEach(el => el.hidden = false);
    toggleIcon.setAttribute('href', '#i-grid');
    if (opts.vodStartTs != null) {
      // grid 回放中點格：帶時段直接進該攝影機 VOD，時間軸同步選中該日
      const d = new Date(opts.vodStartTs * 1000);
      S.currentMonth = new Date(d.getFullYear(), d.getMonth(), 1);
      S.selectedDay = localDayStart(d);
      await loadTimeline();
      const slot = els.timelineBar.children[d.getHours()];
      if (slot) {
        document.querySelectorAll('.timeline-slot.selected')
          .forEach(s => s.classList.remove('selected'));
        slot.classList.add('selected');
      }
      loadVod(opts.vodStartTs);
    } else {
      loadStream();
      loadTimeline();
      startLiveTimers();
    }
  }
```

main.js import 加 `loadVod`（自 `./player.js`）與 `getGridPlaybackHour`（自 `./grid.js`）。

`bindGridPick` callback 的 `setViewMode('single');` 改為：

```js
  const hourTs = getGridPlaybackHour();
  setViewMode('single', hourTs != null ? { vodStartTs: hourTs } : {});
```

（`resetCameraState()` 照舊在前；`loadVod` 自身會設定 VOD 狀態/banner/listeners，與 reset 不衝突。）

- [ ] **Step 2: `node --check static/js/main.js`；聚焦驗證**

Headless：grid 回放 HH 時點某格 → 單畫面直接該攝影機 HH 時 VOD（vod-banner 顯示、時間軸該日該時 selected、transport 為 VOD 模式）；「回到直播」正常；grid 在 LIVE 點格 → 行為與現行完全相同（live 載入）；console 無錯誤。

- [ ] **Step 3: 全功能整合驗證清單**（headless，真實部署，唯讀互動）

- 單畫面：live＋bbox、RGB/Thermal（含無來源無訊號）、VOD 回放/PDT/transport/回直播、VOD 404 無訊號。
- Grid：進出、LIVE 監看、badge、時段回放全流程（Task 6 清單）、thermal 切換、點格帶時段返回、點格 LIVE 返回、重整後回 grid LIVE。
- 版面：1920×1080 一屏可見（3a）、三分頁等高（3b）、寬度掃描無重疊（3c 複驗）。
- 時間軸管理功能不回歸：選日/選時/長按多選出現操作列（不實際刪除）、保留/書籤標記顯示。
- `uv run pytest -p no:cacheprovider -q` → 0 failed。

- [ ] **Step 4: commit**

```bash
git add static/js/main.js
git commit -m "feat(frontend): grid 點格帶回放時段返回單畫面，時間軸同步選中"
```
