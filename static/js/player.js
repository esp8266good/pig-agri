// static/js/player.js — HLS 播放（live / VOD）、WS 追蹤連線、canvas bbox 疊圖、transport scrubber。
import { drawMaskRegions } from './mask.js';
import { S, els, MAX_WS_RETRY, WS_RETRY_BASE_MS, setStatus, showToast, setSkeleton, fmtClock, hasActiveType } from './state.js';
import { clearPigSelection, renderPigStatus, updateVodAnomalyMap, refreshAnomalyMap, refreshNotifications } from './panels.js';

// 允許採用「擷取時間略晚於畫面時間」的 bbox（PDT 內插與 WS 抵達的抖動）。
const BBOX_MATCH_TOLERANCE = 0.5;

// tracker 整幀吐不出已確認軌跡（objects 空陣列）時回頭沿用舊框。
//
// 固定窗口不管取多大都會留下一個「突然不見」的瞬間，只是把它推遠——實測
// 空洞 p90 是 4~11.5 秒、最大到 34 秒，取 15 秒仍會在長尾斷掉。改成透明度
// 隨沿用時間連續遞減：沿用愈久畫得愈淡，資訊愈不可靠這件事直接反映在視覺上，
// 過程中沒有任何一刻是「突然消失」。
//
// 為什麼敢沿用這麼久：空洞期間沒有新幀，HLS writer 會重送同一張影像（見下方
// 零階保持說明），螢幕上的畫面根本沒變，框也就沒有跑掉。淡化表達的是「無法
// 確認 tracker 是否仍認得這些豬」，不是「框的位置可能錯了」。
const BBOX_HELD_ALPHA_NEW = 0.5;    // 剛開始沿用時的透明度
const BBOX_HELD_ALPHA_OLD = 0.15;   // 淡到底的透明度（仍看得見輪廓）
const BBOX_FADE_SECONDS = 30.0;     // 從 NEW 淡到 OLD 所需秒數
// 超過這麼久就完全不畫。涵蓋觀測到的最長空洞（34 秒）仍有餘裕；再久代表
// 不是空洞而是真的斷了，這時留白才誠實。
const BBOX_EMPTY_HOLD_SECONDS = 60.0;

// 最後一次收到「非空」偵測的擷取時間，用來讓豬隻清單／計數在空訊息時沿用
// 而不是跟著閃。切換攝影機／進 VOD 時和 bboxHistory 一起歸零。
let _lastNonEmptyTs = 0;

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
  _lastNonEmptyTs = 0;
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
  _lastNonEmptyTs = 0;
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
      const objs = data.objects || [];
      const ts = data.timestamp || Date.now() / 1000;
      if (data.timestamp) {
        const delay = Date.now() - data.timestamp * 1000;
        els.latencyChip.style.display = '';
        els.latencyVal.textContent = Math.round(delay);
        // Buffer bbox with timestamp for HLS live latency alignment.
        // Keep enough history to cover the HLS back-buffer (~90s) so
        // scrubbing back in live mode still has matching boxes.
        // 這裡一定要記錄「真實觀測」（含空的）：drawBoxes 靠「這筆是不是空的」
        // 決定要不要沿用，先在這裡補過就分不出來了。
        S.bboxHistory.push({ ts: data.timestamp, boxes: objs });
        if (S.bboxHistory.length > 1000) S.bboxHistory.shift();
      }
      // 豬隻清單與計數：空訊息不清空，沿用最後一次非空的結果（理由同 bbox
      // 沿用——低 fps 相機 tracker 常整幀吐不出已確認軌跡）。否則清單會在
      // 「目前無偵測到豬隻」與正常之間反覆跳。
      if (objs.length) {
        S.latestBoxes = objs;
        _lastNonEmptyTs = ts;
      } else if (!_lastNonEmptyTs || ts - _lastNonEmptyTs > BBOX_EMPTY_HOLD_SECONDS) {
        S.latestBoxes = [];
      }
      els.countBadge.textContent = S.latestBoxes.length;
      S.currentObjectIds = new Set(S.latestBoxes.map(o => o.object_id));
      renderPigStatus();
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
// 一般豬（沒有任何標籤）的框色。以前 rgb 畫 #22bb77 綠、thermal 畫 #ff8c42 橘，
// 但那兩個顏色分別跟 reference 的 #3ecf8e 綠、lowest 的 #ff9a3c 橘幾乎一樣：
// 畫面上同時出現時根本分不出哪隻是被挑出來的。改成中性灰白之後，飽和色一律
// 只代表「這隻豬有事」，看到彩色就是有意義。
const NEUTRAL_BOX_COLOR = '#c9d1cd';
// 灰白框壓在熱像的橘紅底、或 rgb 的淺色地板上都會糊掉，所以每個框先描一圈
// 暗色再畫本色。紅框壓在深色墊料上也吃到同樣的好處。
const BOX_HALO_COLOR = 'rgba(0,0,0,0.55)';
// 'ghost' 模式下一般框的透明度。低到不干擾，又還看得見輪廓。
const GHOST_BOX_ALPHA = 0.18;

