// static/js/player.js — HLS 播放（live / VOD）、WS 追蹤連線、canvas bbox 疊圖、transport scrubber。
import { S, els, MAX_WS_RETRY, WS_RETRY_BASE_MS, setStatus, showToast, setSkeleton, fmtClock, hasActiveType } from './state.js';
import { clearPigSelection, renderPigStatus, updateVodAnomalyMap, refreshAnomalyMap, refreshNotifications } from './panels.js';

// #no-signal 在 index.html 靜態帶 aria-hidden="true"（與初始 hidden 狀態配對）；
// 顯示時若不同步翻成 false，畫面上出現的無訊號文案對輔助科技永遠不存在。
function showNoSignal(msg) {
  els.noSignalText.textContent = msg;
  els.noSignal.hidden = false;
  els.noSignal.setAttribute('aria-hidden', 'false');
  setSkeleton(false);
}
function hideNoSignal() {
  els.noSignal.hidden = true;
  els.noSignal.setAttribute('aria-hidden', 'true');
}

// ── VOD mode ──────────────────────────────────────────────
export function loadVod(startTs) {
  S.isLive = false;
  clearPigSelection();
  S.vodStartTs = startTs;
  S.vodFetching = false;
  S.bboxHistory = [];
  els.liveBtn.style.display = '';
  S.latestBoxes = [];
  els.countBadge.textContent = '—';
  els.latencyChip.style.display = 'none';
  clearTimeout(S.vodDebounceTimer);
  stopLiveTimers();
  S.anomalyMap = {};
  S.vodAlerts  = [];
  S.currentObjectIds.clear();
  S.trackingCache.clear();
  S.transportDragging = false;
  els.seekTrack.classList.remove('dragging');
  clearTimeout(S.trackingFetchTimer);

  // 抓此 VOD 時段的歷史 alerts（含前 30 分鐘）
  const vodEnd = startTs + 3600;
  fetch(`/alerts?camera_id=${S.currentCamera}&start_ts=${startTs - 1800}&end_ts=${vodEnd + 300}`)
    .then(r => r.json())
    .then(data => { S.vodAlerts = data.alerts || []; })
    .catch(() => {});

  // 斷開 WS（不重連）
  clearTimeout(S.wsRetryTimer);
  S.wsGeneration++;
  if (S.ws) { S.ws.close(); S.ws = null; }

  detachVodListeners();
  if (S.hls) { S.hls.destroy(); S.hls = null; }
  els.video.src = '';
  hideNoSignal();
  setSkeleton(true);
  setStatus('載入回放...', '');

  const vodUrl = `/stream/${S.currentCamera}/vod?start=${startTs}&end=${startTs + 3600}&type=${S.currentType}`;

  (async () => {
    try {
      const probe = await fetch(vodUrl);
      // 競態守衛：探測期間使用者已回 live 或切了別的時段 → 放棄，
      // 不得在新狀態上覆蓋 S.hls / 佔位。
      if (S.isLive || S.vodStartTs !== startTs) return;
      if (probe.status === 404) {
        showNoSignal('無訊號（此時段無錄影）');
        setStatus('該時段無錄影', '');
        return;
      }
    } catch (_) { /* 探測失敗交給 hls.js 原錯誤路徑 */ }
    if (S.isLive || S.vodStartTs !== startTs) return;
    if (Hls.isSupported()) {
      S.hls = new Hls({ lowLatencyMode: false, backBufferLength: 0 });
      S.hls.loadSource(vodUrl);
      S.hls.attachMedia(els.video);

      S.hls.on(Hls.Events.MANIFEST_PARSED, () => {
        els.video.play().catch(() => {});
        setSkeleton(false);
        const dt = new Date(startTs * 1000);
        const label = dt.toLocaleString('zh-TW', {
          year: 'numeric', month: '2-digit', day: '2-digit',
          hour: '2-digit', minute: '2-digit',
        });
        setStatus(`回放中 ${label}`, 'vod');
        const hh = String(dt.getHours()).padStart(2, '0');
        els.vodBannerText.textContent =
          `回放中：${dt.getMonth() + 1}/${dt.getDate()} ${hh}:00–${hh}:59`;
        els.vodBanner.hidden = false;
      });

      S.hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          setSkeleton(false);
          setStatus(`回放錯誤：${data.details}`, 'error');
        }
      });

      attachVodListeners();
    }
  })();
}

