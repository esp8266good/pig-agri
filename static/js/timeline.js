// static/js/timeline.js — 月曆／每小時時間軸／保留・書籤・刪除操作。
import { S, els, syncBottomInset } from './state.js';
import { loadVod } from './player.js';
import { openBookmarkEditModal, loadBookmarks } from './panels.js';

// ── Timeline helpers ──────────────────────────────────────
export function localDayStart(date) {
  const d = new Date(date);
  d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

function dayHasData(dayTs) {
  for (let h = 0; h < 24; h++) {
    if (S.monthHoursSet.has(dayTs + h * 3600)) return true;
  }
  return false;
}

async function loadCalendar() {
  S.monthHoursSet = new Set();
  if (!S.currentCamera || !S.currentMonth) return;
  const y = S.currentMonth.getFullYear(), m = S.currentMonth.getMonth();
  const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
  const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
  try {
    const resp = await fetch(`/stream/${S.currentCamera}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
    if (resp.ok) {
      const { hours } = await resp.json();
      hours.forEach(h => S.monthHoursSet.add(h));
    }
  } catch (_) {}
  renderCalendar();
}

function renderCalendar() {
  if (!S.currentMonth) return;
  const y = S.currentMonth.getFullYear(), m = S.currentMonth.getMonth();
  els.calLabelEl.textContent = `${y} 年 ${m + 1} 月`;
  const firstDow = new Date(y, m, 1).getDay();        // 0=Sun
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  els.calGridEl.innerHTML = '';
  for (let i = 0; i < firstDow; i++) {
    const e = document.createElement('div');
    e.className = 'cal-day empty';
    els.calGridEl.appendChild(e);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const dayTs = Math.floor(new Date(y, m, d).getTime() / 1000);
    const cell = document.createElement('div');
    cell.className = 'cal-day in-month';
    cell.textContent = d;
    if (dayHasData(dayTs)) cell.classList.add('has-rec');
    if (dayTs === S.selectedDay) cell.classList.add('day-selected');
    cell.addEventListener('click', () => selectDay(dayTs));
    els.calGridEl.appendChild(cell);
  }
  const thisMonthFirst = new Date();
  thisMonthFirst.setDate(1); thisMonthFirst.setHours(0, 0, 0, 0);
  els.nextMonthBtn.disabled = new Date(y, m, 1) >= thisMonthFirst;
}

// ── 日期按鈕 popover ───────────────────────────────────────
const dateBtnEl = document.getElementById('date-btn');
const dateBtnLabelEl = document.getElementById('date-btn-label');
const calendarPopoverEl = document.getElementById('calendar');

function setCalendarOpen(open) {
  calendarPopoverEl.hidden = !open;
  dateBtnEl.setAttribute('aria-expanded', String(open));
  if (open) {
    setTimeout(() => document.addEventListener('click', _onCalendarOutside), 0);
  } else {
    document.removeEventListener('click', _onCalendarOutside);
  }
}
function _onCalendarOutside(e) {
  if (!calendarPopoverEl.contains(e.target) && !dateBtnEl.contains(e.target)) {
    setCalendarOpen(false);
  }
}
dateBtnEl.addEventListener('click', () => setCalendarOpen(calendarPopoverEl.hidden));

function updateDateBtnLabel() {
  if (!S.selectedDay) return;
  const d = new Date(S.selectedDay * 1000);
  dateBtnLabelEl.textContent =
    `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
}

function prevMonth() {
  clearSelection();
  if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
  S.currentMonth = new Date(S.currentMonth.getFullYear(), S.currentMonth.getMonth() - 1, 1);
  loadCalendar();
}

function nextMonth() {
  clearSelection();
  if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
  S.currentMonth = new Date(S.currentMonth.getFullYear(), S.currentMonth.getMonth() + 1, 1);
  loadCalendar();
}

export async function selectDay(dayTs) {
  S.selectedDay = dayTs;
  clearSelection();
  if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
  await loadDaySegments();
  renderDayBar();
  renderCalendar();
  updateDateBtnLabel();
  setCalendarOpen(false);
}

let _longPressTimer = null;

function enterSelectMode() {
  S.selectMode = true;
  els.timelineBar.classList.add('selecting');
  updateActionBar();
}
function exitSelectMode() {
  S.selectMode = false;
  els.timelineBar.classList.remove('selecting');
}
function toggleHourSelection(slot, hourTs) {
  if (S.selectedHours.has(hourTs)) {
    S.selectedHours.delete(hourTs);
    slot.classList.remove('slot-selected');
  } else {
    S.selectedHours.add(hourTs);
    slot.classList.add('slot-selected');
  }
  if (S.selectedHours.size === 0) exitSelectMode();
  updateActionBar();
}

function renderDayBar() {
  clearTimeout(_longPressTimer);
  _longPressTimer = null;
  els.timelineBar.innerHTML = '';
  if (!S.selectedDay) return;
  for (let h = 0; h < 24; h++) {
    const slotTs = S.selectedDay + h * 3600;
    const hasData = S.monthHoursSet.has(slotTs);
    const slot = document.createElement('div');
    slot.className = 'timeline-slot' + (hasData ? ' has-data' : '');
    slot.setAttribute('role', 'listitem');
    slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
    slot.textContent = String(h).padStart(2, '0');
    const seg = S.savedSegmentsMap.get(slotTs);
    if (seg) {
      slot.classList.add(seg.label ? 'bookmarked' : 'protected');
      const marker = document.createElement('button');
      marker.className = 'slot-marker' + (seg.label ? '' : ' protected-marker');
      marker.textContent = seg.label ? '★' : '🔒';
      marker.title = seg.label ? `書籤:${seg.label}(點擊管理)` : '保留中(點擊管理)';
      marker.addEventListener('pointerdown', (e) => e.stopPropagation());
      marker.onclick = (e) => {
        e.stopPropagation();
        openSlotActionMenu(marker, seg);
      };
      slot.appendChild(marker);
    }
    if (S.selectedHours.has(slotTs)) slot.classList.add('slot-selected');
    if (hasData) {
      slot.addEventListener('pointerdown', () => {
        _longPressTimer = setTimeout(() => {
          _longPressTimer = null;
          if (!slot.isConnected) return; // DOM 已重建,此 slot 已是遊魂節點
          enterSelectMode();
          toggleHourSelection(slot, slotTs);
        }, 500);
      });
      slot.addEventListener('pointerup', (e) => {
        if (_longPressTimer === null) return; // 長按已觸發,此次不當 click
        clearTimeout(_longPressTimer);
        _longPressTimer = null;
        if (e.shiftKey && !S.selectMode) enterSelectMode();
        if (S.selectMode) {
          toggleHourSelection(slot, slotTs);
        } else {
          document.querySelectorAll('.timeline-slot.selected')
            .forEach(s => s.classList.remove('selected'));
          slot.classList.add('selected');
          loadVod(slotTs);
        }
      });
      slot.addEventListener('pointerleave', () => {
        clearTimeout(_longPressTimer);
        _longPressTimer = null;
      });
      slot.addEventListener('pointercancel', () => {
        clearTimeout(_longPressTimer);
        _longPressTimer = null;
      });
    }
    els.timelineBar.appendChild(slot);
  }
}

let _slotActionMenuEl = null;
export function closeSlotActionMenu() {
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
  const mkBtn = (label, onClick) => {
    const b = document.createElement('button');
    b.textContent = label;
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
  document.body.appendChild(menu);
  const rect = anchor.getBoundingClientRect();
  const mw = menu.offsetWidth;
  let left = rect.left + window.scrollX;
  if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
  if (left < 8) left = 8;
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
    if (typeof loadBookmarks === 'function') await loadBookmarks();
  } catch (_) { alert('操作失敗'); }
}

export async function loadTimeline() {
  if (!S.currentCamera) return;
  await loadCalendar();
  if (S.selectedDay) {
    await loadDaySegments();
    renderDayBar();
    updateDateBtnLabel();
  }
}

function updateActionBar() {
  const bar = document.getElementById('storage-action-bar');
  document.getElementById('storage-sel-count').textContent = `已選 ${S.selectedHours.size} 小時`;
  bar.classList.toggle('visible', S.selectMode && S.selectedHours.size > 0);
  syncBottomInset();   // 操作列是 fixed 的，不墊出空間就會蓋住頁面最後一段
}

export function clearSelection() {
  clearTimeout(_longPressTimer);   // 清掉 selectDay await 期間過期計時器的殘餘 race
  _longPressTimer = null;
  S.selectedHours.clear();
  document.querySelectorAll('.timeline-slot.slot-selected')
    .forEach(s => s.classList.remove('slot-selected'));
  updateActionBar();
  exitSelectMode();
}

async function loadDaySegments() {
  S.savedSegmentsMap = new Map();
  if (!S.currentCamera || !S.selectedDay) return;
  try {
    const resp = await fetch(`/storage/segments?camera_id=${S.currentCamera}&start_ts=${S.selectedDay}&end_ts=${S.selectedDay + 86400}`);
    if (!resp.ok) return;
    const { segments } = await resp.json();
    segments.forEach(s => S.savedSegmentsMap.set(s.hour_ts, s));
  } catch (_) {}
}

async function onRetainClick() {
  if (S.selectedHours.size === 0) return;
  const hours = [...S.selectedHours];
  try {
    const resp = await fetch('/storage/segments', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: S.currentCamera, hours }),
    });
    if (!resp.ok) throw new Error();
    clearSelection();
    await loadTimeline();
  } catch (_) { alert('保留失敗'); }
}

