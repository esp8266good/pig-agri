// static/js/panels.js — 書籤／豬隻狀態／通知中心／設定 面板。
import { S, els } from './state.js';
import { loadVod } from './player.js';
import { loadTimeline } from './timeline.js';

export async function loadBookmarks() {
  const ul = document.getElementById('bookmark-list');
  if (!ul || !S.currentCamera) return;
  try {
    const resp = await fetch(`/storage/bookmarks?camera_id=${S.currentCamera}`);
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
        noteDiv.style.cssText = 'font-size:12px;opacity:.75;padding-left:8px;white-space:pre-wrap;color:var(--text-muted)';
        li.appendChild(noteDiv);
      }
      ul.appendChild(li);
    });
  } catch (_) {}
}

let _editingBookmarkId = null;
export function openBookmarkEditModal(seg) {
  _editingBookmarkId = seg.id;
  document.getElementById('bookmark-edit-label').value = seg.label || '';
  document.getElementById('bookmark-edit-note').value = seg.note || '';
  const m = document.getElementById('bookmark-edit-modal');
  m.style.display = 'flex';
}
export function closeBookmarkEditModal() {
  _editingBookmarkId = null;
  const m = document.getElementById('bookmark-edit-modal');
  if (m) m.style.display = 'none';
}
async function saveBookmarkEdit() {
  if (_editingBookmarkId === null) return;
  const label = document.getElementById('bookmark-edit-label').value.trim();
  const note = document.getElementById('bookmark-edit-note').value.trim() || null;
  if (!label) { alert('名稱不可空白'); return; }
  const btn = document.getElementById('bookmark-edit-save-btn');
  btn.disabled = true;
  try {
    const resp = await fetch(`/storage/segments/${_editingBookmarkId}`, {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label, note }),
    });
    if (!resp.ok) throw new Error();
    closeBookmarkEditModal();
    await loadBookmarks();
    await loadTimeline();  // savedSegmentsMap 也要刷新(label 可能變)
  } catch (_) { alert('儲存失敗'); }
  finally { btn.disabled = false; }
}

// ── Anomaly map ───────────────────────────────────────────
export async function refreshAnomalyMap() {
  if (!S.currentCamera) return;
  try {
    const data = await fetch(`/alerts/active?camera_id=${S.currentCamera}`).then(r => r.json());
    const camCache = data.cache?.[S.currentCamera] ?? {};
    S.anomalyMap = {};
    for (const [oid, info] of Object.entries(camCache)) {
      S.anomalyMap[parseInt(oid)] = info;
    }
    renderPigStatus();
  } catch (_) {}
}

export function updateVodAnomalyMap(currentTs) {
  S.anomalyMap = {};
  for (const alert of S.vodAlerts) {
    const winStart = alert.triggered_at_unix - 1800;
    const winEnd   = alert.triggered_at_unix;
    if (currentTs >= winStart && currentTs <= winEnd) {
      const entry = S.anomalyMap[alert.object_id] ?? { activity_anomaly: false, temp_anomaly: false };
      if (alert.metric === 'activity')    entry.activity_anomaly = true;
      if (alert.metric === 'temperature') entry.temp_anomaly = true;
      S.anomalyMap[alert.object_id] = entry;
    }
  }
  renderPigStatus();
}

// ── Tab panel ─────────────────────────────────────────────
export function switchTab(tabName) {
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tabName);
  });
  document.querySelectorAll('.tab-content').forEach(c => {
    c.classList.toggle('active', c.id === `tab-${tabName}`);
  });
}

// 依 sortKey/sortDir 排序；null（未分析到）值永遠沉底，避免誤導採血。
function sortPigRows(rows) {
  return rows.sort((a, b) => {
    if (S.sortKey === 'id') return (a.oid - b.oid) * S.sortDir;
    const va = S.sortKey === 'activity' ? a.act : a.temp;
    const vb = S.sortKey === 'activity' ? b.act : b.temp;
    if (va == null && vb == null) return a.oid - b.oid;
    if (va == null) return 1;
    if (vb == null) return -1;
    return (va - vb) * S.sortDir;
  });
}

export function onSortHeaderClick(key) {
  if (S.sortKey === key) S.sortDir = -S.sortDir;
  else { S.sortKey = key; S.sortDir = 1; }   // 換欄一律回升序
  renderPigStatus();
}

function updateSortIndicators() {
  document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
    const ind = th.querySelector('.sort-ind');
    if (!ind) return;
    ind.textContent = (th.dataset.sort === S.sortKey) ? (S.sortDir === 1 ? ' ▲' : ' ▼') : '';
  });
}