// VOD → live 的狀態正規化（旗標／banner／listener／timer／cache），
// 不含連線建立（loadStream）與計時器啟動（startLiveTimers）。
// switchToLive() 與 setViewMode('grid')（main.js，見 Finding 1）共用，
// 避免「進 grid 卻殘留半個 VOD 狀態」複製一份邏輯出去維護。
// 回傳是否真的做了 teardown（已是 live 時回 false，呼叫端可據此判斷是否早退）。
export function exitVodState() {
  els.vodBanner.hidden = true;
  if (S.isLive) return false;
  S.isLive = true;
  els.liveBtn.style.display = 'none';
  detachVodListeners();
  clearTimeout(S.vodDebounceTimer);
  clearTimeout(S.trackingFetchTimer);
  S.trackingCache.clear();
  S.transportDragging = false;
  els.seekTrack.classList.remove('dragging');
  stopLiveTimers();
  S.anomalyMap = {};
  S.vodAlerts  = [];
  S.latestBoxes = [];
  S.bboxHistory = [];
  S.currentObjectIds.clear();
  document.querySelectorAll('.timeline-slot.selected')
    .forEach(s => s.classList.remove('selected'));
  S.wsRetryCount = 0;
  return true;
}

export function switchToLive() {
  if (!exitVodState()) return;   // 已是 live：僅隱藏 banner（exitVodState 內已處理），不重連
  loadStream();
  startLiveTimers();
  refreshAnomalyMap();
  refreshNotifications();
}

// ── Historical MOT overlay (VOD) ──────────────────────────
function onVodTimeUpdate() { scheduleTrackingFetch(false); }
function onVodSeeking()    { scheduleTrackingFetch(false); }
function onVodSeeked()     { scheduleTrackingFetch(true); }
function attachVodListeners() {
  els.video.addEventListener('timeupdate', onVodTimeUpdate);
  els.video.addEventListener('seeking',   onVodSeeking);
  els.video.addEventListener('seeked',    onVodSeeked);
}
export function detachVodListeners() {
  els.video.removeEventListener('timeupdate', onVodTimeUpdate);
  els.video.removeEventListener('seeking',   onVodSeeking);
  els.video.removeEventListener('seeked',    onVodSeeked);
}

// Fetch the tracking frame nearest the current VOD playhead and draw it.
// Debounced 100ms by default; pass immediate=true on discrete seeks.
// A small LRU cache keyed by 0.5s buckets avoids re-fetching while scrubbing.
function scheduleTrackingFetch(immediate = false) {
  if (S.isLive || !S.hls || !S.currentCamera) return;
  clearTimeout(S.trackingFetchTimer);
  const run = async () => {
    let ts;
    const pd = S.hls && S.hls.playingDate;
    if (pd && !isNaN(pd.getTime())) ts = pd.getTime() / 1000;          // 每段重錨、不累積
    else ts = S.vodStartTs + (els.video.currentTime || 0);             // 舊錄影/無 PDT 回退
    if (!ts) return;
    const key = S.currentCamera + '|' + Math.round(ts * 2);
    if (S.trackingCache.has(key)) { applyVodBoxes(S.trackingCache.get(key), ts); return; }
    if (S.vodFetching) { S.trackingFetchTimer = setTimeout(run, 60); return; }
    S.vodFetching = true;
    try {
      const resp = await fetch(`/tracking/${S.currentCamera}?start=${ts - 2}&end=${ts + 2}`);
      if (resp.ok) {
        const boxes = pickClosestFrame((await resp.json()).logs || [], ts);
        S.trackingCache.set(key, boxes);
        if (S.trackingCache.size > 400) S.trackingCache.delete(S.trackingCache.keys().next().value);
        applyVodBoxes(boxes, ts);
      }
    } catch (_) {}
    S.vodFetching = false;
  };
  if (immediate) run(); else S.trackingFetchTimer = setTimeout(run, 100);
}

