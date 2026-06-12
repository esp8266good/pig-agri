# 書籤/保留/通知 UX 補完 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 補完書籤(備註顯示 + 編輯)、保留(timeline 取消入口)、通知(已讀後從清單移除 + 永久刪除單筆/批量),讓既有功能閉環。

**Architecture:** 後端只加 2 個 alert delete endpoints + 對應 db_writer 函式;前端 `static/index.html` 加書籤編輯 modal、timeline 標記變獨立可點熱區 + popover、通知 toolbar(顯示已讀 toggle + 清空已讀 + 單筆刪除)。`saved_segments` schema 與 `health_alerts` schema 皆不動。

**Tech Stack:** FastAPI、asyncpg、純 vanilla JS/CSS。

---

## File Structure

| File | 動作 | 責任 |
|---|---|---|
| `db_writer.py` | Modify | 加 `delete_alert`、`delete_alerts_bulk` |
| `routers/alerts.py` | Modify | 加 `DELETE /alerts/{id}`、`DELETE /alerts` |
| `tests/test_db_writer.py` | Modify | 覆蓋新函式 |
| `tests/test_alerts_router.py` | Modify(若無則 Create) | 覆蓋新 endpoints |
| `static/index.html` | Modify | UI 全部新增/重寫 |

---

## Task 1:後端 `delete_alert` 與 `delete_alerts_bulk`

**Files:**
- Modify: `db_writer.py`
- Test: `tests/test_db_writer.py`

- [ ] **Step 1: 確認 test_db_writer 既有結構**

Run: `grep -n "mark_alert_read\|query_health_alerts\|asyncpg" tests/test_db_writer.py | head -20`

- [ ] **Step 2: 寫 failing tests**

加到 `tests/test_db_writer.py` 末尾:

```python
@pytest.mark.asyncio
async def test_delete_alert_ok(_pool_with_schema):
    pool = _pool_with_schema
    from db_writer import insert_health_alert, delete_alert
    await insert_health_alert(pool, "cam_01", 1, "activity", 1.0, 2.0, 0.5)
    rows = await pool.fetch("SELECT id FROM health_alerts")
    aid = rows[0]["id"]
    assert await delete_alert(pool, aid) is True
    assert await delete_alert(pool, aid) is False
    assert await pool.fetchval("SELECT COUNT(*) FROM health_alerts") == 0


@pytest.mark.asyncio
async def test_delete_alerts_bulk_read_only_default(_pool_with_schema):
    pool = _pool_with_schema
    from db_writer import insert_health_alert, mark_alert_read, delete_alerts_bulk
    for _ in range(3):
        await insert_health_alert(pool, "cam_01", 1, "activity", 1.0, 2.0, 0.5)
    rows = await pool.fetch("SELECT id FROM health_alerts ORDER BY id")
    await mark_alert_read(pool, rows[0]["id"])
    await mark_alert_read(pool, rows[1]["id"])
    n = await delete_alerts_bulk(pool)  # 預設 read_only=True
    assert n == 2
    assert await pool.fetchval("SELECT COUNT(*) FROM health_alerts") == 1


@pytest.mark.asyncio
async def test_delete_alerts_bulk_camera_filter(_pool_with_schema):
    pool = _pool_with_schema
    from db_writer import insert_health_alert, mark_alert_read, delete_alerts_bulk
    for cam in ["cam_01", "cam_02"]:
        await insert_health_alert(pool, cam, 1, "activity", 1.0, 2.0, 0.5)
    rows = await pool.fetch("SELECT id, camera_id FROM health_alerts")
    for r in rows:
        await mark_alert_read(pool, r["id"])
    n = await delete_alerts_bulk(pool, camera_id="cam_01")
    assert n == 1
    remaining = await pool.fetch("SELECT camera_id FROM health_alerts")
    assert [r["camera_id"] for r in remaining] == ["cam_02"]
```

- [ ] **Step 3: 跑測試確認 fail**