function togglePigSelection(oid) {
  S.selectedObjectId = (S.selectedObjectId === oid) ? null : oid;
  renderPigStatus();   // 重繪反映 .selected；bbox 強調由 drawBoxes 每幀自然反映
}

export function renderPigStatus() {
  if (!els.pigStatusBody) return;
  els.pigStatusBody.innerHTML = '';
  updateSortIndicators();
  if (S.currentObjectIds.size === 0) {
    els.pigStatusBody.innerHTML =
      '<tr><td colspan="3" class="pig-empty-msg">目前無偵測到豬隻</td></tr>';
    return;
  }
  const rows = [];
  for (const oid of S.currentObjectIds) {
    const a = S.anomalyMap[oid] ?? null;
    rows.push({
      oid,
      act:  a?.activity_current ?? null,
      temp: a?.temp_current ?? null,
      actAnomaly:  a?.activity_anomaly ?? false,
      tempAnomaly: a?.temp_anomaly ?? false,
    });
  }
  sortPigRows(rows);
  const maxRate = Math.max(...rows.map(r => r.act ?? 0), 1e-9);
  for (const r of rows) {
    const actVal  = r.act  != null ? r.act.toFixed(1)  : '—';
    const tempVal = r.temp != null ? r.temp.toFixed(1) : '—';
    const row = document.createElement('tr');
    row.classList.add('pig-row');
    row.dataset.oid = r.oid;
    if (S.selectedObjectId === r.oid) row.classList.add('selected');
    if (r.actAnomaly || r.tempAnomaly) row.classList.add('anomaly-row');
    row.addEventListener('click', () => togglePigSelection(r.oid));
    row.innerHTML = `
      <td>#${r.oid}</td>
      <td class="${r.actAnomaly ? 'anomaly-cell' : ''}">
        ${r.actAnomaly ? '⚠ ' : ''}${actVal}
      </td>
      <td class="${r.tempAnomaly ? 'anomaly-cell' : ''}">
        ${r.tempAnomaly ? '🌡 ' : ''}${tempVal}
      </td>`;
    if (r.act != null) {
      const activityTd = row.children[1];
      const bar = document.createElement('div');
      bar.className = 'activity-bar';
      bar.style.width = `${Math.max(4, Math.round(r.act / maxRate * 100))}%`;
      activityTd.appendChild(bar);
    }
    els.pigStatusBody.appendChild(row);
  }
}

function renderNotifications(alerts) {
  if (!els.alertListEl) return;
  els.alertListEl.innerHTML = '';
  if (!alerts.length) {
    els.alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
    return;
  }
  for (const alert of alerts) {
    const dt = new Date(alert.triggered_at_unix * 1000)
      .toLocaleString('zh-TW', {year:'numeric',month:'2-digit',day:'2-digit',
                                hour:'2-digit',minute:'2-digit'});
    const sigma = alert.std_value > 0
      ? ((alert.current_value - alert.mean_value) / alert.std_value).toFixed(1)
      : '—';
    const metricLabel = alert.metric === 'activity' ? '活動量偏低' : '體溫異常';
    const li = document.createElement('li');
    li.className = 'alert-item' + (alert.is_read ? '' : ' unread');
    // Finding 3（whole-branch review）：alert.camera_id 來自後端/DB，非硬編字面值，
    // 用 textContent 建節點而非 innerHTML 字串內插，避免注入風險（防禦性強化，行為不變）。
    const infoDiv = document.createElement('div');
    infoDiv.className = 'alert-info';
    const camSpan = document.createElement('span');
    camSpan.className = 'alert-cam';
    camSpan.textContent = `${alert.camera_id} #${alert.object_id}`;
    const metricSpan = document.createElement('span');
    metricSpan.className = 'alert-metric';
    metricSpan.textContent = metricLabel;
    const timeSpan = document.createElement('span');
    timeSpan.className = 'alert-time';
    timeSpan.textContent = dt;
    const sigmaSpan = document.createElement('span');
    sigmaSpan.className = 'alert-sigma';
    sigmaSpan.textContent = `偏差 ${sigma}σ`;
    infoDiv.append(camSpan, metricSpan, timeSpan, sigmaSpan);

    const markReadBtn = document.createElement('button');
    markReadBtn.className = 'mark-read-btn';
    markReadBtn.disabled = !!alert.is_read;
    markReadBtn.textContent = alert.is_read ? '已讀' : '標記已讀';

    const deleteBtn = document.createElement('button');
    deleteBtn.className = 'mark-read-btn';
    deleteBtn.textContent = '刪除';

    li.append(infoDiv, markReadBtn, deleteBtn);
    markReadBtn.addEventListener('click', () => markAlertRead(alert.id, markReadBtn));
    deleteBtn.addEventListener('click', () => onDeleteAlertClick(alert.id, deleteBtn));
    li.addEventListener('click', e => {
      if (e.target.classList.contains('mark-read-btn')) return;
      if (alert.camera_id !== S.currentCamera) {
        els.camSelect.value = alert.camera_id;
        S.currentCamera = alert.camera_id;
      }
      loadVod(alert.triggered_at_unix - 1800);
    });
    els.alertListEl.appendChild(li);
  }
}