function applyVodBoxes(boxes, ts) {
  S.latestBoxes = boxes;
  els.countBadge.textContent = boxes.length;
  S.currentObjectIds = new Set(boxes.map(o => o.object_id));
  updateVodAnomalyMap(ts);
}

function pickClosestFrame(logs, ts) {
  if (!logs.length) return [];
  const byFrame = new Map();
  for (const log of logs) {
    if (!byFrame.has(log.frame_id)) byFrame.set(log.frame_id, []);
    byFrame.get(log.frame_id).push(log);
  }
  let bestFrame = null, bestDist = Infinity;
  for (const [, frameLogs] of byFrame) {
    const dist = Math.abs(frameLogs[0].timestamp - ts);
    if (dist < bestDist) { bestDist = dist; bestFrame = frameLogs; }
  }
  return bestFrame || [];
}

// ── Type toggle ───────────────────────────────────────────
export function setType(type) {
  S.currentType = type;
  const btnRgb     = document.getElementById('btn-rgb');
  const btnThermal = document.getElementById('btn-thermal');
  btnRgb.classList.toggle('active', type === 'rgb');
  btnThermal.classList.toggle('active', type === 'thermal');
  btnRgb.setAttribute('aria-pressed', type === 'rgb');
  btnThermal.setAttribute('aria-pressed', type === 'thermal');
  if (S.viewMode === 'grid') {
    // grid 模式：單畫面播放器已銷毀，不 loadStream/switchToLive；
    // 由 main.js 監聽此事件重建所有 tile（player 不得 import grid）。
    document.dispatchEvent(new CustomEvent('pigagri:grid-type-change'));
    return;
  }
  if (!S.isLive) {
    switchToLive();
  } else {
    loadStream();
  }
}

// ── WebSocket (tracking) with auto-reconnect ──────────────
function connectWS(cameraId) {
  clearTimeout(S.wsRetryTimer);
  if (S.ws) { S.ws.close(); S.ws = null; }
  if (!cameraId) return;

  const gen = ++S.wsGeneration;  // each call gets a unique generation token
  const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${wsProtocol}//${location.host}/ws/tracking/${cameraId}`;
  S.ws = new WebSocket(url);

  S.ws.onopen = () => {
    if (gen !== S.wsGeneration) return;
    S.wsRetryCount = 0;
  };

  S.ws.onmessage = (e) => {
    if (gen !== S.wsGeneration) return;  // stale connection, discard
    try {
      const data = JSON.parse(e.data);
      S.latestBoxes = data.objects || [];
      els.countBadge.textContent = S.latestBoxes.length;
      S.currentObjectIds = new Set(S.latestBoxes.map(o => o.object_id));
      renderPigStatus();
      if (data.timestamp) {
        const delay = Date.now() - data.timestamp * 1000;
        els.latencyChip.style.display = '';
        els.latencyVal.textContent = Math.round(delay);
        // Buffer bbox with timestamp for HLS live latency alignment.
        // Keep enough history to cover the HLS back-buffer (~90s) so
        // scrubbing back in live mode still has matching boxes.
        S.bboxHistory.push({ ts: data.timestamp, boxes: S.latestBoxes });
        if (S.bboxHistory.length > 1000) S.bboxHistory.shift();
      }
    } catch (_) {}
  };

  S.ws.onclose = () => {
    // If gen no longer matches, this close was intentional (camera switched).
    // Do NOT reconnect — that would fight the new connection.
    if (gen !== S.wsGeneration) return;
    S.latestBoxes = [];
    els.countBadge.textContent = '—';
    if (S.wsRetryCount < MAX_WS_RETRY) {
      const delay = WS_RETRY_BASE_MS * Math.pow(1.5, S.wsRetryCount);
      S.wsRetryCount++;
      showToast(`追蹤連線中斷，${(delay/1000).toFixed(1)}s 後重試...`);
      S.wsRetryTimer = setTimeout(() => connectWS(cameraId), delay);
    } else {
      showToast('追蹤連線失敗，請重新整理頁面', 8000);
    }
  };

  S.ws.onerror = () => {
    // onerror 後必定會觸發 onclose，交給 onclose 處理
  };
}