Run: `uv run pytest tests/test_db_writer.py -k delete_alert -x --no-header -q`
Expected: 3 個 fail (ImportError on delete_alert / delete_alerts_bulk)

- [ ] **Step 4: 實作**

加到 `db_writer.py`(放在 `mark_alert_read` 後面):

```python
async def delete_alert(pool: asyncpg.Pool, alert_id: int) -> bool:
    result = await pool.execute("DELETE FROM health_alerts WHERE id = $1", alert_id)
    return result != "DELETE 0"


async def delete_alerts_bulk(
    pool: asyncpg.Pool,
    read_only: bool = True,
    camera_id: Optional[str] = None,
) -> int:
    """批量刪除。預設只刪 is_read=TRUE;可選依攝影機 narrow。
    回傳實際刪除筆數。讀 result 字串末尾數字(asyncpg 慣例)。"""
    conditions: list[str] = []
    params: list = []
    if read_only:
        conditions.append("is_read = TRUE")
    if camera_id is not None:
        conditions.append(f"camera_id = ${len(params) + 1}")
        params.append(camera_id)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    result = await pool.execute(f"DELETE FROM health_alerts {where}", *params)
    return int(result.split()[-1]) if result else 0
```

- [ ] **Step 5: 跑測試確認 pass**

Run: `uv run pytest tests/test_db_writer.py -k delete_alert --no-header -q`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add db_writer.py tests/test_db_writer.py
git commit -m "feat(db): delete_alert + delete_alerts_bulk(預設只刪已讀)"
```

---

## Task 2:`routers/alerts.py` 新增 DELETE endpoints

**Files:**
- Modify: `routers/alerts.py`
- Test: `tests/test_alerts_router.py`(若無則 Create)

- [ ] **Step 1: 檢查 test_alerts_router 是否存在**

Run: `ls tests/test_alerts_router.py 2>&1 || echo "MISSING"`

若 MISSING,參考 `tests/test_storage_router.py` 的 fixture 模式建立。

- [ ] **Step 2: 寫 failing tests**

```python
def test_delete_alert_ok(client):
    with patch("routers.alerts.delete_alert", new_callable=AsyncMock) as m:
        m.return_value = True
        resp = client.delete("/alerts/5")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_delete_alert_404_when_missing(client):
    with patch("routers.alerts.delete_alert", new_callable=AsyncMock) as m:
        m.return_value = False
        resp = client.delete("/alerts/999")
    assert resp.status_code == 404


def test_delete_alerts_bulk_default_read_only(client):
    with patch("routers.alerts.delete_alerts_bulk", new_callable=AsyncMock) as m:
        m.return_value = 7
        resp = client.delete("/alerts")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": 7}
    m.assert_awaited_once()
    kwargs = m.await_args.kwargs
    assert kwargs.get("read_only") is True
    assert kwargs.get("camera_id") is None


def test_delete_alerts_bulk_with_camera(client):
    with patch("routers.alerts.delete_alerts_bulk", new_callable=AsyncMock) as m:
        m.return_value = 3
        resp = client.delete("/alerts?camera_id=cam_01")
    assert resp.status_code == 200
    kwargs = m.await_args.kwargs
    assert kwargs.get("camera_id") == "cam_01"
```

- [ ] **Step 3: 確認 fail**

Run: `uv run pytest tests/test_alerts_router.py -k delete -x --no-header -q`
Expected: 4 fail (405 Method Not Allowed)

- [ ] **Step 4: 實作**

`routers/alerts.py` 加 import + 兩個 endpoints:

```python
from db_writer import (
    delete_alert,
    delete_alerts_bulk,
    mark_alert_read,
    query_health_alerts,
)

# ...既有 routes...