async function _refreshBellBadge() {
  try {
    const d = await fetch('/alerts?unread_only=true').then(r => r.json());
    const n = (d.alerts || []).length;
    els.bellBadge.textContent = n;
    els.bellBadge.style.display = n > 0 ? '' : 'none';
  } catch (_) {}
}

function _emptyNotifIfNeeded() {
  if (els.alertListEl && els.alertListEl.children.length === 0) {
    els.alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
  }
}

async function markAlertRead(alertId, btn) {
  try {
    const resp = await fetch(`/alerts/${alertId}/read`, { method: 'PUT' });
    if (!resp.ok) return;
    const li = btn.closest('.alert-item');
    if (!S.showReadAlerts) {
      li.remove();
      _emptyNotifIfNeeded();
    } else {
      btn.textContent = '已讀';
      btn.disabled = true;
      li.classList.remove('unread');
    }
    await _refreshBellBadge();
  } catch (_) {}
}

async function onDeleteAlertClick(alertId, btn) {
  if (!confirm('永久刪除此警示記錄?')) return;
  try {
    const resp = await fetch(`/alerts/${alertId}`, { method: 'DELETE' });
    if (!resp.ok) { alert('刪除失敗'); return; }
    btn.closest('.alert-item').remove();
    _emptyNotifIfNeeded();
    await _refreshBellBadge();
  } catch (_) { alert('刪除失敗'); }
}

async function onClearReadClick() {
  const target = S.currentCamera ? `「${S.currentCamera}」` : '所有攝影機';
  if (!confirm(`永久清空${target}的所有已讀警示?(未讀不會被刪)`)) return;
  try {
    const url = S.currentCamera
      ? `/alerts?read_only=true&camera_id=${S.currentCamera}`
      : '/alerts?read_only=true';
    const resp = await fetch(url, { method: 'DELETE' });
    if (!resp.ok) { alert('清空失敗'); return; }
    const { deleted } = await resp.json();
    if (deleted === 0) {
      alert('沒有已讀可清除');
    } else {
      alert(`已刪除 ${deleted} 筆已讀警示`);
    }
    refreshNotifications();
  } catch (_) { alert('清空失敗'); }
}

export async function refreshNotifications() {
  if (!S.currentCamera) return;
  try {
    const unread = S.showReadAlerts ? '' : '&unread_only=true';
    const d = await fetch(`/alerts?camera_id=${S.currentCamera}&limit=50${unread}`)
      .then(r => r.json());
    renderNotifications(d.alerts || []);
    await _refreshBellBadge();
  } catch (_) {}
}

// ── Settings ──────────────────────────────────────────────
export async function loadSettings() {
  try {
    const resp = await fetch('/settings');
    if (!resp.ok) return;
    const data = await resp.json();
    const a = document.getElementById('set-analysis-interval');
    const t = document.getElementById('set-anomaly-threshold');
    const r = document.getElementById('set-hls-retention');
    const w = document.getElementById('set-analysis-window');
    const te = document.getElementById('set-temp-enabled');
    if (a && data.analysis_interval_minutes !== undefined) a.value = data.analysis_interval_minutes;
    if (t && data.anomaly_std_threshold !== undefined)     t.value = data.anomaly_std_threshold;
    if (r && data.hls_retention_days !== undefined)        r.value = data.hls_retention_days;
    if (w && data.analysis_window_minutes !== undefined)   w.value = data.analysis_window_minutes;
    if (te && data.temp_anomaly_enabled !== undefined)
      te.value = String(data.temp_anomaly_enabled).toLowerCase() === 'true' ? 'true' : 'false';
    const _sse = document.getElementById('set-recording_schedule_enabled');
    if (_sse) _sse.checked = String(data.recording_schedule_enabled) === 'true';
    const _smap = {
      'set-recording_off_start': 'recording_off_start',
      'set-recording_off_end': 'recording_off_end',
      'set-storage_min_free_gb': 'storage_min_free_gb',
      'set-storage_check_interval_seconds': 'storage_check_interval_seconds',
      'set-ntfy_url': 'ntfy_url',
      'set-ntfy_revive_priority': 'ntfy_revive_priority',
      'set-gpu_off_start': 'gpu_off_start',
      'set-gpu_off_end': 'gpu_off_end',
    };
    for (const [id, key] of Object.entries(_smap)) {
      const el = document.getElementById(id);
      if (el && data[key] != null) el.value = data[key];
    }
    const _ne = document.getElementById('set-ntfy_enabled');
    if (_ne) _ne.checked = String(data.ntfy_enabled) === 'true';
    const _ge = document.getElementById('set-gpu_off_schedule_enabled');
    if (_ge) _ge.checked = String(data.gpu_off_schedule_enabled) === 'true';
  } catch (_) {}
  syncDepFields();
}