// ── Canvas overlay ────────────────────────────────────────
function getBoxColor() {
  return S.currentType === 'thermal' ? '#ff8c42' : '#22bb77';
}

function drawBoxes() {
  updateTransport();
  const canvas = document.getElementById('overlay');
  const elW = els.video.offsetWidth  || 1;
  const elH = els.video.offsetHeight || 1;
  if (canvas.width  !== elW) canvas.width  = elW;
  if (canvas.height !== elH) canvas.height = elH;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, elW, elH);
  const vidW = els.video.videoWidth;
  const vidH = els.video.videoHeight;

  // In live mode, pick the bbox entry whose timestamp best matches the
  // wall-clock time of the frame actually on screen. Prefer hls.playingDate
  // (derived from the segment's #EXT-X-PROGRAM-DATE-TIME) — it travels with
  // the media, so network jitter only changes how much is buffered, never
  // which frame a given timestamp maps to. Fall back to (now - hls.latency)
  // only if PDT is unavailable; that estimate misses the server-side
  // pipeline delay (buffer + encode + segmentation) and runs ~3-5s ahead.
  let displayBoxes = S.latestBoxes;
  if (S.isLive && S.bboxHistory.length) {
    let targetTs = null;
    let dbgSrc = 'latest';
    let chosenTs = S.bboxHistory[S.bboxHistory.length - 1].ts;
    const pd = S.hls && S.hls.playingDate;
    if (pd && !isNaN(pd.getTime())) {
      targetTs = pd.getTime() / 1000;      // PDT≡真實擷取時間，不再減 offset
      dbgSrc = 'PDT';
    } else {
      const latency = (S.hls && S.hls.latency != null) ? S.hls.latency : 0;
      if (latency > 1) { targetTs = Date.now() / 1000 - latency; dbgSrc = 'latency'; }
    }
    if (targetTs != null) {
      let best = S.bboxHistory[S.bboxHistory.length - 1];
      let bestDist = Infinity;
      for (const entry of S.bboxHistory) {
        const d = Math.abs(entry.ts - targetTs);
        if (d < bestDist) { bestDist = d; best = entry; }
      }
      displayBoxes = best.boxes;
      chosenTs = best.ts;
    }
    if (window.__bboxDebug) {
      const now = Date.now() / 1000;
      S._dbg = {
        src: dbgSrc, now,
        latency: (S.hls && S.hls.latency != null) ? S.hls.latency : null,
        playingDate: (pd && !isNaN(pd.getTime())) ? pd.getTime() / 1000 : null,
        targetTs, chosenTs,
        newestTs: S.bboxHistory[S.bboxHistory.length - 1].ts,
        histLen: S.bboxHistory.length,
      };
    } else { S._dbg = null; }
  } else { S._dbg = null; }

  // 只顯示選取：有選取且開關開時，只畫選取的框；無選取則開關不生效（畫全部）。
  if (S.soloMode && S.selectedObjectId != null) {
    displayBoxes = displayBoxes.filter(o => o.object_id === S.selectedObjectId);
  }

  if (!vidW || !vidH || !displayBoxes.length) {
    drawDbgHud();
    S.animFrameId = requestAnimationFrame(drawBoxes);
    return;
  }
  const scale   = Math.min(elW / vidW, elH / vidH);
  const renderW = vidW * scale;
  const renderH = vidH * scale;
  const offX = (elW - renderW) / 2;
  const offY = (elH - renderH) / 2;
  const baseColor = getBoxColor();
  ctx.lineWidth = 1.5;
  ctx.font = 'bold 11px "DM Sans", monospace';

  for (const o of displayBoxes) {
    const [x, y, w, h] = o.bbox;
    const px = offX + x * scale;
    const py = offY + y * scale;
    const pw = w * scale;
    const ph = h * scale;
    const anomaly     = S.anomalyMap[o.object_id];
    const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);
    const color       = isAnomalous ? '#ff4444' : baseColor;

    // 選取強調：選取的框加粗全亮，其餘淡化（selectedObjectId 為 null 時不變）
    const isSel  = S.selectedObjectId != null && o.object_id === S.selectedObjectId;
    const dimmed = S.selectedObjectId != null && !isSel;
    ctx.save();
    if (dimmed) ctx.globalAlpha = 0.25;
    if (isSel)  ctx.lineWidth = 4;

    ctx.strokeStyle = color;
    ctx.fillStyle   = color;
    roundRect(ctx, px, py, pw, ph, 3);
    ctx.stroke();

    // 豬隻 ID 標籤
    const label = `#${o.object_id}`;
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = color;
    ctx.fillRect(px - 0.5, py - 16, tw + 6, 15);
    ctx.fillStyle = '#000';
    ctx.fillText(label, px + 2, py - 4);
    ctx.fillStyle = color;

    // 異常圖示（bbox 左下角）
    if (anomaly) {
      let icons = '';
      if (anomaly.activity_anomaly) icons += '⚠';
      if (anomaly.temp_anomaly)     icons += '🌡';
      if (icons) ctx.fillText(icons, px + 2, py + ph - 2);
    }
    ctx.restore();
  }
  drawDbgHud();
  S.animFrameId = requestAnimationFrame(drawBoxes);
}

