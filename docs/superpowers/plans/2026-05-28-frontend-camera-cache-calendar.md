# 前端體驗：攝影機 cache + timeline 月曆日期選擇器 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 記住最後瀏覽的攝影機（localStorage），並把擁擠的 168 格週時間軸改成「月曆網格選日期 → 下方當日 24 小時格」，格子變寬易點，保留「哪些日期有錄影」綜觀。

**Architecture:** 純前端（`static/index.html`），後端與測試套件零改動，沿用既有 `GET /stream/{camera_id}/timeline?start_ts&end_ts` 端點查不同範圍。月曆與當日 24 格的 `hour_ts`（3600 倍數）值域與子系統 B 既存 `saved_segments` 完全相容，B 的選取/標記/播放邏輯逐一保留。

**Tech Stack:** vanilla JS（無框架），`node --check` 驗語法。

**測試/驗證備註：** 此子系統無後端改動，Python 測試套件維持綠燈（`uv run pytest`，4 既有 ZMQ_SOURCES 失敗無關，不需重跑亦可）。前端唯一自動 gate 是抽出主 inline `<script>` 做 `node --check`。**抽取指令**（inline `<script>` 起始行會隨 HTML 增長位移，務必動態找）：

```bash
L=$(grep -n "^  <script>" static/index.html | head -1 | cut -d: -f1)
awk -v s="$L" 'NR>s && /<\/script>/{exit} NR>s{print}' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```

**標準慣例：** 在 `master` 直接 commit，**不 push、不開 branch**。只動 `static/index.html`。

---

### Task 1: 最後瀏覽攝影機 cache（localStorage）

**Files:** Modify `static/index.html`（`init()` 與 `camSelect` change handler）

前置：閱讀 `init()`（約 line 2118-2144，含 `currentCamera = cameras[0]; loadStream();`）與 `camSelect.addEventListener('change', ...)`（約 line 2155+，開頭 `currentCamera = camSelect.value;`）。

- [ ] **Step 1: init() 改成讀 cache**

把 `init()` 內這行：

```javascript
        currentCamera = cameras[0];
```

替換為（cache 命中且仍在清單才用，否則退回第一個；localStorage 不可用時 try/catch 退回）：

```javascript
        const cachedCam = (() => { try { return localStorage.getItem('lastCamera'); } catch (_) { return null; } })();
        currentCamera = (cachedCam && cameras.includes(cachedCam)) ? cachedCam : cameras[0];
        camSelect.value = currentCamera;
```

- [ ] **Step 2: camSelect change 時寫入 cache**

在 `camSelect` change handler 開頭的 `currentCamera = camSelect.value;` 之後，緊接加入：

```javascript
      try { localStorage.setItem('lastCamera', currentCamera); } catch (_) {}
```

- [ ] **Step 3: 驗證 JS 語法**

```bash
L=$(grep -n "^  <script>" static/index.html | head -1 | cut -d: -f1)
awk -v s="$L" 'NR>s && /<\/script>/{exit} NR>s{print}' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```
Expected: `JS_OK`。

- [ ] **Step 4: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): localStorage 記住最後瀏覽攝影機"
```

---

### Task 2: timeline 月曆網格 + 當日 24 小時格（取代週視圖）

**Files:** Modify `static/index.html`（HTML、CSS、狀態、timeline JS、init 的 timeline 初始化）

前置閱讀：
- HTML `#timeline-section`（約 line 779-795：`#week-nav` 內含 prev-week/week-label/next-week/live-btn/select-toggle，接 `#timeline-bar`、`#storage-action-bar`）。
- CSS：`#week-nav`/`.week-nav-btn`/`#week-label`/`#live-btn`/`#timeline-bar`/`.timeline-slot*`（約 line 358-419）。
- 狀態變數：`let currentWeekStart`（約 903）；元素參照 `prevWeekBtn`/`nextWeekBtn`/`weekLabelEl`（約 939-941）。
- JS timeline 區：`getWeekStart`/`formatWeekLabel`/`updateWeekNavButtons`/`prevWeek`/`nextWeek`/`loadTimeline`/`renderTimeline`（約 976-1058）、`updateActionBar`/`clearSelection`（約 1060-1071，**保留不動**）、`loadSavedSegments`（約 1073-1083，將被取代）。
- `init()`（約 2132-2136 的 `currentWeekStart = getWeekStart(...); updateWeekNavButtons(); loadTimeline();`）。

子系統 B 的 `selectedHours`/`selectMode`/`savedSegmentsMap`/`updateActionBar`/`clearSelection`/`onRetainClick`/`onBookmarkClick`/`onDeleteRecClick`/`confirmDeleteRecordings` 與 `#storage-action-bar`/`#select-mode-toggle` **保留**；它們只依賴 `hour_ts`（3600 倍數），與 24 格一致。