// ── 影格比例 ──────────────────────────────────────────────
// #video-wrap 以前寫死 aspect-ratio: 4/3，但相機送出來的是 1280x720（16:9），
// contain 之後上下各留一條黑邊，等於整個畫框有四分之一的高度是黑的。
// 改成量 video 真正的 videoWidth/videoHeight 寫回 CSS 變數，畫框就剛好貼合影像、
// 一條黑邊都不留；換成別的比例的相機也自動跟上，不必再改 CSS。
// metadata 還沒到之前用 16/9 當預設，避免載入瞬間畫框跳動。
export function syncVideoAspect() {
  const w = els.video.videoWidth, h = els.video.videoHeight;
  if (!w || !h) return;
  // 寫在 :root 而不是 #video-wrap 上：高度受限時要縮的是整張 .video-card
  // （卡片貼合影片，transport 與頁尾才會跟影片切齊；只縮畫框的話卡片還是滿版，
  // 影片兩側留下的空卡片跟黑邊一樣是浪費）。CSS 變數只往下繼承，
  // 掛在畫框上的話它的祖先卡片讀不到。
  const root = document.documentElement.style;
  root.setProperty('--main-video-ar', `${w} / ${h}`);
  // 數值版本：calc() 不能拿 `16 / 9` 這種 aspect-ratio 值來乘。
  root.setProperty('--main-video-ar-num', String(w / h));
}
els.video.addEventListener('loadedmetadata', syncVideoAspect);
els.video.addEventListener('resize', syncVideoAspect);   // 換相機/換 rendition 時比例可能變

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
  // 這批框沿用了多久（秒）。null = 不是沿用的（即時觀測或零階保持）。
  let heldAgeSec = null;
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
      // 零階保持，不是最近鄰。畫面上顯示的是「擷取時間 <= targetTs 的最後
      // 一張真實幀」：HLS writer 沒有新幀時會重送同一張影像並沿用它原本的
      // capture_ts（hls_manager._writer_tick），所以相機只送 1 fps 時同一張
      // 畫面會在螢幕上停留整整一秒。那一秒裡豬完全沒動，該幀的 bbox 一直是
      // 正確答案。用最近鄰的話會提前跳到「下一筆」——而下一筆若是空的，框
      // 就在畫面根本沒變的情況下憑空消失。
      let cur = null;
      for (const entry of S.bboxHistory) {
        if (entry.ts > targetTs + BBOX_MATCH_TOLERANCE) continue;
        if (!cur || entry.ts > cur.ts) cur = entry;
      }
      // targetTs 比整段歷史都舊（剛切攝影機／剛連上）→ 用最舊的一筆墊著。
      if (!cur) cur = S.bboxHistory[0];
      displayBoxes = cur.boxes;
      chosenTs = cur.ts;

      // 該幀 tracker 沒吐出任何已確認軌跡（低 fps 下 min_hits 難達成，見
      // docs 的追蹤缺口交接）。往回找最近一筆非空的沿用並淡化——這一段是
      // 推測而非觀測，跟上面的零階保持性質不同，所以要在視覺上區分開。
      if (!displayBoxes.length) {
        let held = null;
        for (const entry of S.bboxHistory) {
          if (!entry.boxes.length) continue;
          if (entry.ts > cur.ts) continue;
          // 一律以「畫面時間」量距離，與下面淡化用的 heldAgeSec 同一把尺；
          // 用 cur.ts 量會在長時間沒有新觀測時和淡化脫節（切掉的時機與淡到
          // 底的時機對不上）。
          if (targetTs - entry.ts > BBOX_EMPTY_HOLD_SECONDS) continue;
          if (!held || entry.ts > held.ts) held = entry;
        }
        if (held) {
          displayBoxes = held.boxes;
          chosenTs = held.ts;
          // 用畫面時間而非 cur.ts 量沿用時長：使用者實際感受到的「這批框放
          // 多久了」是相對於眼前的畫面，不是相對於最後一筆空觀測。
          heldAgeSec = Math.max(0, targetTs - held.ts);
        }
      }
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

  if (!vidW || !vidH) {
    drawDbgHud();
    S.animFrameId = requestAnimationFrame(drawBoxes);
    return;
  }
  if (!displayBoxes.length) {
    drawBoxCountChip(ctx, elH, 0, 0);
    drawDbgHud();
    S.animFrameId = requestAnimationFrame(drawBoxes);
    return;
  }
  const scale   = Math.min(elW / vidW, elH / vidH);
  const renderW = vidW * scale;
  const renderH = vidH * scale;
  const offX = (elW - renderW) / 2;
  const offY = (elH - renderH) / 2;
  // 遮罩疊圖畫在 bbox 之前，才不會蓋住框。預設關閉，除錯時才開。
  drawMaskRegions(ctx, { elW, elH, scale, offX, offY,
                         renderW, renderH });
  ctx.font = 'bold 11px "DM Sans", monospace';
  // 被 'focus' 濾掉、與被 'ghost' 淡化的一般框各數幾個。零異常時畫面會整片
  // 空白，這個數字是「偵測還活著」的唯一證據（見下方 drawBoxCountChip）。
  let plainCount = 0;

  for (const o of displayBoxes) {
    const [x, y, w, h] = o.bbox;
    const px = offX + x * scale;
    const py = offY + y * scale;
    const pw = w * scale;
    const ph = h * scale;
    const anomaly     = S.anomalyMap[o.object_id];
    const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);
    // 關注清單的三種標籤各有顏色。零異常時畫面上仍然有橘框與綠框，
    // 使用者才分得出「系統正常但沒事」與「系統掛了」。
    const focusLabel  = S.focusLabels[o.object_id] ?? null;
    // 選取強調：選取的框加粗全亮，其餘淡化（selectedObjectId 為 null 時不變）
    const isSel  = S.selectedObjectId != null && o.object_id === S.selectedObjectId;
    const dimmed = S.selectedObjectId != null && !isSel;
    // 使用者親手點選的那一隻永遠照畫：他要找的就是牠，被顯示模式濾掉會像壞了。
    const isKey  = isAnomalous || !!focusLabel || isSel;
    if (!isKey) {
      plainCount++;
      // 'focus' 直接不畫；'ghost' 畫但極淡；'all' 照常。VOD 沒有關注清單，
      // 所以在回放這條等於「只留異常的紅框」，也就是預設看到的樣子。
      if (S.boxDisplayMode === 'focus') continue;
    }
    const ghosted = !isKey && S.boxDisplayMode === 'ghost';
    const color = isAnomalous ? '#ff4444'
                : focusLabel === 'lowest'    ? '#ff9a3c'
                : focusLabel === 'reference' ? '#3ecf8e'
                : NEUTRAL_BOX_COLOR;

    ctx.save();
    ctx.lineWidth = 1.5;
    if (dimmed) ctx.globalAlpha = 0.25;
    if (ghosted) ctx.globalAlpha = Math.min(ctx.globalAlpha, GHOST_BOX_ALPHA);
    // 沿用的框畫淡一點；與 dimmed 疊加時取較小值，不會反而變亮。
    if (heldAgeSec != null) {
      // 沿用愈久畫得愈淡，連續遞減不會有「突然消失」的瞬間。
      const t = Math.min(1, heldAgeSec / BBOX_FADE_SECONDS);
      const a = BBOX_HELD_ALPHA_NEW + (BBOX_HELD_ALPHA_OLD - BBOX_HELD_ALPHA_NEW) * t;
      ctx.globalAlpha = Math.min(ctx.globalAlpha, a);
    }
    if (isSel)  ctx.lineWidth = 4;

    roundRect(ctx, px, py, pw, ph, 3);
    // 淡框不描暗邊：那圈黑線比框本身還顯眼，等於白淡化。
    if (!ghosted) {
      ctx.strokeStyle = BOX_HALO_COLOR;
      ctx.lineWidth  += 2;
      ctx.stroke();
      ctx.lineWidth  -= 2;
    }
    ctx.strokeStyle = color;
    ctx.fillStyle   = color;
    ctx.stroke();

    // 豬隻 ID 標籤。只有重點框才畫：每個框頂一塊實心色塊，在小螢幕上比框本身
    // 還吵。想知道某隻一般豬的 ID 就點右邊清單選取牠，選取的框一定會帶標籤。
    if (isKey) {
      const label = `#${o.object_id}`;
      const tw = ctx.measureText(label).width;
      ctx.fillStyle = BOX_HALO_COLOR;
      ctx.fillRect(px - 1.5, py - 17, tw + 8, 17);
      ctx.fillStyle = color;
      ctx.fillRect(px - 0.5, py - 16, tw + 6, 15);
      ctx.fillStyle = '#000';
      ctx.fillText(label, px + 2, py - 4);
      ctx.fillStyle = color;
    }

    // 異常圖示（bbox 左下角）
    if (anomaly) {
      let icons = '';
      if (anomaly.activity_anomaly) icons += '⚠';
      if (anomaly.temp_anomaly)     icons += '🌡';
      if (icons) ctx.fillText(icons, px + 2, py + ph - 2);
    }
    ctx.restore();
  }
  drawBoxCountChip(ctx, elH, displayBoxes.length, plainCount);
  drawDbgHud();
  S.animFrameId = requestAnimationFrame(drawBoxes);
}