async function onBookmarkClick() {
  if (S.selectedHours.size === 0) return;
  const label = prompt('書籤名稱：');
  if (label === null || label.trim() === '') return;
  const note = prompt('備註（可留空）：') || null;
  const hours = [...S.selectedHours];
  try {
    const resp = await fetch('/storage/segments', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: S.currentCamera, hours, label: label.trim(), note }),
    });
    if (!resp.ok) throw new Error();
    clearSelection();
    await loadTimeline();
    if (typeof loadBookmarks === 'function') loadBookmarks();
  } catch (_) { alert('書籤失敗'); }
}

function onDeleteRecClick() {
  if (S.selectedHours.size === 0) return;
  const hours = [...S.selectedHours].sort((a, b) => a - b);
  const fmt = ts => new Date(ts * 1000).toLocaleString('zh-TW',
    { month: '2-digit', day: '2-digit', hour: '2-digit' });
  document.getElementById('delete-modal-summary').textContent =
    `將刪除 ${hours.length} 個小時：${hours.map(fmt).join('、')}`;
  const protectedSel = hours.filter(h => S.savedSegmentsMap.has(h));
  const warn = document.getElementById('delete-modal-warn');
  const check = document.getElementById('delete-confirm-check');
  const btn = document.getElementById('delete-confirm-btn');
  if (protectedSel.length > 0) {
    const ul = document.getElementById('delete-modal-protected');
    ul.innerHTML = '';
    protectedSel.forEach(h => {
      const seg = S.savedSegmentsMap.get(h);
      const li = document.createElement('li');
      li.textContent = `${fmt(h)}${seg.label ? '（書籤：' + seg.label + '）' : '（保留）'}`;
      ul.appendChild(li);
    });
    warn.style.display = '';
    check.checked = false;
    btn.disabled = true;
    check.onchange = () => { btn.disabled = !check.checked; };
  } else {
    warn.style.display = 'none';
    btn.disabled = false;
    check.onchange = null;
  }
  document.getElementById('delete-modal').style.display = 'flex';
}

