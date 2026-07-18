// static/js/main.js — 應用程式進入點：init()、頂層事件綁定、儲存健康小燈輪詢。
import { S, els, setStatus, setSkeleton } from './state.js';
import { loadStream, startLiveTimers, stopLiveTimers, detachVodListeners } from './player.js';
import { loadTimeline, clearSelection, closeSlotActionMenu, closeDeleteModal, localDayStart } from './timeline.js';
import { switchTab, onSortHeaderClick, refreshAnomalyMap, refreshNotifications, loadSettings, loadBookmarks, closeBookmarkEditModal, openSettingsDrawer, closeSettingsDrawer, initSettingsDrawer } from './panels.js';
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

// 切換攝影機的完整狀態重置（原僅存在於 camSelect 的 change handler）。
// bindGridPick 的點格切換也走這條路徑，行為與下拉選單切換攝影機等價，
// 避免前一台攝影機的 anomaly/bbox/VOD 狀態殘留到新攝影機。
// 只含「重置」不含「載入」：loadStream()/loadTimeline()/startLiveTimers()
// 由呼叫端自行接續（bindGridPick 走 setViewMode('single') 內建的載入，
// 避免重複載入）。
function resetCameraState() {
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
    els.vodBanner.hidden = true;
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
}

bindGridPick(cam => {
  S.currentCamera = cam;
  els.camSelect.value = cam;
  try { localStorage.setItem('lastCamera', cam); } catch (_) {}
  resetCameraState();
  // setViewMode('single') 內部已呼叫 loadStream()/loadTimeline()/startLiveTimers()，
  // 這裡不重複呼叫；離開 grid、還原單畫面 UI 都在其中完成。
  setViewMode('single');
  // 與 camSelect change handler 對齊：立即刷新一次，不等 30s 輪詢。
  refreshAnomalyMap();
  refreshNotifications();
  if (typeof loadBookmarks === 'function') loadBookmarks();
});

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

    const savedMode = (() => { try { return localStorage.getItem('viewMode'); } catch (_) { return null; } })();
    if (savedMode === 'grid') setViewMode('grid');
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
  resetCameraState();
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

// #settings-btn：開啟設定 drawer
{
  const settingsBtn = document.getElementById('settings-btn');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', () => openSettingsDrawer());
  }
}
// #view-toggle-btn：單畫面 ⇄ Grid 多畫面監看
{
  const viewToggleBtn = document.getElementById('view-toggle-btn');
  if (viewToggleBtn) {
    viewToggleBtn.addEventListener('click',
      () => setViewMode(viewMode === 'grid' ? 'single' : 'grid'));
  }
}

// Esc 鍵關閉設定 drawer（僅在 drawer 開啟時）
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const drawer = document.getElementById('settings-drawer');
  if (drawer && drawer.classList.contains('open')) closeSettingsDrawer();
});

initSettingsDrawer();
init();