// Diagnostic HUD for live bbox/stream sync — toggle with the 'd' key.
// Shows whether PDT (hls.playingDate) is actually driving alignment and
// how far the chosen bbox sits from the displayed frame's timestamp.
// 渲染成 DOM <pre>（可框選複製），而非畫進 canvas。每幀呼叫；
// _dbg 為 null（非 live / HUD 關）時隱藏，保留最後內容可複製。
let _hudEl = null;
function drawDbgHud() {
  if (!_hudEl) _hudEl = document.getElementById('bbox-hud');
  if (!_hudEl) return;
  const d = S._dbg;
  if (!d) { _hudEl.classList.remove('visible'); return; }
  const fmt = (v) => (v == null ? 'null' : (typeof v === 'number' ? v.toFixed(2) : v));
  const leadTarget = (d.targetTs != null) ? (d.chosenTs - d.targetTs) : null;
  const leadNewest = d.newestTs - (d.targetTs != null ? d.targetTs : d.now);
  _hudEl.textContent = [
    `src=${d.src}  hist=${d.histLen}`,
    `hls.playingDate=${fmt(d.playingDate)}`,
    `hls.latency=${fmt(d.latency)}`,
    `now=${fmt(d.now)}`,
    `targetTs=${fmt(d.targetTs)}`,
    `chosenBbox.ts=${fmt(d.chosenTs)}`,
    `newestBbox.ts=${fmt(d.newestTs)}`,
    `chosen-target=${fmt(leadTarget)}s`,
    `newest-target=${fmt(leadNewest)}s`,
  ].join('\n');
  _hudEl.classList.add('visible');
}