@router.delete("/{alert_id}")
async def delete_one(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    found = await delete_alert(pool, alert_id)
    if not found:
        raise HTTPException(status_code=404, detail="Alert not found")
    return {"ok": True}


@router.delete("")
async def delete_bulk(read_only: bool = True, camera_id: Optional[str] = None):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    n = await delete_alerts_bulk(pool, read_only=read_only, camera_id=camera_id)
    return {"deleted": n}
```

- [ ] **Step 5: 跑測試確認綠**

Run: `uv run pytest tests/test_alerts_router.py --no-header -q`

- [ ] **Step 6: Commit**

```bash
git add routers/alerts.py tests/test_alerts_router.py
git commit -m "feat(alerts): DELETE /alerts/{id} 單筆 + DELETE /alerts 批量(預設只刪已讀)"
```

---

## Task 3:前端通知中心 — toolbar + 已讀後移除 + 單筆/批量刪除

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 找通知 tab 區塊**

Run: `grep -n "tab-notifications\|alert-list\|renderNotifications\|markAlertRead\|refreshNotifications" static/index.html`

- [ ] **Step 2: 在 `#tab-notifications` 頂端加 toolbar**

在 `<div id="tab-notifications" class="tab-content">` 開頭、`<ul id="alert-list">` 之前插入:

```html
<div class="notif-toolbar" style="display:flex;gap:var(--space-3);align-items:center;padding:var(--space-3) var(--space-4);border-bottom:1px solid var(--surface-3)">
  <label style="display:flex;gap:6px;align-items:center;cursor:pointer;font-size:var(--text-xs);color:var(--text-muted)">
    <input type="checkbox" id="show-read-toggle">
    <span>顯示已讀</span>
  </label>
  <button id="clear-read-btn" class="mark-read-btn" style="margin-left:auto" onclick="onClearReadClick()">清空已讀</button>
</div>
```

- [ ] **Step 3: 加狀態變數 + 改 `refreshNotifications`**

在 script 區既有 `let vodAlerts = [];` 附近加:

```javascript
let showReadAlerts = false;  // 「顯示已讀」toggle 狀態
```

並改 `refreshNotifications`:

```javascript
async function refreshNotifications() {
  if (!currentCamera) return;
  try {
    const unread = showReadAlerts ? '' : '&unread_only=true';
    const d = await fetch(`/alerts?camera_id=${currentCamera}&limit=50${unread}`)
      .then(r => r.json());
    renderNotifications(d.alerts || []);
  } catch (_) {}
}
```

並在初始化區註冊 toggle:

```javascript
document.getElementById('show-read-toggle').addEventListener('change', e => {
  showReadAlerts = e.target.checked;
  refreshNotifications();
});
```

- [ ] **Step 4: 改 `renderNotifications` 加「刪除」按鈕**

在現有 `li.innerHTML` template 內,把「標記已讀」按鈕後面追加:

```javascript
const li = document.createElement('li');
li.className = 'alert-item' + (alert.is_read ? '' : ' unread');
li.innerHTML = `
  <div class="alert-info">
    <span class="alert-cam">${alert.camera_id} #${alert.object_id}</span>
    <span class="alert-metric">${metricLabel}</span>
    <span class="alert-time">${dt}</span>
    <span class="alert-sigma">偏差 ${sigma}σ</span>
  </div>
  <button class="mark-read-btn"
          onclick="markAlertRead(${alert.id}, this)"
          ${alert.is_read ? 'disabled' : ''}>
    ${alert.is_read ? '已讀' : '標記已讀'}
  </button>
  <button class="mark-read-btn" onclick="onDeleteAlertClick(${alert.id}, this)">刪除</button>`;
```

- [ ] **Step 5: 改 `markAlertRead`:成功後若 `!showReadAlerts` 則從 DOM 移除**

```javascript
async function markAlertRead(alertId, btn) {
  try {
    const resp = await fetch(`/alerts/${alertId}/read`, { method: 'PUT' });
    if (!resp.ok) return;
    const li = btn.closest('.alert-item');
    if (!showReadAlerts) {
      li.remove();
      // 若清單空了補一行
      if (alertListEl.children.length === 0) {
        alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
      }
    } else {
      btn.textContent = '已讀';
      btn.disabled = true;
      li.classList.remove('unread');
    }
    // bell badge:重抓未讀數
    const d = await fetch('/alerts?unread_only=true').then(r => r.json());
    const n = (d.alerts || []).length;
    bellBadge.textContent = n;
    bellBadge.style.display = n > 0 ? '' : 'none';
  } catch (_) {}
}
```

- [ ] **Step 6: 加 `onDeleteAlertClick` 與 `onClearReadClick`**

```javascript
async function onDeleteAlertClick(alertId, btn) {
  if (!confirm('永久刪除此警示記錄?')) return;
  try {
    const resp = await fetch(`/alerts/${alertId}`, { method: 'DELETE' });
    if (!resp.ok) return;
    btn.closest('.alert-item').remove();
    if (alertListEl.children.length === 0) {
      alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
    }
    // 若該筆未讀,badge 也要 -1
    const d = await fetch('/alerts?unread_only=true').then(r => r.json());
    const n = (d.alerts || []).length;
    bellBadge.textContent = n;
    bellBadge.style.display = n > 0 ? '' : 'none';
  } catch (_) { alert('刪除失敗'); }
}

async function onClearReadClick() {
  if (!confirm(`清空 ${currentCamera || '此攝影機'} 所有已讀警示?`)) return;
  try {
    const url = currentCamera
      ? `/alerts?read_only=true&camera_id=${currentCamera}`
      : '/alerts?read_only=true';
    const resp = await fetch(url, { method: 'DELETE' });
    if (!resp.ok) return;
    const { deleted } = await resp.json();
    if (deleted === 0) alert('沒有已讀可清除');
    refreshNotifications();
  } catch (_) { alert('清空失敗'); }
}
```

- [ ] **Step 7: 抽出 inline script 跑 `node --check`**

```bash
L=$(grep -n "^  <script>" static/index.html | head -1 | cut -d: -f1)
awk -v s="$L" 'NR>s && /<\/script>/{exit} NR>s{print}' static/index.html > /tmp/_idx.js
node --check /tmp/_idx.js
```
Expected: 無錯誤輸出

- [ ] **Step 8: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 通知中心 顯示已讀toggle + 單筆/批量永久刪除 + 已讀立即從清單移除"
```

---

## Task 4:前端書籤編輯 — modal + 列表顯示 note + 編輯按鈕

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 找書籤列表渲染處**

Run: `grep -n "loadBookmarks\|bookmark-list\|onBookmarkClick" static/index.html`

- [ ] **Step 2: 新增 bookmark-edit modal DOM**

在 `#delete-modal` 旁邊加(沿用相同 CSS 類):

```html
<div id="bookmark-edit-modal" class="modal-overlay" style="display:none">
  <div class="modal-content">
    <div class="modal-title">編輯書籤</div>
    <div style="display:flex;flex-direction:column;gap:var(--space-3);padding:var(--space-3) 0">
      <label style="display:flex;flex-direction:column;gap:4px;font-size:var(--text-xs)">
        <span>名稱</span>
        <input id="bookmark-edit-label" type="text" maxlength="100"
               style="padding:6px 8px;background:var(--surface-2);border:1px solid var(--surface-3);color:var(--text);border-radius:4px">
      </label>
      <label style="display:flex;flex-direction:column;gap:4px;font-size:var(--text-xs)">
        <span>備註</span>
        <textarea id="bookmark-edit-note" rows="3" maxlength="500"
                  style="padding:6px 8px;background:var(--surface-2);border:1px solid var(--surface-3);color:var(--text);border-radius:4px;font-family:inherit;resize:vertical"></textarea>
      </label>
    </div>
    <div class="modal-actions">
      <button onclick="closeBookmarkEditModal()">取消</button>
      <button id="bookmark-edit-save-btn" onclick="saveBookmarkEdit()">儲存</button>
    </div>
  </div>
</div>
```

(若既有 modal CSS 類名不同,改用對應的;檢查 `#delete-modal` 樣式即可)

- [ ] **Step 3: 改 `loadBookmarks` 渲染 note + 加編輯按鈕**

```javascript
async function loadBookmarks() {
  const ul = document.getElementById('bookmark-list');
  if (!ul || !currentCamera) return;
  try {
    const resp = await fetch(`/storage/bookmarks?camera_id=${currentCamera}`);
    if (!resp.ok) return;
    const { bookmarks } = await resp.json();
    ul.innerHTML = '';
    if (bookmarks.length === 0) {
      ul.innerHTML = '<li style="opacity:.6">尚無書籤</li>';
      return;
    }
    bookmarks.forEach(b => {
      const li = document.createElement('li');
      li.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding:8px 0;border-bottom:1px solid var(--surface-3)';
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:8px;align-items:center';
      const when = new Date(b.hour_ts * 1000).toLocaleString('zh-TW',
        { month: '2-digit', day: '2-digit', hour: '2-digit' });
      const link = document.createElement('a');
      link.href = '#'; link.textContent = `★ ${b.label}`;
      link.style.cssText = 'color:var(--accent);text-decoration:none;flex:1';
      link.onclick = (e) => { e.preventDefault(); loadVod(b.hour_ts); };
      const time = document.createElement('span');
      time.textContent = when; time.style.cssText = 'opacity:.7;font-size:12px';
      const edit = document.createElement('button');
      edit.textContent = '編輯'; edit.style.cssText = 'padding:2px 8px;cursor:pointer';
      edit.onclick = () => openBookmarkEditModal(b);
      const del = document.createElement('button');
      del.textContent = '移除'; del.style.cssText = 'padding:2px 8px;cursor:pointer';
      del.onclick = async () => {
        if (!confirm(`移除書籤「${b.label}」?(不刪影片)`)) return;
        await fetch(`/storage/segments/${b.id}`, { method: 'DELETE' });
        loadBookmarks(); loadTimeline();
      };
      row.append(link, time, edit, del);
      li.appendChild(row);
      if (b.note) {
        const noteDiv = document.createElement('div');
        noteDiv.textContent = b.note;
        noteDiv.style.cssText = 'font-size:12px;opacity:.7;padding-left:8px;white-space:pre-wrap';
        li.appendChild(noteDiv);
      }
      ul.appendChild(li);
    });
  } catch (_) {}
}
```

- [ ] **Step 4: 加 modal 操作函式**

```javascript
let _editingBookmarkId = null;

function openBookmarkEditModal(seg) {
  _editingBookmarkId = seg.id;
  document.getElementById('bookmark-edit-label').value = seg.label || '';
  document.getElementById('bookmark-edit-note').value = seg.note || '';
  document.getElementById('bookmark-edit-modal').style.display = '';
}

function closeBookmarkEditModal() {
  _editingBookmarkId = null;
  document.getElementById('bookmark-edit-modal').style.display = 'none';
}

async function saveBookmarkEdit() {
  if (_editingBookmarkId === null) return;
  const label = document.getElementById('bookmark-edit-label').value.trim();
  const note = document.getElementById('bookmark-edit-note').value.trim() || null;
  if (!label) { alert('名稱不可空白'); return; }
  try {
    const resp = await fetch(`/storage/segments/${_editingBookmarkId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, note }),
    });
    if (!resp.ok) throw new Error();
    closeBookmarkEditModal();
    await loadBookmarks();
    await loadTimeline();  // savedSegmentsMap 也需更新(label 可能變)
  } catch (_) { alert('儲存失敗'); }
}
```

- [ ] **Step 5: 切換攝影機/Live↔VOD 時關閉 modal**

找 `closeDeleteModal` 的呼叫點(子系統 B 已加),沿用同樣的位置加 `closeBookmarkEditModal()`:在 `camSelect` change handler、`loadStream` 開頭、`loadVod` 開頭。

- [ ] **Step 6: `node --check`**

(同 Task 3 Step 7)

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): 書籤列表顯示備註 + 編輯modal(改名+改備註)"
```

