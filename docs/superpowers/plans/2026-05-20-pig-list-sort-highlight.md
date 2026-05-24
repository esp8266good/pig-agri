# 下方 ID 列排序 + 點選強調/只顯示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓底部 pig 列表可依活動量/體溫/ID 排序，點一列即在影片上強調該豬 bbox（其餘變淡），並可一鍵只顯示該豬——加速採血當天在擁擠畫面定位低活動豬。

**Architecture:** 純前端，僅改 `static/index.html`。新增四個前端狀態
（`selectedObjectId`/`soloMode`/`sortKey`/`sortDir`），改寫 `renderPigStatus()`
做排序與列點選、在 `drawBoxes()` 迴圈加強調/淡化與 solo 過濾、切換時重置選取。
後端零改動，live 與 VOD 共用。

**Tech Stack:** 原生 HTML/CSS/JS（無框架）、hls.js（既有）。無 JS 單元測試框架→
驗證用 `node --check`（抽出 `<script>`）+ 瀏覽器驗收；後端 `uv run pytest` 當回歸閘。

**參考定位（現況行號，實作時以實際內容為準）：**
- `renderPigStatus()`：約 1203；呼叫點 1174 / 1190 / 1387。
- pig table 表頭：約 788（`<tr><th>豬隻</th><th>活動量</th><th>體溫</th></tr>`）。
- `drawBoxes()` 迴圈：約 1496（`for (const o of displayBoxes)`）；`displayBoxes`
  於約 1446 宣告為 `let`，`ctx.lineWidth = 1.5` 於約 1493。
- 狀態宣告區：約 863–875；DOM ref `pigStatusBody` 約 901。
- 切換點：`loadVod`（約 1006）、`switchToLive`（約 1073）、`camSelect` change
  （約 1879）、`setType`（約 1349，走 `loadStream`/`switchToLive`）、`loadStream`
  （約 1585）。

**通用驗證指令（每個 Task 收尾用）：**
```bash
# 1) 抽出所有 <script> 做 JS 語法檢查
python3 - <<'EOF'
import re
html=open('static/index.html').read()
open('/tmp/idx_check.js','w').write("\n".join(re.findall(r'<script>(.*?)</script>', html, re.S)))
EOF
node --check /tmp/idx_check.js && echo JS_OK
```

---

### Task 1: 狀態 + 排序 + 可點欄頭

**Files:**
- Modify: `static/index.html`（狀態宣告區、CSS、pig table 表頭、`renderPigStatus`、init 監聽）

- [ ] **Step 1: 新增四個狀態變數**

在狀態宣告區（約 875，`let currentObjectIds = new Set();` 之後）加入：

```javascript
    let selectedObjectId = null;   // 點選要強調的豬 object_id（null = 未選）
    let soloMode = false;          // 「只顯示選取」開關
    let sortKey = 'activity';      // 'activity' | 'temp' | 'id'
    let sortDir = 1;               // 1 = 升序（活動/溫度低先、ID 小先）；-1 = 降序
```

- [ ] **Step 2: 加可點欄頭的 CSS**

在 pig table 既有樣式區（搜 `#pig-status-table` 或 Bottom panel 樣式段）加入：

```css
    #pig-status-table th.sortable { cursor: pointer; user-select: none; white-space: nowrap; }
    #pig-status-table th.sortable:hover { color: var(--text); }
    #pig-status-table th .sort-ind { font-size: 0.8em; opacity: 0.8; }
```

- [ ] **Step 3: 改 pig table 表頭為可點**

把：
```html
          <tr><th>豬隻</th><th>活動量</th><th>體溫</th></tr>
```
改成：
```html
          <tr>
            <th data-sort="id" class="sortable">豬隻<span class="sort-ind"></span></th>
            <th data-sort="activity" class="sortable">活動量<span class="sort-ind"></span></th>
            <th data-sort="temp" class="sortable">體溫<span class="sort-ind"></span></th>
          </tr>
```

- [ ] **Step 4: 加排序 helper + 方向指示**

在 `renderPigStatus` 之前加入：

