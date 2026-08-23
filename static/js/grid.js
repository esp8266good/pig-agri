// static/js/grid.js — 多畫面純監看。每格：live RGB（靜音、無 bbox），
// 資訊列：名稱＋追蹤數＋LIVE 狀態；有異常告警 → 紅框＋角標。
// 離開時銷毀全部播放器。點格 → main.js 切回單畫面選該攝影機。
//
// `_gridHourTs` 跨 leaveGrid() 存活（session 內記住上次選的回放時段，
// 下次進 grid 直接回到同一時段），與單畫面的 `S.isLive` 是兩套獨立的
// 回放狀態，不要混著推論。
//
// 時段選擇器（日期列＋月曆 popover）拆在 grid-timeline.js：本檔透過
// initGridTimeline() 注入 callback（onPickHour/getPlayingHour/getCams/getGen）
// 供其讀取播放狀態，並呼叫其匯出的 refreshGridCalendar()/renderGridDayBar()/
// setGridCalOpen() 觸發重繪；依賴方向 grid → grid-timeline，不建反向邊。
import { getJSON } from './api.js';
import { S, hasActiveType } from './state.js';
import { initGridTimeline, refreshGridCalendar, renderGridDayBar, setGridCalOpen } from './grid-timeline.js';

let _players = [];        // [{hls, video}]
let _anomalyTimer = null;
let _onPickCamera = null; // main.js 注入的 callback
let _gridGen = 0;         // generation token（Finding 4）：leaveGrid() 遞增使進行中的
                           // enterGrid() await 結束後偵測到過期、直接放棄不建 tiles

// ── grid 時段回放（共用時間軸，所有攝影機聯集）──────────────
let _cams = [];               // enterGrid 抓到的攝影機清單（rebuild 重用）
let _gridHourTs = null;       // null = LIVE；否則為回放小時的 epoch ts

initGridTimeline({
  onPickHour: hourTs => setGridPlayback(hourTs),
  getPlayingHour: () => _gridHourTs,
  getCams: () => _cams,
  getGen: () => _gridGen,
});

export function getGridPlaybackHour() { return _gridHourTs; }

export function bindGridPick(fn) { _onPickCamera = fn; }

// 共用 teardown：停 badge 輪詢、destroy 所有 hls 實例、清空 _players。
// enterGrid/leaveGrid/setGridPlayback 三處重建 tile 前都要做同一套清理。
function _teardownPlayers() {
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
}

export async function enterGrid() {
  const gen = ++_gridGen;
  // 自清舊狀態：type-switch 時 enterGrid() 會在已滿的 grid 上重跑（不經
  // leaveGrid()），root.innerHTML='' 只拔 DOM、不會自動 destroy hls 實例／
  // 清掉舊的 anomaly interval——沿用 leaveGrid() 同一套 teardown 才不會每切
  // 一次型別就多漏一批背景播放器與重複輪詢。首次進場／錯誤畫面重試時
  // _players 本來就是空的，這段是 no-op。
  _teardownPlayers();
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = false;
  let cameras;
  try {
    const data = await getJSON('/cameras');
    cameras = data.cameras;
    _cams = cameras;
    S.cameraActiveTypes = data.active_types || {};
  } catch (e) {
    if (gen !== _gridGen) return;   // 已被 leaveGrid/另一次 enterGrid 取代，不再顯示過期的錯誤畫面
    renderGridError(root);
    return;
  }
  if (gen !== _gridGen) return;     // 等待 /cameras 期間已離開 grid（Finding 4），放棄 append tiles
  root.style.setProperty('--grid-cols',
    cameras.length <= 2 ? 2 : cameras.length <= 4 ? 2 : 3);
  for (const cam of cameras) buildTile(root, cam, null, gen);
  if (_gridHourTs === null) {
    _anomalyTimer = setInterval(refreshTileBadges, 30000);
    refreshTileBadges();
  }
  document.getElementById('grid-timeline').hidden = false;
  updateGridPlaybackUI();
  refreshGridCalendar(gen);   // 非同步：抓齊聯集後 renderGridDayBar()
}

export function leaveGrid() {
  _gridGen++;   // 讓進行中的 enterGrid() resume 後 abort（見上）
  _teardownPlayers();
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = true;
  document.getElementById('grid-timeline').hidden = true;
  document.getElementById('grid-playback-banner').hidden = true;
  setGridCalOpen(false);
}