- [ ] **Step 1: 替換 `#timeline-section` 的 HTML**

把 `<div id="week-nav">...</div>` 整塊（prev-week / week-label / next-week / live-btn / select-toggle，約 line 781-787）替換為月曆 + 控制列：

```html
    <div id="calendar">
      <div id="calendar-header">
        <button class="cal-nav-btn" id="prev-month-btn" onclick="prevMonth()" aria-label="上一月">&#8249;</button>
        <span id="calendar-label"></span>
        <button class="cal-nav-btn" id="next-month-btn" onclick="nextMonth()" aria-label="下一月">&#8250;</button>
      </div>
      <div id="calendar-weekdays"><span>日</span><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span></div>
      <div id="calendar-grid"></div>
    </div>
    <div id="timeline-controls">
      <button id="live-btn" onclick="switchToLive()" style="display:none">● Live</button>
      <label class="select-toggle"><input type="checkbox" id="select-mode-toggle">選取</label>
    </div>
```

`#timeline-bar`（改 `aria-label="所選日期的每小時時間軸"`）與 `#storage-action-bar` 維持在其後不動。

- [ ] **Step 2: 替換 CSS（`#week-nav` 相關 → 月曆）**

把 `#week-nav`、`.week-nav-btn`、`.week-nav-btn:hover...`、`.week-nav-btn:disabled`、`#week-label` 這幾條規則（約 line 358-380）替換為：

```css
    #calendar {
      background: var(--surface-2);
      border-radius: var(--radius-md);
      padding: var(--space-2);
    }
    #calendar-header { display: flex; align-items: center; gap: var(--space-2); margin-bottom: var(--space-1); }
    #calendar-label { flex: 1; text-align: center; font-size: var(--text-sm); color: var(--text); }
    .cal-nav-btn {
      padding: var(--space-1) var(--space-2);
      background: var(--surface-3);
      border: 1px solid var(--border);
      border-radius: var(--radius-sm);
      color: var(--text);
      cursor: pointer;
    }
    .cal-nav-btn:hover:not(:disabled) { background: var(--surface-2); }
    .cal-nav-btn:disabled { opacity: 0.35; cursor: not-allowed; }
    #calendar-weekdays, #calendar-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
    #calendar-weekdays span { text-align: center; font-size: 11px; color: var(--text-muted); padding: 2px 0; }
    .cal-day {
      text-align: center; font-size: 12px; padding: 6px 0; border-radius: 4px;
      color: var(--text-muted); cursor: pointer; position: relative;
    }
    .cal-day.in-month { color: var(--text); }
    .cal-day.empty { visibility: hidden; cursor: default; }
    .cal-day.has-rec::after {
      content: ""; position: absolute; bottom: 3px; left: 50%; transform: translateX(-50%);
      width: 4px; height: 4px; border-radius: 50%; background: var(--accent);
    }
    .cal-day:not(.empty):hover { background: var(--surface-3); }
    .cal-day.day-selected { background: var(--accent); color: #000; font-weight: 600; }
    #timeline-controls { display: flex; align-items: center; gap: var(--space-2); }
```

並把 `#timeline-bar` 的 `height: 24px;` 改為 `height: 30px;`（24 格變寬後略加高、更好點）。

- [ ] **Step 3: 狀態變數 + 元素參照**

3a. 把 `let currentWeekStart = null;`（約 903）替換為：

```javascript
    let currentMonth = null;        // Date：顯示中月份（本地，設為該月 1 號）
    let selectedDay = null;         // 選取日本地午夜的 Unix 秒（3600 倍數）
    let monthHoursSet = new Set();  // 當月有資料的 hour_ts（3600 倍數）
```

3b. 把元素參照 `prevWeekBtn`/`nextWeekBtn`/`weekLabelEl`（約 939-941）替換為：

```javascript
    const calLabelEl     = document.getElementById('calendar-label');
    const calGridEl      = document.getElementById('calendar-grid');
    const prevMonthBtn   = document.getElementById('prev-month-btn');
    const nextMonthBtn   = document.getElementById('next-month-btn');
```

- [ ] **Step 4: 替換 timeline JS（週 → 月曆 + 當日）**

把從 `getWeekStart`（約 976）到 `renderTimeline` 結尾（約 1058）的整段，連同 `loadSavedSegments`（約 1073-1083），全部替換為下列函式。**務必保留中間的 `updateActionBar` 與 `clearSelection` 兩函式不動**（它們在 `renderTimeline` 與 `loadSavedSegments` 之間）。做法：先替換 `getWeekStart`..`renderTimeline` 區段，再單獨把 `loadSavedSegments` 函式替換成 `loadDaySegments`。

