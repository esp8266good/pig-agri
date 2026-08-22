// static/js/state.js — 共享狀態、共用 DOM 參照、微型 UI 工具。不 import 任何模組。

export const S = {
  hls: null, ws: null, wsGeneration: 0, wsRetryTimer: null, wsRetryCount: 0,
  latestBoxes: [], vodStartTs: 0, bboxHistory: [], _dbg: null,
  currentCamera: null, currentType: 'rgb', animFrameId: null, isLive: true,
  currentMonth: null, selectedDay: null, monthHoursSet: new Set(),
  vodDebounceTimer: null, vodFetching: false, anomalyMap: {}, vodAlerts: [],
  // 遮罩：目前相機的區域（正規化 0..1 座標），與編輯模式旗標。
  maskRegions: [], maskEditing: false,
  // 說明模式：點任何東西只顯示用途、不執行。觸控裝置沒有 hover，靠這個補。
  helpMode: false,
  // 顯示遮罩範圍。預設關：遮罩正常運作時不需要看到它，一直蓋著色塊只會干擾看豬。
  showMaskOverlay: (() => {
    try { return localStorage.getItem('showMaskOverlay') === 'true'; }
    catch (_) { return false; }
  })(),
  // 關注清單：object_id → 'anomaly'|'lowest'|'reference'，由 /alerts/focus 每輪更新。
  focusLabels: {}, focusItems: [], focusStatus: 'ok',
  // 只畫關注清單的框。純視覺偏好，每個瀏覽器各自記。
  focusOnlyBoxes: (() => {
    try { return localStorage.getItem('focusOnlyBoxes') !== 'false'; }
    catch (_) { return true; }
  })(),
  showReadAlerts: false,
  // 通知分頁的 keyset cursor：指向目前最後一個折疊群組裡最舊的那筆告警。
  alertHasMore: false, alertBeforeTs: null, alertBeforeId: null, liveAnomalyIntervalId: null, liveHandoffIntervalId: null,
  currentLiveUrl: null, currentObjectIds: new Set(), selectedObjectId: null,
  soloMode: false, sortKey: 'activity', sortDir: 1,
  selectMode: false, selectedHours: new Set(), savedSegmentsMap: new Map(),
  transportDragging: false, dragFrac: 0, seekCommitTimer: null,
  dragSeekPending: false, trackingFetchTimer: null, trackingCache: new Map(),
  cameraActiveTypes: {}, viewMode: 'single',
};

export const MAX_WS_RETRY = 5;
export const WS_RETRY_BASE_MS = 2000;

// /cameras 的 active_types 判斷：無該 cam 資料時 fail-open（回 true），
// 資訊缺失不得誤封鎖實際正常的串流。
export function hasActiveType(cam, type) {
  const t = S.cameraActiveTypes[cam];
  return !Array.isArray(t) || t.includes(type);
}

// ── DOM refs ──────────────────────────────────────────────
export const els = {
  video:        document.getElementById('video'),
  camSelect:    document.getElementById('cam-select'),
  statusEl:     document.getElementById('status'),
  statusTxt:    document.getElementById('status-text'),
  skeleton:     document.getElementById('skeleton'),
  countBadge:   document.getElementById('count-badge'),
  latencyChip:  document.getElementById('latency-chip'),
  latencyVal:   document.getElementById('latency-val'),
  toastEl:      document.getElementById('toast'),
  liveBtn:      document.getElementById('live-btn'),
  calLabelEl:   document.getElementById('calendar-label'),
  calGridEl:    document.getElementById('calendar-grid'),
  prevMonthBtn: document.getElementById('prev-month-btn'),
  nextMonthBtn: document.getElementById('next-month-btn'),
  timelineBar:  document.getElementById('timeline-bar'),
  bellBadge:    document.getElementById('bell-badge'),
  pigStatusBody:document.getElementById('pig-status-body'),
  alertListEl:  document.getElementById('alert-list'),
  alertLoadMoreBtn: document.getElementById('alert-load-more'),
  focusListEl:  document.getElementById('focus-list'),
  focusOnlyChk: document.getElementById('focus-only-checkbox'),
  transportEl:  document.getElementById('transport'),
  playBtn:      document.getElementById('play-btn'),
  playIconUse:  document.getElementById('play-icon-use'),
  timeCurEl:    document.getElementById('time-cur'),
  timeDurEl:    document.getElementById('time-dur'),
  seekTrack:    document.getElementById('seek-track'),
  seekBuffered: document.getElementById('seek-buffered'),
  seekProgress: document.getElementById('seek-progress'),
  seekHandle:   document.getElementById('seek-handle'),
  liveBtnT:     document.getElementById('transport-live-btn'),
  liveLabelT:   document.getElementById('transport-live-label'),
  vodBanner:    document.getElementById('vod-banner'),
  vodBannerText: document.getElementById('vod-banner-text'),
  vodBannerLiveBtn: document.getElementById('vod-banner-live-btn'),
  noSignal:     document.getElementById('no-signal'),
  noSignalText: document.getElementById('no-signal-text'),
};

// ── Status helpers ────────────────────────────────────────
export function setStatus(msg, cls = '') {
  els.statusTxt.textContent = msg;
  els.statusEl.className = 'status-pill' + (cls ? ' ' + cls : '');
}

let toastTimer = null;
export function showToast(msg, duration = 3000) {
  els.toastEl.textContent = msg;
  els.toastEl.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => els.toastEl.classList.remove('show'), duration);
}

export function setSkeleton(visible) {
  els.skeleton.classList.toggle('visible', visible);
}

// ── Transport / scrubber ──────────────────────────────────
export function fmtClock(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}


// ── 底部固定面板的留白 ────────────────────────────────────
// 說明模式的說明條、時間軸多選後彈出的操作列，都是 position: fixed 貼在畫面底部，
// 會蓋住頁面最後一段內容（多選操作列這個問題存在很久了）。
// 這裡量出目前可見的底部面板有多高，寫進 --bottom-inset，
// 由 CSS 去墊出對應的捲動空間。任何之後新增的底部面板只要加進 _BOTTOM_BARS 就會跟著生效。
const _BOTTOM_BARS = [
  () => {
    const el = document.getElementById('help-sheet');
    return el && !el.hasAttribute('hidden') ? el : null;
  },
  () => {
    const el = document.getElementById('storage-action-bar');
    return el && el.classList.contains('visible') ? el : null;
  },
];

export function syncBottomInset() {
  let inset = 0;
  for (const get of _BOTTOM_BARS) {
    const el = get();
    if (el) inset = Math.max(inset, el.offsetHeight || 0);
  }
  document.documentElement.style.setProperty('--bottom-inset', `${inset}px`);
}

// 說明條的高度會隨文字長度變，光在開關時量一次不夠。
if (typeof ResizeObserver !== 'undefined') {
  const ro = new ResizeObserver(() => syncBottomInset());
  for (const id of ['help-sheet', 'storage-action-bar']) {
    const el = document.getElementById(id);
    if (el) ro.observe(el);
  }
}