// Finding 2：/cameras 失敗時顯示錯誤佔位＋重試鈕，不留空白 grid／unhandled rejection。
function renderGridError(root) {
  root.innerHTML = '';
  const box = document.createElement('div');
  box.className = 'grid-error';
  const msg = document.createElement('p');
  msg.textContent = '無法取得攝影機清單';
  const btn = document.createElement('button');
  btn.className = 'grid-error-retry';
  btn.textContent = '重試';
  btn.addEventListener('click', () => enterGrid());
  box.append(msg, btn);
  root.appendChild(box);
}

// 切回放時段（null = 回 LIVE）：重建所有 tile。badge 輪詢只在 LIVE 跑。
function setGridPlayback(hourTs) {
  if (hourTs === _gridHourTs) return;
  _gridHourTs = hourTs;
  const gen = ++_gridGen;   // 使進行中的 buildTile await 過期
  _teardownPlayers();
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  for (const cam of _cams) buildTile(root, cam, null, gen);
  if (_gridHourTs === null) {
    _anomalyTimer = setInterval(refreshTileBadges, 30000);
    refreshTileBadges();
  }
  renderGridDayBar();
  updateGridPlaybackUI();
}

function updateGridPlaybackUI() {
  const banner = document.getElementById('grid-playback-banner');
  if (_gridHourTs === null) { banner.hidden = true; return; }
  const dt = new Date(_gridHourTs * 1000);
  const hh = String(dt.getHours()).padStart(2, '0');
  document.getElementById('grid-playback-text').textContent =
    `回放中：${dt.getMonth() + 1}/${dt.getDate()} ${hh}:00–${hh}:59`;
  banner.hidden = false;
}

// gen：呼叫端當下的 _gridGen（Finding 4）。root.insertBefore 之後、_players.push
// 之前都還有一個 await（GET /stream/{cam}/live），若這段期間 leaveGrid() 已經
// 遞增 _gridGen（tile 被拔掉、_players 被清空），resume 後必須直接 abort、
// 不能再建 hls／push 進 _players——否則會殘留背景播放器與輪詢（單純檢查
// enterGrid() 外層那次 await 不夠，buildTile 自己這次 await 才是真正會漏的窗口）。
async function buildTile(root, cam, beforeNode = null, gen = _gridGen) {
  const tile = document.createElement('div');
  tile.className = 'grid-tile';
  tile.dataset.cam = cam;
  tile.innerHTML = `
    <video muted playsinline></video>
    <div class="tile-info">
      <span class="tile-name"></span>
      <span class="tile-count" title="分析中豬隻數"></span>
      <span class="tile-live"><span class="status-dot"></span>LIVE</span>
    </div>
    <span class="tile-alert" hidden><svg class="icon"><use href="#i-alert"/></svg></span>`;
  if (_gridHourTs !== null) {
    const liveEl = tile.querySelector('.tile-live');
    liveEl.classList.remove('tile-live'); liveEl.classList.add('tile-vod');
    liveEl.textContent = '回放';
    tile.querySelector('.tile-count').textContent = '';
  }
  tile.querySelector('.tile-name').textContent = cam;
  // 「點格子會切到那台的單畫面」完全看不出來，滑鼠移上去只有邊框變色。
  // title 給桌機，data-help 給說明模式（tile 是動態產生的，initHelp() 那輪
  // 還不存在，所以要在這裡自己補上）。
  const tileHelp = '點一下切到這台攝影機的單畫面，那裡才有豬隻方框、以及保留／刪除影像的功能。';
  tile.title = tileHelp;
  tile.dataset.help = tileHelp;
  tile.addEventListener('click', () => _onPickCamera && _onPickCamera(cam));
  root.insertBefore(tile, beforeNode);   // beforeNode=null 時等同 appendChild，保持重試後原位置
  // 該型別無來源（如 thermal 攝影機沒裝）：預期狀態，無訊號佔位、不建播放器、無重試鈕。
  // active_types 是 live 概念（近期是否有送幀）；回放模式的訊號有無由該時段是否有
  // 錄影（VOD 404 探測）決定，不能沿用 live 的 active_types 判斷——否則目前離線／
  // 型別暫無來源的攝影機即使該小時確實有錄影，也會被這道守衛提早擋掉。
  // 只在 thermal 型別套用這道「預期無來源」守衛（與單畫面 player.js:loadStream()
  // 同一條規則對齊）：RGB 離線是異常（最常見的路徑就是下面 fetch 404），必須落到
  // tileError() 才有重試鈕；hasActiveType() 對離線攝影機回傳 `[]`（陣列），fail-open
  // 不生效，若不限定 thermal，離線 RGB 會被這裡提早攔截、永遠拿不到重試鈕。
  if (_gridHourTs === null && S.currentType === 'thermal' && !hasActiveType(cam, S.currentType)) { tileNoSignal(tile); return; }
  try {
    let url;
    if (_gridHourTs !== null) {
      // VOD tile：該攝影機該時段無錄影 → 404 → 無訊號（預期狀態，無重試鈕）
      const vodUrl = `/stream/${cam}/vod?start=${_gridHourTs}&end=${_gridHourTs + 3600}&type=${S.currentType}`;
      const probe = await fetch(vodUrl);
      if (gen !== _gridGen) return;
      if (probe.status === 404) { tileNoSignal(tile); return; }
      if (!probe.ok) throw new Error(`HTTP ${probe.status}`);
      url = vodUrl;
    } else {
      const live = await getJSON(`/stream/${cam}/live?type=${S.currentType}`);
      if (gen !== _gridGen) return;
      url = live.url;
    }
    const video = tile.querySelector('video');
    // 每格自己貼合該路串流的比例，理由同 player.js 的 syncVideoAspect()：
    // 寫死 4/3 而串流是 16:9，每一格上下都白白黑掉四分之一的高度。
    const fitTile = () => {
      if (video.videoWidth && video.videoHeight)
        tile.style.setProperty('--video-ar', `${video.videoWidth} / ${video.videoHeight}`);
    };
    video.addEventListener('loadedmetadata', fitTile);
    video.addEventListener('resize', fitTile);
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
                            maxBufferLength: 20 });
      hls.loadSource(url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) tileError(tile, cam); });
      _players.push({ hls, video });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = url; video.play().catch(() => {});
      _players.push({ hls: null, video });
    }
  } catch (_) {
    if (gen !== _gridGen) return;   // 過期：不在已拔掉的 tile 上顯示「無訊號」佔位
    // 攝影機斷線最常見的路徑就是這裡（GET /stream/{cam}/live 404 等）——
    // 沿用 tileError()（同一份「無訊號＋重試鈕」邏輯），讓使用者不用離開
    // grid 再進來才能重試，直接點格內按鈕重建該格播放器。
    tileError(tile, cam);
  }
}