```javascript
    // 依 sortKey/sortDir 排序；null（未分析到）值永遠沉底，避免誤導採血。
    function sortPigRows(rows) {
      return rows.sort((a, b) => {
        if (sortKey === 'id') return (a.oid - b.oid) * sortDir;
        const va = sortKey === 'activity' ? a.act : a.temp;
        const vb = sortKey === 'activity' ? b.act : b.temp;
        if (va == null && vb == null) return a.oid - b.oid;
        if (va == null) return 1;
        if (vb == null) return -1;
        return (va - vb) * sortDir;
      });
    }

    function onSortHeaderClick(key) {
      if (sortKey === key) sortDir = -sortDir;
      else { sortKey = key; sortDir = 1; }   // 換欄一律回升序
      renderPigStatus();
    }

    function updateSortIndicators() {
      document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
        const ind = th.querySelector('.sort-ind');
        if (!ind) return;
        ind.textContent = (th.dataset.sort === sortKey) ? (sortDir === 1 ? ' ▲' : ' ▼') : '';
      });
    }
```

- [ ] **Step 5: 改寫 `renderPigStatus` 加排序**

把整個 `renderPigStatus` 函式（約 1203–1229）替換為：

```javascript
    function renderPigStatus() {
      if (!pigStatusBody) return;
      pigStatusBody.innerHTML = '';
      updateSortIndicators();
      if (currentObjectIds.size === 0) {
        pigStatusBody.innerHTML =
          '<tr><td colspan="3" class="pig-empty-msg">目前無偵測到豬隻</td></tr>';
        return;
      }
      const rows = [];
      for (const oid of currentObjectIds) {
        const a = anomalyMap[oid] ?? null;
        rows.push({
          oid,
          act:  a?.activity_current ?? null,
          temp: a?.temp_current ?? null,
          actAnomaly:  a?.activity_anomaly ?? false,
          tempAnomaly: a?.temp_anomaly ?? false,
        });
      }
      sortPigRows(rows);
      for (const r of rows) {
        const actVal  = r.act  != null ? r.act.toFixed(1)  : '—';
        const tempVal = r.temp != null ? r.temp.toFixed(1) : '—';
        const row = document.createElement('tr');
        if (r.actAnomaly || r.tempAnomaly) row.classList.add('anomaly-row');
        row.innerHTML = `
          <td>#${r.oid}</td>
          <td class="${r.actAnomaly ? 'anomaly-cell' : ''}">
            ${r.actAnomaly ? '⚠ ' : ''}${actVal}
          </td>
          <td class="${r.tempAnomaly ? 'anomaly-cell' : ''}">
            ${r.tempAnomaly ? '🌡 ' : ''}${tempVal}
          </td>`;
        pigStatusBody.appendChild(row);
      }
    }
```

- [ ] **Step 6: 在 init 綁欄頭點擊**

在 `camSelect.addEventListener('change', ...)` 區塊之後（約 1903 之後）加入一次性綁定：

```javascript
    document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
      th.addEventListener('click', () => onSortHeaderClick(th.dataset.sort));
    });
```

- [ ] **Step 7: 驗證 + commit**

跑「通用驗證指令」，預期 `JS_OK`。
```bash
git add static/index.html
git commit -m "feat(ui): pig 列表可依活動量/體溫/ID 排序（預設活動量低→高，null 沉底）"
```

---

### Task 2: 點列強調 bbox

**Files:**
- Modify: `static/index.html`（CSS、`renderPigStatus` 列、新增 toggle 函式、`drawBoxes` 迴圈）

- [ ] **Step 1: 加選取列 CSS**

在 pig table 樣式區加入：

```css
    #pig-status-table tr.pig-row { cursor: pointer; }
    #pig-status-table tr.pig-row.selected { background: var(--accent-dim, rgba(80,160,255,0.18)); }
```

- [ ] **Step 2: 加選取切換函式**

在 `renderPigStatus` 之前加入：

```javascript
    function togglePigSelection(oid) {
      selectedObjectId = (selectedObjectId === oid) ? null : oid;
      renderPigStatus();   // 重繪列表反映 .selected；bbox 強調由 drawBoxes 每幀自然反映
    }
```

