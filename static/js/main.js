// static/js/main.js — 應用程式進入點：init()、頂層事件綁定、儲存健康小燈輪詢。
import { S, els, setStatus, setSkeleton } from './state.js';
import { loadStream, loadVod, startLiveTimers, stopLiveTimers, detachVodListeners, exitVodState } from './player.js';
import { loadTimeline, clearSelection, closeSlotActionMenu, closeDeleteModal, localDayStart } from './timeline.js';
import { initMaskEditor, loadMaskRegions, clearMaskRegions, canLeaveMaskEditor } from './mask.js';
import { initAlignEditor, loadThermalAlign, clearThermalAlign, syncAlignButtonVisibility } from './align.js';
import { initHelp } from './help.js';
import { switchTab, onSortHeaderClick, refreshAnomalyMap, refreshNotifications, loadMoreNotifications, refreshFocusList, setBoxDisplayMode, loadSettings, loadBookmarks, closeBookmarkEditModal, openSettingsDrawer, closeSettingsDrawer, initSettingsDrawer, updateSoloHint, bindCameraSwitch } from './panels.js';
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
    // 放大模式的退出鈕長在 video-card 上，而 grid 模式把 video-card 整個藏起來：
    // 不先退出的話，右欄與時間軸會在 grid 模式下憑空消失，而且沒有東西可以按回來。
    setVideoMax(false, false);
    toggleIcon.setAttribute('href', '#i-single');
    await enterGrid();
  } else {
    leaveGrid();
    singleEls.forEach(el => el.hidden = false);
    try { if (localStorage.getItem('videoMax') === 'true') setVideoMax(true, false); } catch (_) {}
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
  S.liveRewind = null;
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

// 換相機的唯一入口。以前有三條各自為政的路徑（下拉選單、grid 點磚、點通知跳過
// 去），只有下拉那條記得重載遮罩與對位參數，另外兩條把上一台相機的遮罩留在畫面
// 上、把上一台的對位參數套到這一台的熱像。新增第四個入口時走這裡就不會再漏。
//
// 回傳 false = 使用者在「遮罩有未儲存的改動」的確認視窗按了取消，沒有換成。
// opts.loadMedia=false 給「呼叫端自己會載入畫面」的路徑用（grid 點磚交給
// setViewMode、點通知交給 loadVod），避免同一秒載入兩次。
function switchCamera(cam, { loadMedia = true, resetState = true } = {}) {
  if (cam === S.currentCamera) return true;
  if (!canLeaveMaskEditor()) return false;
  S.currentCamera = cam;
  els.camSelect.value = cam;
  try { localStorage.setItem('lastCamera', cam); } catch (_) {}
  // 先清空再去 fetch。這兩支 fetch 是非同步的，不先清的話空窗期間畫面上畫的
  // 是前一台相機的遮罩與對位。
  clearMaskRegions();
  clearThermalAlign();
  if (resetState) resetCameraState();
  loadMaskRegions();
  loadThermalAlign();
  if (loadMedia) {
    loadStream();
    loadTimeline();
    startLiveTimers();
  }
  refreshAnomalyMap();
  refreshNotifications();
  if (typeof loadBookmarks === 'function') loadBookmarks();
  return true;
}

bindGridPick(cam => {
  // setViewMode('single') 內部已呼叫 loadStream()/loadTimeline()/startLiveTimers()
  // （或帶 vodStartTs 時改呼叫 loadVod()/loadTimeline()），所以這裡 loadMedia=false；
  // 離開 grid、還原單畫面 UI 都在其中完成。
  if (!switchCamera(cam, { loadMedia: false })) return;
  const hourTs = getGridPlaybackHour();
  setViewMode('single', hourTs != null ? { vodStartTs: hourTs } : {});
});

// 點通知跳到另一台相機：畫面由 loadVod 載，而且不能 resetCameraState
// （那會把 isLive 拉回 true、拆掉剛要用的回放狀態）。
bindCameraSwitch(cam => switchCamera(cam, { loadMedia: false, resetState: false }));

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
    const { cameras, active_types, frame_sizes } = await res.json();
    S.cameraActiveTypes = active_types || {};
    S.cameraFrameSize = frame_sizes || {};
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
    loadMaskRegions();
    loadThermalAlign();
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
  if (els.alertLoadMoreBtn) {
    els.alertLoadMoreBtn.addEventListener('click', () => loadMoreNotifications());
  }
  initMaskEditor();
  initHelp();
  if (els.boxModeSel) {
    // 選擇存在 localStorage，所以要從 S 反向同步到 DOM，不能只信 HTML 的 selected。
    els.boxModeSel.value = S.boxDisplayMode;
    els.boxModeSel.addEventListener('change', e => setBoxDisplayMode(e.target.value));
  }
  if (els.showIdsChk) {
    // 同上：值存在 localStorage，要從 S 反向同步到 DOM。
    els.showIdsChk.checked = S.showAllIds;
    els.showIdsChk.addEventListener('change', e => {
      S.showAllIds = e.target.checked;
      try { localStorage.setItem('showAllIds', String(S.showAllIds)); } catch (_) {}
    });
  }
  initAlignEditor();
  syncAlignButtonVisibility();
}