// 左下角的一行小字。存在的理由只有一個：'focus' 模式把一般框全濾掉之後，
// 一個沒有異常的時段跟「偵測整個掛掉」在畫面上長得一模一樣。有了這個數字，
// 「已隱藏 12 個正常框」＝系統在數豬、只是沒事；「未偵測到豬隻」＝真的沒東西。
// 畫在 canvas 而不是另做 DOM：它要跟著影片實際的畫面區域走，而且左上角已經被
// bbox 同步診斷 HUD 佔住了。
function drawBoxCountChip(ctx, elH, totalBoxes, plainCount) {
  if (S.boxDisplayMode !== 'focus') return;   // 其餘模式框都看得見，不必再說
  // 「只顯示選取的豬」是使用者自己把畫面清空的，這時數字只會誤導。
  if (S.soloMode && S.selectedObjectId != null) return;
  if (totalBoxes > 0 && plainCount === 0) return;   // 沒有東西被藏起來
  const text = totalBoxes === 0 ? '未偵測到豬隻' : `已隱藏 ${plainCount} 個正常框`;
  ctx.save();
  ctx.font = '11px "DM Sans", sans-serif';
  const tw = ctx.measureText(text).width;
  const x = 6, y = elH - 22;
  ctx.fillStyle = 'rgba(0,0,0,0.55)';
  roundRect(ctx, x, y, tw + 12, 18, 4);
  ctx.fill();
  ctx.fillStyle = 'rgba(255,255,255,0.72)';
  ctx.fillText(text, x + 6, y + 13);
  ctx.restore();
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
