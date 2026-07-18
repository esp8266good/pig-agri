// static/js/state.js — 共享狀態、共用 DOM 參照、微型 UI 工具。不 import 任何模組。

export const S = {
  hls: null, ws: null, wsGeneration: 0, wsRetryTimer: null, wsRetryCount: 0,
  latestBoxes: [], vodStartTs: 0, bboxHistory: [], _dbg: null,
  currentCamera: null, currentType: 'rgb', animFrameId: null, isLive: true,
  currentMonth: null, selectedDay: null, monthHoursSet: new Set(),
  vodDebounceTimer: null, vodFetching: false, anomalyMap: {}, vodAlerts: [],
  showReadAlerts: false, liveAnomalyIntervalId: null, liveHandoffIntervalId: null,
  currentLiveUrl: null, currentObjectIds: new Set(), selectedObjectId: null,
  soloMode: false, sortKey: 'activity', sortDir: 1,
  selectMode: false, selectedHours: new Set(), savedSegmentsMap: new Map(),
  transportDragging: false, dragFrac: 0, seekCommitTimer: null,
  dragSeekPending: false, trackingFetchTimer: null, trackingCache: new Map(),
};

export const MAX_WS_RETRY = 5;
export const WS_RETRY_BASE_MS = 2000;

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
  vodBannerText:document.getElementById('vod-banner-text'),
  vodBannerLiveBtn: document.getElementById('vod-banner-live-btn'),
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