4a. `getWeekStart`..`renderTimeline`（約 976-1058）整段替換為：

```javascript
    function localDayStart(date) {
      const d = new Date(date);
      d.setHours(0, 0, 0, 0);
      return Math.floor(d.getTime() / 1000);
    }

    function dayHasData(dayTs) {
      for (let h = 0; h < 24; h++) {
        if (monthHoursSet.has(dayTs + h * 3600)) return true;
      }
      return false;
    }

    async function loadCalendar() {
      monthHoursSet = new Set();
      if (!currentCamera || !currentMonth) return;
      const y = currentMonth.getFullYear(), m = currentMonth.getMonth();
      const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
      const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
      try {
        const resp = await fetch(`/stream/${currentCamera}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
        if (resp.ok) {
          const { hours } = await resp.json();
          hours.forEach(h => monthHoursSet.add(h));
        }
      } catch (_) {}
      renderCalendar();
    }

    function renderCalendar() {
      if (!currentMonth) return;
      const y = currentMonth.getFullYear(), m = currentMonth.getMonth();
      calLabelEl.textContent = `${y} 年 ${m + 1} 月`;
      const firstDow = new Date(y, m, 1).getDay();        // 0=Sun
      const daysInMonth = new Date(y, m + 1, 0).getDate();
      calGridEl.innerHTML = '';
      for (let i = 0; i < firstDow; i++) {
        const e = document.createElement('div');
        e.className = 'cal-day empty';
        calGridEl.appendChild(e);
      }
      for (let d = 1; d <= daysInMonth; d++) {
        const dayTs = Math.floor(new Date(y, m, d).getTime() / 1000);
        const cell = document.createElement('div');
        cell.className = 'cal-day in-month';
        cell.textContent = d;
        if (dayHasData(dayTs)) cell.classList.add('has-rec');
        if (dayTs === selectedDay) cell.classList.add('day-selected');
        cell.addEventListener('click', () => selectDay(dayTs));
        calGridEl.appendChild(cell);
      }
      const thisMonthFirst = new Date();
      thisMonthFirst.setDate(1); thisMonthFirst.setHours(0, 0, 0, 0);
      nextMonthBtn.disabled = new Date(y, m, 1) >= thisMonthFirst;
    }

    function prevMonth() {
      clearSelection();
      currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1);
      loadCalendar();
    }

    function nextMonth() {
      clearSelection();
      currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
      loadCalendar();
    }

    async function selectDay(dayTs) {
      selectedDay = dayTs;
      clearSelection();
      await loadDaySegments();
      renderDayBar();
      renderCalendar();
    }

    function renderDayBar() {
      timelineBar.innerHTML = '';
      if (!selectedDay) return;
      for (let h = 0; h < 24; h++) {
        const slotTs = selectedDay + h * 3600;
        const hasData = monthHoursSet.has(slotTs);
        const slot = document.createElement('div');
        slot.className = 'timeline-slot' + (hasData ? ' has-data' : '');
        slot.setAttribute('role', 'listitem');
        slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
        const seg = savedSegmentsMap.get(slotTs);
        if (seg) slot.classList.add(seg.label ? 'bookmarked' : 'protected');
        if (selectedHours.has(slotTs)) slot.classList.add('slot-selected');
        if (hasData) {
          slot.addEventListener('click', () => {
            if (selectMode) {
              if (selectedHours.has(slotTs)) { selectedHours.delete(slotTs); slot.classList.remove('slot-selected'); }
              else { selectedHours.add(slotTs); slot.classList.add('slot-selected'); }
              updateActionBar();
            } else {
              document.querySelectorAll('.timeline-slot.selected')
                .forEach(s => s.classList.remove('selected'));
              slot.classList.add('selected');
              loadVod(slotTs);
            }
          });
        }
        timelineBar.appendChild(slot);
      }
    }

    async function loadTimeline() {
      if (!currentCamera) return;
      await loadCalendar();
      if (selectedDay) {
        await loadDaySegments();
        renderDayBar();
      }
    }
```

4b. 把 `loadSavedSegments`（約 1073-1083）整個函式替換為 `loadDaySegments`：

```javascript
    async function loadDaySegments() {
      savedSegmentsMap = new Map();
      if (!currentCamera || !selectedDay) return;
      try {
        const resp = await fetch(`/storage/segments?camera_id=${currentCamera}&start_ts=${selectedDay}&end_ts=${selectedDay + 86400}`);
        if (!resp.ok) return;
        const { segments } = await resp.json();
        segments.forEach(s => savedSegmentsMap.set(s.hour_ts, s));
      } catch (_) {}
    }