export async function saveSettings() {
  const body = {
    analysis_interval_minutes: document.getElementById('set-analysis-interval').value,
    analysis_window_minutes:   document.getElementById('set-analysis-window').value,
    anomaly_std_threshold:     document.getElementById('set-anomaly-threshold').value,
    hls_retention_days:        document.getElementById('set-hls-retention').value,
    temp_anomaly_enabled:      document.getElementById('set-temp-enabled').value,
    recording_schedule_enabled: String(document.getElementById('set-recording_schedule_enabled').checked),
    recording_off_start:        document.getElementById('set-recording_off_start').value,
    recording_off_end:          document.getElementById('set-recording_off_end').value,
    storage_min_free_gb:        document.getElementById('set-storage_min_free_gb').value,
    storage_check_interval_seconds: document.getElementById('set-storage_check_interval_seconds').value,
    ntfy_enabled:               String(document.getElementById('set-ntfy_enabled').checked),
    ntfy_url:                   document.getElementById('set-ntfy_url').value,
    ntfy_revive_priority:       document.getElementById('set-ntfy_revive_priority').value,
    gpu_off_schedule_enabled:   String(document.getElementById('set-gpu_off_schedule_enabled').checked),
    gpu_off_start:              document.getElementById('set-gpu_off_start').value,
    gpu_off_end:                document.getElementById('set-gpu_off_end').value,
  };
  const statusEl = document.getElementById('settings-status');
  try {
    const resp = await fetch('/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (resp.ok) {
      statusEl.textContent = '✓ 已儲存';
      setTimeout(() => { statusEl.textContent = ''; }, 3000);
      return true;
    } else {
      const err = await resp.json();
      statusEl.textContent = `✗ ${err.detail || '儲存失敗'}`;
      return false;
    }
  } catch (e) {
    statusEl.textContent = `✗ 網路錯誤`;
    return false;
  }
}

// ── Settings drawer ───────────────────────────────────────
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
  const onFieldEvent = e => {
    _settingsDirty = true;
    if (e.target.matches('input[type="number"]')) validateField(e.target);
    syncDepFields();
  };
  // brief 只綁 input；select/checkbox 在部分瀏覽器只觸發 change，兩者都綁同一 handler 才能保證
  // dirty 追蹤／連動停用／即時驗證一致觸發。
  body.addEventListener('input', onFieldEvent);
  body.addEventListener('change', onFieldEvent);
  document.getElementById('settings-close-btn')
    .addEventListener('click', () => closeSettingsDrawer());
  document.getElementById('settings-overlay')
    .addEventListener('click', () => closeSettingsDrawer());
  document.getElementById('save-settings-btn').addEventListener('click', async () => {
    const nums = [...body.querySelectorAll('input[type="number"]')];
    if (!nums.every(validateField)) return;
    const ok = await saveSettings();
    if (ok) _settingsDirty = false;   // 失敗時保留 dirty，關 drawer 仍會提示未儲存
  });
}

// 切攝影機 / RGB↔Thermal / Live↔VOD 時清掉選取與 solo（避免殘留別來源的高亮）。
// sortKey/sortDir 為使用者偏好，不重置。
export function clearPigSelection() {
  S.selectedObjectId = null;
  S.soloMode = false;
  const cb = document.getElementById('solo-checkbox');
  if (cb) cb.checked = false;
}

// ── onclick 綁定（原 index.html inline onclick，改用 addEventListener） ──
document.querySelectorAll('.tab-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    switchTab(btn.dataset.tab);
    if (btn.dataset.tab === 'bookmarks') loadBookmarks();
  });
});
document.getElementById('clear-read-btn').addEventListener('click', onClearReadClick);
{
  const modal = document.getElementById('bookmark-edit-modal');
  const cancelBtn = modal.querySelector('button:not(#bookmark-edit-save-btn)');
  if (cancelBtn) cancelBtn.addEventListener('click', closeBookmarkEditModal);
  const saveBtn = document.getElementById('bookmark-edit-save-btn');
  if (saveBtn) saveBtn.addEventListener('click', saveBookmarkEdit);
}