---

## Task 5:前端 timeline 標記 — 獨立可點熱區 + popover

**Files:**
- Modify: `static/index.html`

- [ ] **Step 1: 找 timeline-slot 渲染與 CSS**

Run: `grep -n "bookmarked\|protected\|timeline-slot\|renderDayBar" static/index.html`

- [ ] **Step 2: 移除舊 pseudo-element marker**

刪除這兩行 CSS(類似結構,若有變動以實際為準):

```css
.timeline-slot.bookmarked::after { content: "★"; ... }
.timeline-slot.protected::after { content: "🔒"; ... }
```

並加上新 marker 樣式:

```css
.slot-marker {
  position: absolute;
  top: -2px;
  right: -2px;
  min-width: 28px;
  min-height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  background: transparent;
  border: none;
  cursor: pointer;
  font-size: 16px;
  line-height: 1;
  padding: 4px;
  touch-action: manipulation;
  z-index: 2;
}
.slot-marker:hover { background: rgba(255,255,255,.08); border-radius: 4px; }
.timeline-slot { position: relative; }
```

(`#timeline-bar` 結構需確保 slot 父為 `position: relative` — 加在 `.timeline-slot` 規則即可)

- [ ] **Step 3: 改 `renderDayBar` 把標記變成真正的 `<button>` 子元素**