window.addEventListener('keydown', (e) => {
  if (e.key === 'd' && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName)) {
    window.__bboxDebug = !window.__bboxDebug;
    showToast(`bbox 同步診斷 HUD：${window.__bboxDebug ? '開' : '關'}`);
  }
});

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.quadraticCurveTo(x + w, y, x + w, y + r);
  ctx.lineTo(x + w, y + h - r);
  ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
  ctx.lineTo(x + r, y + h);
  ctx.quadraticCurveTo(x, y + h, x, y + h - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

S.animFrameId = requestAnimationFrame(drawBoxes);

// ── HLS stream ────────────────────────────────────────────
// 整點 rollover：後端 ffmpeg 換到新小時目錄、舊小時 playlist 凍結（不再
// append、無 ENDLIST）。前端定期重抓 /live，若 URL（含小時段）變了就
// loadSource 新小時 → 避免 live 卡死在凍結 playlist 直到手動重整。
async function checkLiveHandoff() {
  if (!S.isLive || !S.hls || !S.currentCamera) return;
  try {
    const res = await fetch(`/stream/${S.currentCamera}/live?type=${S.currentType}`);
    if (!res.ok) return;
    const live = await res.json();
    if (live.url && live.url !== S.currentLiveUrl) {
      S.currentLiveUrl = live.url;
      S.hls.loadSource(live.url);   // 沿用 attachMedia，重解析新小時 playlist
      els.video.play().catch(() => {});
    }
  } catch (_) { /* 暫時性網路錯誤，下個 tick 再試 */ }
}

// live 模式的兩個週期任務（異常地圖刷新 + 整點小時交接）統一管理，
// 避免散落的 setInterval/clearInterval 漏清。
export function startLiveTimers() {
  stopLiveTimers();
  S.liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
  S.liveHandoffIntervalId = setInterval(checkLiveHandoff, 12000);
}
export function stopLiveTimers() {
  clearInterval(S.liveAnomalyIntervalId); S.liveAnomalyIntervalId = null;
  clearInterval(S.liveHandoffIntervalId); S.liveHandoffIntervalId = null;
}

export async function loadStream() {
  if (!S.currentCamera) return;
  clearPigSelection();

  // 清理舊的 HLS instance
  if (S.hls) { S.hls.destroy(); S.hls = null; }
  els.video.src = '';
  hideNoSignal();
  // thermal 無來源：後端 /cameras active_types 判定（fail-open）。不建 HLS、不報錯誤 toast。
  if (S.currentType === 'thermal' && !hasActiveType(S.currentCamera, 'thermal')) {
    // S.cameraActiveTypes 只在 init()/enterGrid() 寫入，是一次 20s(ish) 前的快照。
    // 若 thermal 串流剛好卡在重連/ffmpeg 自癒中的瞬間開頁，會整個 session 誤判
    // 「硬體無熱成像來源」、切來切去也不會恢復。只在這條冷路徑（判定為無來源時）
    // 多打一次 /cameras 刷新後重判——不影響一般切換（有來源時完全不多打請求）。
    const camAtStart = S.currentCamera, typeAtStart = S.currentType;
    try {
      const res = await fetch('/cameras');
      if (res.ok) {
        const data = await res.json();
        S.cameraActiveTypes = data.active_types || {};
      }
    } catch (_) { /* 刷新失敗：維持原快照，走下面既有判定 */ }
    // 刷新期間使用者已切走攝影機/型別：放棄，交給新一輪 loadStream() 處理。
    if (S.currentCamera !== camAtStart || S.currentType !== typeAtStart) return;
    if (S.currentType === 'thermal' && !hasActiveType(S.currentCamera, 'thermal')) {
      // 文案不斷言硬體事實（可能只是暫時無串流），避免刷新後仍誤判時誤導使用者。
      showNoSignal('無訊號（目前無熱成像串流）');
      setStatus('Thermal 無訊號', '');
      connectWS(S.currentCamera);   // WS 仍照常（bbox 資料與畫面型別無關）
      return;
    }
    // 刷新後判定實際有來源：不 return，往下走正常載入流程。
  }
  setSkeleton(true);
  setStatus('正在連線...');
  connectWS(S.currentCamera);

  try {
    const res = await fetch(`/stream/${S.currentCamera}/live?type=${S.currentType}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const live = await res.json();
    const url = live.url;
    S.currentLiveUrl = url;

    if (Hls.isSupported()) {
      S.hls = new Hls({
        // ── 穩定播放（與後端 stable FPS 設計一致） ──
        lowLatencyMode: false,         // 不需要極低延遲
        liveSyncDurationCount: 3,      // buffer 3 個 segment（12s）讓播放更穩
        liveMaxLatencyDurationCount: 20,// 容忍較大延遲 → 使用者可往回拖約 80s 不被拉回
        maxBufferLength: 40,
        maxMaxBufferLength: 80,
        backBufferLength: 90,          // 保留 90s 已播放 buffer（給「回到幾分鐘前」用）
        // ── 網路抖動容忍 ──
        levelLoadingTimeOut: 10000,
        fragLoadingTimeOut: 20000,
        // ── 遇到 stall 時積極追趕 ──
        maxStarvationDelay: 4,
        maxLoadingDelay: 4,
      });

      S.hls.loadSource(url);
      S.hls.attachMedia(els.video);

      S.hls.on(Hls.Events.MANIFEST_PARSED, () => {
        els.video.play().catch(() => {});
        setSkeleton(false);
        setStatus('連線中', 'live');
      });

      S.hls.on(Hls.Events.LEVEL_LOADED, () => {
        // 每次 segment 載入成功就更新一下狀態（讓人知道串流仍在進行）
        if (els.statusEl.classList.contains('live')) {
          setStatus('連線中', 'live');
        }
      });

      S.hls.on(Hls.Events.ERROR, (_, data) => {
        if (data.fatal) {
          setSkeleton(true);
          if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
            setStatus('網路錯誤，嘗試重新載入...', '');
            setTimeout(() => S.hls && S.hls.startLoad(), 1500);
          } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
            setStatus('影像錯誤，嘗試修復...', '');
            S.hls.recoverMediaError();
          } else {
            setStatus(`串流錯誤：${data.details}`, 'error');
          }
        }
      });

      // video 恢復播放後隱藏 skeleton
      els.video.addEventListener('playing', () => setSkeleton(false), { once: false });
      els.video.addEventListener('waiting', () => setSkeleton(true),   { once: false });

    } else if (els.video.canPlayType('application/vnd.apple.mpegurl')) {
      // Safari native HLS
      els.video.src = url;
      els.video.play().catch(() => {});
      setSkeleton(false);
      setStatus('連線中', 'live');
    } else {
      setSkeleton(false);
      setStatus('瀏覽器不支援 HLS', 'error');
    }
  } catch (e) {
    setSkeleton(false);
    setStatus(`無法取得串流：${e.message}`, 'error');
  }
}

// ── Transport / scrubber bar ──────────────────────────────
// Range over which the user may scrub, expressed in *video time*.
function getSeekRange() {
  let start = 0, end = 0;
  if (!S.isLive && isFinite(els.video.duration) && els.video.duration > 0) {
    start = 0; end = els.video.duration;
  } else if (els.video.seekable && els.video.seekable.length) {
    start = els.video.seekable.start(0);
    end   = els.video.seekable.end(els.video.seekable.length - 1);
  } else if (isFinite(els.video.duration) && els.video.duration > 0) {
    end = els.video.duration;
  }
  const hi = end > start ? end : Infinity;
  const cur = Math.min(Math.max(els.video.currentTime || 0, start), hi);
  return { start, end, cur };
}

function updateTransport() {
  if (!els.transportEl) return;
  if (els.playIconUse) {
    els.playIconUse.setAttribute('href', els.video.paused ? '#i-play' : '#i-pause');
  }
  const { start, end, cur } = getSeekRange();
  const span = Math.max(end - start, 0.001);
  const frac = S.transportDragging ? S.dragFrac : (cur - start) / span;
  const cf   = Math.min(Math.max(frac, 0), 1);
  const pct  = (cf * 100) + '%';
  els.seekProgress.style.width = pct;
  els.seekHandle.style.left    = pct;

  let bEnd = start;
  for (let i = 0; i < els.video.buffered.length; i++) {
    if (els.video.buffered.start(i) <= cur + 0.25 && els.video.buffered.end(i) >= cur - 0.25) {
      bEnd = Math.max(bEnd, els.video.buffered.end(i));
    }
  }
  els.seekBuffered.style.width = (Math.min(Math.max((bEnd - start) / span, 0), 1) * 100) + '%';
  els.seekTrack.setAttribute('aria-valuenow', String(Math.round(cf * 100)));

  els.transportEl.classList.toggle('is-vod', !S.isLive);

  if (!S.isLive) {
    els.timeCurEl.textContent = fmtClock(cur - start);
    els.timeDurEl.textContent = fmtClock(span);
    els.timeCurEl.title = new Date((S.vodStartTs + cur) * 1000).toLocaleString('zh-TW');
    els.liveBtnT.dataset.state = 'vod';
    els.liveLabelT.textContent = '即時串流';
  } else {
    const ideal  = (S.hls && isFinite(S.hls.liveSyncPosition)) ? S.hls.liveSyncPosition : end;
    const behind = Math.max(ideal - cur, 0);
    if (behind > 4) {
      els.timeCurEl.textContent = '-' + fmtClock(behind);
      els.timeDurEl.textContent = 'LIVE';
      els.timeCurEl.title = '已往回 ' + fmtClock(behind);
      els.liveBtnT.dataset.state = 'behind';
      els.liveLabelT.textContent = '回到即時';
    } else {
      els.timeCurEl.textContent = 'LIVE';
      els.timeDurEl.textContent = 'LIVE';
      els.timeCurEl.title = '';
      els.liveBtnT.dataset.state = 'at-edge';
      els.liveLabelT.textContent = 'LIVE';
    }
  }
}

function seekToFraction(frac) {
  frac = Math.min(Math.max(frac, 0), 1);
  const { start, end } = getSeekRange();
  if (end <= start) return;
  let target = start + frac * (end - start);
  // Avoid seeking to the exact live edge — it stalls; back off ~0.5s.
  if (S.isLive && end - target < 1) target = Math.max(start, end - 0.5);
  try { els.video.currentTime = target; } catch (_) {}
  scheduleTrackingFetch(true);
}

function commitDragSeek() {
  if (S.seekCommitTimer) { S.dragSeekPending = true; return; }
  seekToFraction(S.dragFrac);
  S.seekCommitTimer = setTimeout(() => {
    S.seekCommitTimer = null;
    if (S.dragSeekPending) { S.dragSeekPending = false; commitDragSeek(); }
  }, 150);
}

function fracFromEvent(ev) {
  const rect = els.seekTrack.getBoundingClientRect();
  const clientX = ev.clientX != null ? ev.clientX
    : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : rect.left);
  return Math.min(Math.max((clientX - rect.left) / Math.max(rect.width, 1), 0), 1);
}

els.seekTrack.addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  S.transportDragging = true;
  els.seekTrack.classList.add('dragging');
  try { els.seekTrack.setPointerCapture(ev.pointerId); } catch (_) {}
  S.dragFrac = fracFromEvent(ev);
  commitDragSeek();
});
els.seekTrack.addEventListener('pointermove', (ev) => {
  if (!S.transportDragging) return;
  S.dragFrac = fracFromEvent(ev);
  commitDragSeek();
});
function endTransportDrag() {
  if (!S.transportDragging) return;
  S.transportDragging = false;
  els.seekTrack.classList.remove('dragging');
  clearTimeout(S.seekCommitTimer); S.seekCommitTimer = null; S.dragSeekPending = false;
  seekToFraction(S.dragFrac);
}
els.seekTrack.addEventListener('pointerup', endTransportDrag);
els.seekTrack.addEventListener('pointercancel', endTransportDrag);
els.seekTrack.addEventListener('lostpointercapture', endTransportDrag);

els.seekTrack.addEventListener('keydown', (ev) => {
  const { start, end, cur } = getSeekRange();
  const span = Math.max(end - start, 0.001);
  if      (ev.key === 'ArrowLeft')  seekToFraction(((cur - start) - 5) / span);
  else if (ev.key === 'ArrowRight') seekToFraction(((cur - start) + 5) / span);
  else if (ev.key === 'Home')       seekToFraction(0);
  else if (ev.key === 'End')        seekToFraction(1);
  else return;
  ev.preventDefault();
});

els.playBtn.addEventListener('click', () => {
  if (els.video.paused) els.video.play().catch(() => {}); else els.video.pause();
});

function goToLiveEdge() {
  if (S.hls && isFinite(S.hls.liveSyncPosition)) {
    try { els.video.currentTime = S.hls.liveSyncPosition; } catch (_) {}
  } else if (els.video.seekable && els.video.seekable.length) {
    try { els.video.currentTime = els.video.seekable.end(els.video.seekable.length - 1) - 0.5; } catch (_) {}
  }
  els.video.play().catch(() => {});
}

export function onLiveBtnClick() {
  if (!S.isLive) { switchToLive(); return; }
  goToLiveEdge();
}

// ── onclick 綁定（原 index.html inline onclick，改用 addEventListener） ──
document.querySelectorAll('.type-btn').forEach(btn => {
  btn.addEventListener('click', () => setType(btn.dataset.type));
});
els.liveBtn.addEventListener('click', switchToLive);
els.liveBtnT.addEventListener('click', onLiveBtnClick);
els.vodBannerLiveBtn.addEventListener('click', switchToLive);