- [ ] **Step 3: `renderPigStatus` 列加 class/點擊/選取態**

在 Task 1 的 `renderPigStatus` 迴圈內，把 `const row = document.createElement('tr');`
之後、設定 `innerHTML` 之前的列屬性補上（在 `if (r.actAnomaly...)` 那行附近）：

```javascript
        const row = document.createElement('tr');
        row.classList.add('pig-row');
        row.dataset.oid = r.oid;
        if (selectedObjectId === r.oid) row.classList.add('selected');
        if (r.actAnomaly || r.tempAnomaly) row.classList.add('anomaly-row');
        row.addEventListener('click', () => togglePigSelection(r.oid));
```

（其餘 `row.innerHTML = ...` 與 `appendChild` 不變。）

- [ ] **Step 4: `drawBoxes` 迴圈加強調/淡化**

把 `for (const o of displayBoxes) {` 的迴圈本體（約 1496–1527）替換為：

```javascript
      for (const o of displayBoxes) {
        const [x, y, w, h] = o.bbox;
        const px = offX + x * scale;
        const py = offY + y * scale;
        const pw = w * scale;
        const ph = h * scale;
        const anomaly     = anomalyMap[o.object_id];
        const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);
        const color       = isAnomalous ? '#ff4444' : baseColor;

        // 選取強調：選取的框加粗全亮，其餘淡化（selectedObjectId 為 null 時不變）
        const isSel  = selectedObjectId != null && o.object_id === selectedObjectId;
        const dimmed = selectedObjectId != null && !isSel;
        ctx.save();
        if (dimmed) ctx.globalAlpha = 0.25;
        if (isSel)  ctx.lineWidth = 4;

        ctx.strokeStyle = color;
        ctx.fillStyle   = color;
        roundRect(ctx, px, py, pw, ph, 3);
        ctx.stroke();

        const label = `#${o.object_id}`;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = color;
        ctx.fillRect(px - 0.5, py - 16, tw + 6, 15);
        ctx.fillStyle = '#000';
        ctx.fillText(label, px + 2, py - 4);
        ctx.fillStyle = color;

        if (anomaly) {
          let icons = '';
          if (anomaly.activity_anomaly) icons += '⚠';
          if (anomaly.temp_anomaly)     icons += '🌡';
          if (icons) ctx.fillText(icons, px + 2, py + ph - 2);
        }
        ctx.restore();
      }