找 `renderDayBar` 內逐 slot 處理的迴圈,把加 `.bookmarked`/`.protected` class 的位置改成 append button:

```javascript
// 既有:slot.classList.add(seg.label ? 'bookmarked' : 'protected');
// 改成:
const seg = savedSegmentsMap.get(slotTs);
if (seg) {
  slot.classList.add(seg.label ? 'bookmarked' : 'protected');
  const marker = document.createElement('button');
  marker.className = 'slot-marker';
  marker.textContent = seg.label ? '★' : '🔒';
  marker.title = seg.label ? `書籤:${seg.label}` : '保留中';
  marker.onclick = (e) => {
    e.stopPropagation();
    openSlotActionMenu(e.currentTarget, seg);
  };
  slot.appendChild(marker);
}
```

(注意:既有 `.bookmarked`/`.protected` class 仍保留,可能用於背景色 hint)

- [ ] **Step 4: 加 `openSlotActionMenu` popover**

```javascript
let _slotActionMenuEl = null;

function closeSlotActionMenu() {
  if (_slotActionMenuEl) {
    _slotActionMenuEl.remove();
    _slotActionMenuEl = null;
    document.removeEventListener('click', _onSlotMenuOutside);
  }
}

function _onSlotMenuOutside(e) {
  if (_slotActionMenuEl && !_slotActionMenuEl.contains(e.target)) {
    closeSlotActionMenu();
  }
}

function openSlotActionMenu(anchor, seg) {
  closeSlotActionMenu();
  const menu = document.createElement('div');
  menu.className = 'slot-action-menu';
  menu.style.cssText = `
    position: absolute; background: var(--surface-1);
    border: 1px solid var(--surface-3); border-radius: 6px;
    padding: 4px; box-shadow: 0 4px 12px rgba(0,0,0,.4);
    z-index: 100; display: flex; flex-direction: column; gap: 2px;
    min-width: 140px;
  `;
  const mkBtn = (label, onClick) => {
    const b = document.createElement('button');
    b.textContent = label;
    b.style.cssText = 'background:transparent;border:none;color:var(--text);padding:8px 12px;cursor:pointer;text-align:left;font-size:var(--text-xs);border-radius:4px';
    b.onmouseenter = () => b.style.background = 'var(--surface-2)';
    b.onmouseleave = () => b.style.background = 'transparent';
    b.onclick = (e) => { e.stopPropagation(); closeSlotActionMenu(); onClick(); };
    return b;
  };
  if (seg.label) {
    menu.appendChild(mkBtn('編輯書籤', () => openBookmarkEditModal(seg)));
    menu.appendChild(mkBtn('取消書籤', () => onUnmarkSlot(seg, '取消書籤')));
  } else {
    menu.appendChild(mkBtn('取消保留', () => onUnmarkSlot(seg, '取消保留')));
  }
  menu.appendChild(mkBtn('關閉', () => {}));

  // 位置:錨點下方,若超出視窗右緣則靠右對齊
  const rect = anchor.getBoundingClientRect();
  document.body.appendChild(menu);
  const mw = menu.offsetWidth;
  let left = rect.left + window.scrollX;
  if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
  menu.style.left = `${left}px`;
  menu.style.top = `${rect.bottom + window.scrollY + 4}px`;

  _slotActionMenuEl = menu;
  // 延遲掛 listener,避免本次 click 立刻被當「outside」
  setTimeout(() => document.addEventListener('click', _onSlotMenuOutside), 0);
}

async function onUnmarkSlot(seg, label) {
  if (!confirm(`${label}此小時?(不刪影片)`)) return;
  try {
    const resp = await fetch(`/storage/segments/${seg.id}`, { method: 'DELETE' });
    if (!resp.ok) throw new Error();
    await loadTimeline();
    await loadBookmarks();
  } catch (_) { alert('操作失敗'); }
}
```