```

（`updateActionBar`/`clearSelection` 維持原樣於兩者之間。`clearSelection` 內仍 `querySelectorAll('.timeline-slot.slot-selected')`——24 格同樣有此 class，正確。）

- [ ] **Step 5: init() 的 timeline 初始化改月曆**

把 `init()` 內（約 2134-2136）：

```javascript
        currentWeekStart = getWeekStart(new Date());
        updateWeekNavButtons();
        loadTimeline();
```

替換為：

```javascript
        const today = new Date();
        currentMonth = new Date(today.getFullYear(), today.getMonth(), 1);
        selectedDay = localDayStart(today);
        loadTimeline();
```

（Task 1 已在其上方把 `currentCamera` 改成讀 cache + `camSelect.value`，勿動那段。）

- [ ] **Step 6: 驗證 JS 語法**

```bash
L=$(grep -n "^  <script>" static/index.html | head -1 | cut -d: -f1)
awk -v s="$L" 'NR>s && /<\/script>/{exit} NR>s{print}' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```
Expected: `JS_OK`。

- [ ] **Step 7: 確認無殘留週視圖參照**

```bash
grep -n "currentWeekStart\|getWeekStart\|prevWeek\|nextWeek\|updateWeekNavButtons\|formatWeekLabel\|loadSavedSegments\|weekLabelEl\|prevWeekBtn\|nextWeekBtn" static/index.html || echo "NO_WEEK_REFS"
```
Expected: `NO_WEEK_REFS`（週視圖函式/變數/參照已全數移除或取代）。

- [ ] **Step 8: 後端套件未受影響（確認零改動）**

```bash
git status --short
```
Expected: 只有 `static/index.html` 被改（無後端檔案）。

- [ ] **Step 9: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): timeline 改月曆網格選日期 + 當日 24 小時格"
```

---

## Self-Review（撰寫後對照 spec）

- **§3 攝影機 cache（init 讀 + camSelect 寫 + 不在清單退回 + try/catch）** → Task 1。✅
- **§4.1 移除週視圖（week-nav/prevWeek/nextWeek/getWeekStart/168 renderTimeline/loadSavedSegments）** → Task 2 Step 1/4 + Step 7 grep 驗證。✅
- **§4.2 月曆 DOM** → Task 2 Step 1。**§4.3 狀態（currentMonth/selectedDay/monthHoursSet）** → Step 3a。✅
- **§4.4 資料流（loadCalendar/renderCalendar/selectDay/loadDaySegments/renderDayBar/loadTimeline）** → Step 4。✅
- **§4.5 月份導覽（prevMonth/nextMonth、未來月 disabled）** → Step 4 的 `prevMonth`/`nextMonth` + `renderCalendar` 的 `nextMonthBtn.disabled`。✅
- **§5 B 整合（24 格保留 markers/選取/播放；clearSelection 於 prevMonth/nextMonth/selectDay/camSelect；hour_ts 相容）** → Step 4（renderDayBar 保留 B 邏輯、selectDay/prevMonth/nextMonth 含 clearSelection）；camSelect 已有 clearSelection（B 既有，不動）。✅
- **§6 預設今天 + 今天無資料不報錯** → Step 5（selectedDay=今天；renderDayBar 對 has-data 判斷無資料即非 has-data，不報錯）。✅
- **§7 測試（node --check 動態找起始行）** → Task 1 Step 3 / Task 2 Step 6 的指令。✅
- **Placeholder 掃描**：無 TBD；每步含完整程式碼。✅
- **型別/命名一致**：`currentMonth`(Date)/`selectedDay`(number)/`monthHoursSet`(Set)、`loadCalendar`/`renderCalendar`/`selectDay`/`loadDaySegments`/`renderDayBar`/`loadTimeline`/`localDayStart`/`dayHasData`/`prevMonth`/`nextMonth` 跨 step 一致；`calLabelEl`/`calGridEl`/`prevMonthBtn`/`nextMonthBtn` 參照與 HTML id（`calendar-label`/`calendar-grid`/`prev-month-btn`/`next-month-btn`）對應；保留 B 的 `savedSegmentsMap`/`selectedHours`/`selectMode`/`updateActionBar`/`clearSelection`/`loadVod`。✅
- **camSelect 既有 `loadTimeline()` 呼叫** → 現在重載月曆 + 當日格，攝影機切換刷新正確（B 已加的 `clearSelection()` 保留）。✅