function tileOffline(tile) {
  tile.classList.add('offline');
  tile.querySelector('.tile-live, .tile-vod').innerHTML = '無訊號';
}

// 「無訊號」＝預期狀態（無來源/該時段無錄影）：不給重試鈕，與 tileError（異常）區分。
function tileNoSignal(tile) {
  destroyTilePlayer(tile);
  tileOffline(tile);
  tile.classList.add('nosignal');
}

// fatal 的 hls 實例即時 destroy 並自 _players 移除，不留到 leaveGrid/retry 才清。
function destroyTilePlayer(tile) {
  const video = tile.querySelector('video');
  const idx = _players.findIndex(p => p.video === video);
  if (idx !== -1) {
    try { _players[idx].hls?.destroy(); } catch (_) {}
    _players.splice(idx, 1);
  }
}

function tileError(tile, cam) {
  destroyTilePlayer(tile);
  tileOffline(tile);
  if (tile.querySelector('.tile-retry')) return;
  const btn = document.createElement('button');
  btn.className = 'tile-retry';
  btn.textContent = '重試';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    tile.classList.remove('offline'); btn.remove();
    tile.querySelector('.tile-live, .tile-vod').innerHTML =
      '<span class="status-dot"></span>LIVE';
    await rebuildTilePlayer(tile, cam);
  });
  tile.appendChild(btn);
}

async function rebuildTilePlayer(tile, cam) {
  destroyTilePlayer(tile);
  const root = document.getElementById('grid-view');
  const nextSibling = tile.nextSibling;   // 記錄原位置，重建後插回原處而非跳到尾端
  tile.remove();
  await buildTile(root, cam, nextSibling, _gridGen);
}

async function refreshTileBadges() {
  const gen = _gridGen;   // M-2：切走 grid（型別切換/離開/換時段）後別再打完剩餘 tile 的 /alerts/active
  for (const tile of document.querySelectorAll('.grid-tile:not(.offline)')) {
    if (gen !== _gridGen) return;
    const cam = tile.dataset.cam;
    try {
      const data = await getJSON(`/alerts/active?camera_id=${cam}`);
      const cache = data.cache?.[cam] ?? {};
      const entries = Object.values(cache);
      tile.querySelector('.tile-count').textContent = `${entries.length} 隻`;
      const hasAnomaly = entries.some(e => e.activity_anomaly || e.temp_anomaly);
      tile.classList.toggle('alerting', hasAnomaly);
      tile.querySelector('.tile-alert').hidden = !hasAnomaly;
    } catch (_) {}
  }
}

{
  const liveBtn = document.getElementById('grid-live-btn');
  if (liveBtn) liveBtn.addEventListener('click', () => setGridPlayback(null));
}