export function closeDeleteModal() {
  document.getElementById('delete-modal').style.display = 'none';
}

async function confirmDeleteRecordings() {
  const hours = [...S.selectedHours];
  try {
    const resp = await fetch('/storage/recordings/delete', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ camera_id: S.currentCamera, hours }),
    });
    if (!resp.ok) throw new Error();
    const r = await resp.json();
    closeDeleteModal();
    clearSelection();
    await loadTimeline();
    if (typeof loadBookmarks === 'function') loadBookmarks();
    alert(`已刪除 ${r.deleted_hours} 小時（影片目錄 ${r.dirs_removed}、軌跡 ${r.tracking_logs}、告警 ${r.health_alerts}）`);
  } catch (_) { alert('刪除失敗'); }
}

// ── onclick 綁定（原 index.html inline onclick，改用 addEventListener） ──
els.prevMonthBtn.addEventListener('click', prevMonth);
els.nextMonthBtn.addEventListener('click', nextMonth);
document.getElementById('btn-retain').addEventListener('click', onRetainClick);
document.getElementById('btn-bookmark').addEventListener('click', onBookmarkClick);
document.getElementById('btn-delete-rec').addEventListener('click', onDeleteRecClick);
document.getElementById('btn-clear-sel').addEventListener('click', clearSelection);
{
  const cancelBtn = document.querySelector('#delete-modal button:not(#delete-confirm-btn)');
  if (cancelBtn) cancelBtn.addEventListener('click', closeDeleteModal);
  const confirmBtn = document.getElementById('delete-confirm-btn');
  if (confirmBtn) confirmBtn.addEventListener('click', confirmDeleteRecordings);
}
