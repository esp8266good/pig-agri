// static/js/state.js — 共享狀態、共用 DOM 參照、微型 UI 工具。不 import 任何模組。

export const S = {
  hls: null, ws: null, wsGeneration: 0, wsRetryTimer: null, wsRetryCount: 0,
  // 每次 loadVod 遞增。探測與 MANIFEST_PARSED 都是非同步的，同一個小時被連續
  // 載入兩次時（RGB→Thermal 切換就是這種情況）舊的那次還在跑，會把它自己的
  // Hls 覆蓋上來、播回舊的畫面種類。以 vodStartTs 當守衛擋不住這種同時段重載。
  vodGeneration: 0,
  // 回放中最後一次算得出來的真實時刻（unix 秒）。切到一個沒有錄影的畫面種類時
  // 播放器是空的，currentTime 與 PDT 都問不出東西，切回來要靠它才回得到原地。
  vodLastWall: null,
  latestBoxes: [], vodStartTs: 0, bboxHistory: [], _dbg: null,
  // LIVE 往回拖超過 bboxHistory 範圍時，從 REST /tracking 補回來的那一幀。
  // bboxHistory 是「筆數」上限（1000 筆），換算成時間長度會隨相機 fps 變動：
  // 10fps 約 100 秒，1fps 就有 1000 秒。所以不能靠它涵蓋使用者拖得到的範圍。
  liveRewind: null,
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
  // focusItems 只含現在畫面上的豬；離開畫面的異常在 focusRecent（「最近消失」）。
  // focusOnScreenCount 是後端數的「畫面上有幾隻」，清單空掉時要靠它分辨
  // 「都沒事」與「根本沒偵測到豬」，那兩種情況該去查的方向完全不同。
  focusLabels: {}, focusItems: [], focusStatus: 'ok',
  focusRecent: [], focusOnScreenCount: 0,
  // bbox 的座標系尺寸：camera_id → [w, h]（rgb 原始解析度，後端 /cameras 給）。
  // ⚠ 不能拿 <video> 的 videoWidth 當分母。rgb 那條串流剛好同尺寸所以看不出來，
  // 熱像改成原生 640x480 之後就露餡了：bbox 是 1280x720 的座標，除以 640
  // 等於把每個框放大一倍再往右下推，整片位移。
  cameraFrameSize: {},
  // 目前相機的熱像對位參數（見後端 thermal_align.py）。兩顆鏡頭視角不同，
  // 等比例換算過去還是會偏，這四個數字把它推回去。
  thermalAlign: { off_x: 0, off_y: 0, scale_x: 1, scale_y: 1 },
  // 熱像對位校正模式：在熱像畫面上拖曳移動、用按鈕縮放。
  alignEditing: false,
  // 每個框都標上 #ID。預設關：一堆色塊比框本身還吵。需要一眼對上豬隻編號時開。
  showAllIds: (() => {
    try { return localStorage.getItem('showAllIds') === 'true'; }
    catch (_) { return false; }
  })(),
  // 畫面上的框要畫到什麼程度。純視覺偏好，每個瀏覽器各自記。
  //   'focus' 只畫有意義的框（異常紅框，以及 LIVE 才有的關注清單橘／綠框）
  //   'ghost' 其餘的豬畫成極淡細線、不加 ID 標籤
  //   'all'   全部照常畫
  // 這個開關以前叫 focusOnlyBoxes（布林），而且寫死只在 LIVE 生效：回放沒有關注
  // 清單，過濾就只剩紅框，當時判斷「整片空白會分不出系統壞了沒」而整個跳過。
  // 現在改由左下角的計數（已隱藏 N 個正常框／未偵測到豬隻）承擔那件事，回放也
  // 就能一起套用了。
  boxDisplayMode: (() => {
    try {
      const v = localStorage.getItem('boxDisplayMode');
      if (v === 'focus' || v === 'ghost' || v === 'all') return v;
      // 舊版布林開關的值：沒勾就是要看全部。
      return localStorage.getItem('focusOnlyBoxes') === 'false' ? 'all' : 'focus';
    } catch (_) { return 'focus'; }
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
  boxModeSel:   document.getElementById('box-mode-select'),
  showIdsChk:   document.getElementById('show-ids-toggle'),
  alignPanel:   document.getElementById('align-panel'),
  alignToggleBtn: document.getElementById('align-toggle-btn'),
  alignReadout: document.getElementById('align-readout'),
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

// ── 回到即時邊緣 ──────────────────────────────────────────
// player.js（「回到即時」鈕）與 align.js（校正結束後恢復播放）都要用。
// 放在這裡而不是 player.js：align.js 反過來 import player.js 會與
// player.js → align.js 形成環，而這支只用得到 S 與 els。
export function goToLiveEdge() {
  if (S.hls && isFinite(S.hls.liveSyncPosition)) {
    try { els.video.currentTime = S.hls.liveSyncPosition; } catch (_) {}
  } else if (els.video.seekable && els.video.seekable.length) {
    try { els.video.currentTime = els.video.seekable.end(els.video.seekable.length - 1) - 0.5; } catch (_) {}
  }
  els.video.play().catch(() => {});
}

// ── Transport / scrubber ──────────────────────────────────
export function fmtClock(sec) {
  if (!isFinite(sec) || sec < 0) sec = 0;
  sec = Math.floor(sec);
  const m = Math.floor(sec / 60), s = sec % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}


// ── 底部固定面板的留白 ────────────────────────────────────
// 說明模式的說明條、時間軸多選後彈出的操作列，都貼在畫面底部，會蓋住頁面最後一段內容。
// 它們現在全部裝在 #bottom-stack（一個 flex column）裡面垂直排開，所以只要量這個
// 容器有多高就好：以前是分別量、再取最大值，那個做法預設了兩條面板互相重疊——
// 而它們確實重疊了，說明條把整條操作列蓋掉，數字卻看起來完全正常。
// 之後新增底部面板，直接放進 #bottom-stack 就會自動計入，這裡不用改。
export function syncBottomInset() {
  const stack = document.getElementById('bottom-stack');
  const inset = stack ? Math.round(stack.getBoundingClientRect().height) : 0;
  document.documentElement.style.setProperty('--bottom-inset', `${inset}px`);
}

// 說明條的高度會隨文字長度變，光在開關時量一次不夠。
if (typeof ResizeObserver !== 'undefined') {
  const stack = document.getElementById('bottom-stack');
  if (stack) new ResizeObserver(() => syncBottomInset()).observe(stack);
}