- [ ] **Step 5: 切換攝影機/月/日 / Live↔VOD 時關閉 popover**

在以下既有重置點加上 `closeSlotActionMenu()`:`prevMonth`、`nextMonth`、`selectDay`、`camSelect` change、`loadStream` 開頭、`loadVod` 開頭。

- [ ] **Step 6: `node --check`**

(同 Task 3 Step 7)

- [ ] **Step 7: Commit**

```bash
git add static/index.html
git commit -m "feat(ui): timeline 標記變獨立可點熱區(28x28) + popover 取消保留/編輯書籤"
```

---

## Task 6:全套件驗證 + CLAUDE.md

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: 跑全套件**

Run: `uv run pytest --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py --no-header -q 2>&1 | tail -10`
Expected: 新增測試全綠;既有失敗集合不擴大(待辦 #12 那 4 個可保留)

- [ ] **Step 2: 更新 CLAUDE.md**

在現有「子系統 C」段後追加「子系統 D:書籤/保留/通知 UX 補完(2026-06-13)」段,記錄:
- 新 endpoint(`DELETE /alerts/{id}`、`DELETE /alerts`)+ `delete_alert`/`delete_alerts_bulk`
- 前端 modal 編輯書籤、`.slot-marker` button(28×28 hit area,popover)、通知 toolbar
- `health_alerts` 自動 retention 仍為待辦(YAGNI;批量清除已涵蓋實際需求)

- [ ] **Step 3: 最終 commit**

```bash
git add CLAUDE.md
git commit -m "docs(claude.md): 子系統D 書籤/保留/通知 UX 補完"
```

---

## Self-Review 檢查清單(寫完 plan 自己跑一次)

- **Spec 覆蓋率**:#2-A 編輯(Task 4 Modal)、#2-B 備註顯示(Task 4 Step 3)、#2-C 取消保留(Task 5)、#3-A 已讀後移除(Task 3 Step 5)、#3-B 顯示已讀 toggle(Task 3 Step 3)、#3-C 永久刪除單筆+批量(Task 1+2+3)。✓ 全覆蓋。
- **Placeholder**:無 TODO/TBD;每步驟有具體 code 或指令。
- **Type consistency**:`saveBookmarkEdit` 用 `_editingBookmarkId`;`openBookmarkEditModal` 設它;`closeBookmarkEditModal` 清它。Action menu `_slotActionMenuEl` 由 `closeSlotActionMenu` 統一管理。一致。