els.camSelect.addEventListener('change', () => {
  // 使用者反悔（遮罩有未存改動）時，select 已經跳到新的值了，要自己推回去。
  if (!switchCamera(els.camSelect.value)) els.camSelect.value = S.currentCamera;
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

// #more-btn：收納說明模式／操作手冊／登出的浮動選單。
// 開關手法仿 timeline.js 的 slot-action-menu（點外部關閉、延遲掛
// outside-click listener 避免開啟當下的那次 click 立刻被當外部點擊自關）；
// 差別是這裡錨點固定在 header，位置交給 CSS 相對定位，不用 JS 算座標。
{
  const moreBtn = document.getElementById('more-btn');
  const moreMenu = document.getElementById('more-menu');
  if (moreBtn && moreMenu) {
    const closeMoreMenu = () => {
      moreMenu.hidden = true;
      moreBtn.setAttribute('aria-expanded', 'false');
      // 拆 listener 的 capture 旗標要跟掛的時候一致，否則拆不掉，
      // 選單關了之後這條還留在 document 上。
      document.removeEventListener('click', onOutsideClick, true);
    };
    const onOutsideClick = (e) => {
      // 點按鈕自己不算「外部」：捕獲階段比按鈕自己的 handler 早跑，
      // 這裡先把選單關掉的話，按鈕的 handler 會看到 hidden=true 又把它開回來，
      // 於是再按一次關不掉。用 closest 而不是 e.target !== moreBtn，因為
      // 實際被點到的是按鈕裡的 <svg>。
      if (e.target.closest?.('#more-btn')) return;
      if (!moreMenu.contains(e.target)) closeMoreMenu();
    };
    moreBtn.addEventListener('click', () => {
      const willOpen = moreMenu.hidden;
      if (willOpen) {
        moreMenu.hidden = false;
        moreBtn.setAttribute('aria-expanded', 'true');
        // 掛捕獲階段：說明模式的攔截器（help.js）在捕獲階段就 stopPropagation，
        // 掛冒泡階段的話說明模式下點畫面別處收不到事件，選單永遠關不掉。
        setTimeout(() => document.addEventListener('click', onOutsideClick, true), 0);
      } else {
        closeMoreMenu();
      }
    });
    // 選單裡任一項目按下去之後就收合，不管該項目自己的行為是什麼
    // （切換說明模式／開新分頁看手冊／登出）。
    moreMenu.addEventListener('click', (e) => {
      if (e.target.closest('button, a')) closeMoreMenu();
    });
    // Escape 先關選單就好。放大影片那條 Escape 也掛在 window 上，不擋掉的話
    // 一次按鍵會同時關選單又退出放大，使用者只想收掉選單卻掉出放大模式。
    window.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && !moreMenu.hidden) {
        closeMoreMenu();
        e.stopImmediatePropagation();
      }
    });
  }
}
// ── 放大影片 ──────────────────────────────────────────────
// 收起右欄與時間軸，把整個版面讓給影片。純視覺切換，不碰播放器也不碰資料，
// 所以離開時什麼都不用還原。每個瀏覽器各自記住上次的選擇。
// persist=false 用在「不是使用者主動關的」情況（例如切去 grid 被迫退出）：
// 那時不該把偏好也一起改掉，否則從 grid 回到單畫面會莫名其妙變回小畫面。
function setVideoMax(on, persist = true) {
  document.body.classList.toggle('video-max', on);
  const btn = document.getElementById('video-max-btn');
  if (btn) {
    btn.setAttribute('aria-pressed', String(on));
    btn.setAttribute('aria-label', on ? '還原影片大小' : '放大影片');
    btn.title = on ? '還原版面（顯示右欄與時間軸）' : '放大影片（收起右欄與時間軸）';
    document.getElementById('video-max-icon')
      ?.setAttribute('href', on ? '#i-collapse' : '#i-expand');
  }
  if (persist) { try { localStorage.setItem('videoMax', String(on)); } catch (_) {} }
}
export function isVideoMax() { return document.body.classList.contains('video-max'); }

document.getElementById('video-max-btn')
  ?.addEventListener('click', () => setVideoMax(!isVideoMax()));
// Esc 是「退出放大」的通用預期。只在放大時吃掉，不影響其他 Esc 用途。
window.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && isVideoMax()) { setVideoMax(false); e.preventDefault(); }
});
try {
  if (localStorage.getItem('videoMax') === 'true') setVideoMax(true);
} catch (_) {}

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
