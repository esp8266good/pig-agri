// static/js/main.js — 應用程式進入點：init()、頂層事件綁定、儲存健康小燈輪詢。
import { S, els, setStatus, setSkeleton } from './state.js';
import { loadStream, loadVod, startLiveTimers, stopLiveTimers, detachVodListeners, exitVodState } from './player.js';
import { loadTimeline, clearSelection, closeSlotActionMenu, closeDeleteModal, localDayStart } from './timeline.js';
import { switchTab, onSortHeaderClick, refreshAnomalyMap, refreshNotifications, loadSettings, loadBookmarks, closeBookmarkEditModal, openSettingsDrawer, closeSettingsDrawer, initSettingsDrawer, updateSoloHint } from './panels.js';
import { enterGrid, leaveGrid, bindGridPick, getGridPlaybackHour } from './grid.js';
import { ensureAuth } from './auth.js';

async function setViewMode(mode, opts = {}) {
  if (mode === S.viewMode) return;
  S.viewMode = mode;
  try { localStorage.setItem('viewMode', mode); } catch (_) {}
  const singleEls = [document.querySelector('.video-card'),
                     document.getElementById('timeline-section')];
  const toggleIcon = document.querySelector('#view-toggle-btn use');
  if (mode === 'grid') {
    stopLiveTimers();
    if (S.hls) { S.hls.destroy(); S.hls = null; }   // 單畫面播放器停掉省資源
    // 單畫面 tracking WS 進 grid 不需要（grid tile 本身不疊 bbox）；離開 grid
    // 會走 loadStream()→connectWS()（一般返回）或 loadVod()（Task 7：grid 回放中
    // 點格帶時段返回，見下方 vodStartTs 分支）補回連線或改用 REST /tracking，
    // 這裡關閉安全。一併清 retry timer／歸零 retry count，避免背景重連（斷線
    // 重試排程）在 grid 模式下悄悄補一條 WS 連線，也避免使用者帶著已耗盡大半
    // 的重試額度離開再返回。
    clearTimeout(S.wsRetryTimer);
    S.wsRetryCount = 0;
    S.wsGeneration++; S.ws?.close(); S.ws = null;
    // 正規化 VOD 狀態（Finding 1）：進 grid 前把殘留的 VOD 旗標/banner/listener
    // 清乾淨，避免從 grid 返回單畫面時顯示殘留的 VOD transport/橫幅、或殘留
    // listener 用過期 vodStartTs 打 /tracking 導錯 overlay。不含 loadStream/
    // startLiveTimers——grid 本身有自己的計時器，不需要單畫面播放器連線。
    exitVodState();
    singleEls.forEach(el => el.hidden = true);
    toggleIcon.setAttribute('href', '#i-single');
    await enterGrid();
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
  // setViewMode('single') 內部已呼叫 loadStream()/loadTimeline()/startLiveTimers()
  // （或帶 vodStartTs 時改呼叫 loadVod()/loadTimeline()），這裡不重複呼叫；
  // 離開 grid、還原單畫面 UI 都在其中完成。
  const hourTs = getGridPlaybackHour();
  setViewMode('single', hourTs != null ? { vodStartTs: hourTs } : {});
  // 與 camSelect change handler 對齊：立即刷新一次，不等 30s 輪詢。
  refreshAnomalyMap();
  refreshNotifications();
  if (typeof loadBookmarks === 'function') loadBookmarks();
});

// grid 模式下切 RGB/Thermal：重建所有 tile（enterGrid 內建 _gridGen 守衛與
// /cameras 重抓——順便刷新 active_types）。
document.addEventListener('pigagri:grid-type-change', () => {
  if (S.viewMode === 'grid') enterGrid();
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
  // 驗證關閉（預設）時 ensureAuth 立刻回，下面完全照舊；開啟且未登入時會
  // 卡在登入畫面，登入成功才往下跑——避免每支 API 都吃 401 畫出一堆錯誤。
  await ensureAuth();
  try {
    const res = await fetch('/cameras');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const { cameras, active_types } = await res.json();
    S.cameraActiveTypes = active_types || {};
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
  if (soloCb) soloCb.addEventListener('change', e => { S.soloMode = e.target.checked; updateSoloHint(); });
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
      () => setViewMode(S.viewMode === 'grid' ? 'single' : 'grid'));
  }
}

// Esc 鍵關閉設定 drawer（僅在 drawer 開啟時）
document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  const drawer = document.getElementById('settings-drawer');
  if (drawer && drawer.classList.contains('open')) closeSettingsDrawer();
});

// ── header 高度同步（供 #tab-bar sticky top 用）───────────────
// 窄螢幕 header-controls 會 flex-wrap 折兩行，讓 #tab-bar 用即時量測的
// --header-h 取代寫死 60px，才不會被折成兩行的 header 蓋住。
{
  const headerEl = document.querySelector('header');
  if (headerEl && 'ResizeObserver' in window) {
    const syncHeaderHeight = () =>
      document.documentElement.style.setProperty('--header-h', `${headerEl.offsetHeight}px`);
    new ResizeObserver(syncHeaderHeight).observe(headerEl);
    syncHeaderHeight();
  }
}

initSettingsDrawer();
init();
