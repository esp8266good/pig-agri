// static/js/main.js — 應用程式進入點：init()、頂層事件綁定、儲存健康小燈輪詢。
import { S, els, setStatus, setSkeleton } from './state.js';
import { loadStream, startLiveTimers, stopLiveTimers, detachVodListeners } from './player.js';
import { loadTimeline, clearSelection, closeSlotActionMenu, closeDeleteModal, localDayStart } from './timeline.js';
import { switchTab, onSortHeaderClick, refreshAnomalyMap, refreshNotifications, loadSettings, loadBookmarks, closeBookmarkEditModal } from './panels.js';

// ── Storage health pill ────────────────────────────────────
async function pollStorageHealth() {
  try {
    const r = await fetch('/storage/health');
    if (!r.ok) return;
    const h = await r.json();
    const pill = document.getElementById('storage-pill');
    if (!pill) return;
    const mode = h.target_mode || 'record';
    const recState = h.recording_state || 'ok';
    let cls = 'ok', label = '錄影中';
    if (mode === 'drop') { cls = 'down'; label = '儲存故障：丟幀'; }
    else if (recState === 'down') { cls = 'down'; label = '錄影碟故障 → ephemeral live'; }
    else if (mode === 'ephemeral') { cls = 'degraded'; label = h.recording_time === false ? '夜間不錄影（live 中）' : 'ephemeral live'; }
    else if (recState === 'degraded') { cls = 'degraded'; label = `空間不足（剩 ${h.recording_free_gb}GB）`; }
    pill.className = 'storage-pill ' + cls;
    pill.title = label;
    pill.style.display = '';
  } catch (e) { /* 靜默：監控小燈非關鍵路徑 */ }
}

// ── Init ──────────────────────────────────────────────────
async function init() {
  try {
    const res = await fetch('/cameras');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { cameras } = await res.json();
    if (cameras.length === 0) throw new Error('no cameras');

    cameras.forEach(cam => {
      const opt = document.createElement('option');
      opt.value = cam;
      opt.textContent = cam;
      els.camSelect.appendChild(opt);
    });

    const cachedCam = (() => { try { return localStorage.getItem('lastCamera'); } catch (_) { return null; } })();
    S.currentCamera = (cachedCam && cameras.includes(cachedCam)) ? cachedCam : cameras[0];
    els.camSelect.value = S.currentCamera;
    loadStream();
    const today = new Date();
    S.currentMonth = new Date(today.getFullYear(), today.getMonth(), 1);
    S.selectedDay = localDayStart(today);
    loadTimeline();
    startLiveTimers();
    refreshAnomalyMap();
    refreshNotifications();
    loadSettings();
    pollStorageHealth();
    setInterval(pollStorageHealth, 20000);
  } catch (e) {
    setSkeleton(false);
    setStatus(`無法取得 camera 清單：${e.message}`, 'error');
  }
}

{
  const showReadEl = document.getElementById('show-read-toggle');
  if (showReadEl) {
    showReadEl.addEventListener('change', e => {
      S.showReadAlerts = e.target.checked;
      refreshNotifications();
    });
  }
}

els.camSelect.addEventListener('change', () => {
  S.currentCamera = els.camSelect.value;
  try { localStorage.setItem('lastCamera', S.currentCamera); } catch (_) {}
  stopLiveTimers();
  S.anomalyMap = {};
  S.vodAlerts  = [];
  S.currentObjectIds.clear();
  S.wsRetryCount = 0;
  S.latestBoxes = [];
  S.bboxHistory = [];
  els.countBadge.textContent = '—';
  if (!S.isLive) {
    S.isLive = true;
    els.liveBtn.style.display = 'none';
    detachVodListeners();
    clearTimeout(S.vodDebounceTimer);
    clearTimeout(S.trackingFetchTimer);
    S.trackingCache.clear();
    document.querySelectorAll('.timeline-slot.selected')
      .forEach(s => s.classList.remove('selected'));
  }
  clearSelection();
  if (typeof closeDeleteModal === 'function') closeDeleteModal();
  if (typeof closeBookmarkEditModal === 'function') closeBookmarkEditModal();
  if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
  loadStream();
  loadTimeline();
  startLiveTimers();
  refreshAnomalyMap();
  refreshNotifications();
  if (typeof loadBookmarks === 'function') loadBookmarks();
});

// pig 列表：可點欄頭排序 + 「只顯示選取」開關（一次性綁定）
document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
  th.addEventListener('click', () => onSortHeaderClick(th.dataset.sort));
});
{
  const soloCb = document.getElementById('solo-checkbox');
  if (soloCb) soloCb.addEventListener('change', e => { S.soloMode = e.target.checked; });
}
{
  const t = document.getElementById('select-mode-toggle');
  if (t) t.addEventListener('change', e => {
    S.selectMode = e.target.checked;
    clearSelection();
  });
}

// #bell-btn：切到通知分頁並捲動到底部面板
{
  const bellBtn = document.getElementById('bell-btn');
  if (bellBtn) {
    bellBtn.addEventListener('click', () => {
      switchTab('notifications');
      document.getElementById('bottom-panel').scrollIntoView({ behavior: 'smooth' });
    });
  }
}

// #settings-btn / #view-toggle-btn：暫時佔位，Task 5（設定抽屜）/ Task 8（多畫面）接手
{
  const settingsBtn = document.getElementById('settings-btn');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', () => console.debug('settings drawer: Task 5'));
  }
}
{
  const viewToggleBtn = document.getElementById('view-toggle-btn');
  if (viewToggleBtn) {
    viewToggleBtn.addEventListener('click', () => console.debug('grid view: Task 8'));
  }
}

init();