```

- [ ] **Step 5: 驗證 + commit**

跑「通用驗證指令」，預期 `JS_OK`。
```bash
git add static/index.html
git commit -m "feat(ui): 點 pig 列強調該豬 bbox（加粗亮、其餘淡化）"
```

---

### Task 3: 「只顯示選取」開關

**Files:**
- Modify: `static/index.html`（CSS、tab-pig-status 標記、init 監聽、`drawBoxes` 過濾）

- [ ] **Step 1: 加開關 CSS**

```css
    .solo-toggle { display: flex; align-items: center; gap: 6px; padding: 6px 2px;
                   font-size: 0.85em; color: var(--text-dim, #9aa); cursor: pointer; user-select: none; }
```

- [ ] **Step 2: 在 pig 分頁表格上方加 checkbox**

在 `#tab-pig-status` 的 `<table id="pig-status-table">` 之前插入：

```html
      <label class="solo-toggle">
        <input type="checkbox" id="solo-checkbox"> 只顯示選取的豬
      </label>
```

- [ ] **Step 3: init 綁開關**

在 Task 1 Step 6 的欄頭綁定附近加入：

```javascript
    {
      const soloCb = document.getElementById('solo-checkbox');
      if (soloCb) soloCb.addEventListener('change', e => { soloMode = e.target.checked; });
    }
```

- [ ] **Step 4: `drawBoxes` 加 solo 過濾**

在 `displayBoxes` 已決定之後、`if (!vidW || !vidH || !displayBoxes.length) {` 那行**之前**
（約 1482）插入：

```javascript
      // 只顯示選取：有選取且開關開時，只畫選取的框；無選取則開關不生效（畫全部）。
      if (soloMode && selectedObjectId != null) {
        displayBoxes = displayBoxes.filter(o => o.object_id === selectedObjectId);
      }
```

- [ ] **Step 5: 驗證 + commit**

跑「通用驗證指令」，預期 `JS_OK`。
```bash
git add static/index.html
git commit -m "feat(ui): 新增「只顯示選取的豬」開關（solo）"
```

---

### Task 4: 切換重置 + 最終驗證

**Files:**
- Modify: `static/index.html`（`clearPigSelection`、`loadStream`/`loadVod` 呼叫）

- [ ] **Step 1: 加重置函式**

在 `loadStream` 之前加入：

```javascript
    // 切攝影機 / RGB↔Thermal / Live↔VOD 時清掉選取與 solo（避免殘留別來源的高亮）。
    // sortKey/sortDir 為使用者偏好，不重置。
    function clearPigSelection() {
      selectedObjectId = null;
      soloMode = false;
      const cb = document.getElementById('solo-checkbox');
      if (cb) cb.checked = false;
    }
```

- [ ] **Step 2: 在 `loadStream` 開頭呼叫**

`async function loadStream() {` 的第一行（`if (!currentCamera) return;` 之後）加入：

```javascript
      clearPigSelection();
```
（涵蓋：切攝影機、RGB↔Thermal、Live 重入、初次載入——皆經 `loadStream`。）

- [ ] **Step 3: 在 `loadVod` 開頭呼叫**

`function loadVod(startTs) {` 內（`isLive = false;` 附近、其他清理一起）加入：

```javascript
      clearPigSelection();
```

- [ ] **Step 4: 最終驗證**

```bash
# JS 語法
python3 - <<'EOF'
import re
html=open('static/index.html').read()
open('/tmp/idx_check.js','w').write("\n".join(re.findall(r'<script>(.*?)</script>', html, re.S)))
EOF
node --check /tmp/idx_check.js && echo JS_OK
# 後端回歸閘（4 既有 ZMQ_SOURCES 失敗無關）
uv run pytest -q --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py
```
預期：`JS_OK`、`4 failed, 143 passed`（零新回歸）。

- [ ] **Step 5: commit**

```bash
git add static/index.html
git commit -m "feat(ui): 切攝影機/類型/模式時重置 pig 選取與 solo"
```

- [ ] **Step 6: 瀏覽器驗收清單（交付使用者，現有測試無法涵蓋）**

1. 點「活動量」欄頭 → 列表升序、最低置頂；再點 → 降序；`—` 永遠沉底。
2. 點「體溫」「豬隻」欄頭排序正確、▲▼ 指示正確。
3. 點一列 → 該豬框加粗變亮、其餘變淡；再點同列取消、恢復全亮；強調隨豬移動。
4. 勾「只顯示選取」→ 只剩該豬框；取消恢復全部；未選取時勾選不致空白。
5. 切攝影機 / RGB↔Thermal / Live↔VOD → 選取與 solo 自動重置、無殘留高亮；
   排序偏好保留。
6. 選取的豬被遮擋消失再出現 → 高亮自動恢復。
7. VOD 拖曳時間軸下，排序/選取/只顯示與 live 一致。

---

## Self-Review

**Spec 覆蓋：** §3 狀態→Task1 Step1；§4 排序→Task1；§5 點列強調→Task2；
§6 solo→Task3；§7 重置→Task4；§8 邊界（null 沉底 / 選取以 object_id 記 / 遮擋恢復）
→ Task1 `sortPigRows` null 規則、Task2 以 object_id 比對、Task4 不重置 sort；
§9 測試→各 Task node --check + Task4 pytest + 驗收清單。無遺漏。

**Placeholder 掃描：** 無 TBD/TODO；每段皆含實際程式碼與指令。

**型別/命名一致：** `selectedObjectId`/`soloMode`/`sortKey`/`sortDir`、
`sortPigRows`/`onSortHeaderClick`/`updateSortIndicators`/`togglePigSelection`/
`clearPigSelection`、DOM id `solo-checkbox`、`th.sortable`/`.sort-ind`/`.pig-row`
全計畫一致。`displayBoxes` 為 `let`（可在 Task3 reassign）。
