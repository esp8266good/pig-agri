// static/js/grid.js — 多畫面純監看。每格：live RGB（靜音、無 bbox），
// 資訊列：名稱＋追蹤數＋LIVE 狀態；有異常告警 → 紅框＋角標。
// 離開時銷毀全部播放器。點格 → main.js 切回單畫面選該攝影機。
import { getJSON } from './api.js';
import { S, hasActiveType } from './state.js';

let _players = [];        // [{hls, video}]
let _anomalyTimer = null;
let _onPickCamera = null; // main.js 注入的 callback
let _gridGen = 0;         // generation token（Finding 4）：leaveGrid() 遞增使進行中的
                           // enterGrid() await 結束後偵測到過期、直接放棄不建 tiles

// ── grid 時段回放（共用時間軸，所有攝影機聯集）──────────────
let _cams = [];               // enterGrid 抓到的攝影機清單（rebuild 重用）
let _gridHourTs = null;       // null = LIVE；否則為回放小時的 epoch ts
let _gridDay = null;          // 選中日 00:00 epoch
let _gridMonth = null;        // Date（該月 1 日）
let _unionHours = new Set();  // 該月「任一攝影機有錄影」的小時 ts 聯集

function localDayStart(date) {
  const d = new Date(date); d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

export function getGridPlaybackHour() { return _gridHourTs; }

export function bindGridPick(fn) { _onPickCamera = fn; }

export async function enterGrid() {
  const gen = ++_gridGen;
  // 自清舊狀態：type-switch 時 enterGrid() 會在已滿的 grid 上重跑（不經
  // leaveGrid()），root.innerHTML='' 只拔 DOM、不會自動 destroy hls 實例／
  // 清掉舊的 anomaly interval——沿用 leaveGrid() 同一套 teardown 才不會每切
  // 一次型別就多漏一批背景播放器與重複輪詢。首次進場／錯誤畫面重試時
  // _players 本來就是空的，這段是 no-op。
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
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
  if (_gridDay === null) {
    const today = new Date();
    _gridDay = localDayStart(today);
    _gridMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  }
  root.style.setProperty('--grid-cols',
    cameras.length <= 2 ? 2 : cameras.length <= 4 ? 2 : 3);
  for (const cam of cameras) buildTile(root, cam, null, gen);
  if (_gridHourTs === null) {
    _anomalyTimer = setInterval(refreshTileBadges, 30000);
    refreshTileBadges();
  }
  document.getElementById('grid-timeline').hidden = false;
  updateGridPlaybackUI();
  loadGridCalendar(gen);   // 非同步：抓齊聯集後 renderGridDayBar()
}

export function leaveGrid() {
  _gridGen++;   // 讓進行中的 enterGrid() resume 後 abort（見上）
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
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
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
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
  tile.addEventListener('click', () => _onPickCamera && _onPickCamera(cam));
  root.insertBefore(tile, beforeNode);   // beforeNode=null 時等同 appendChild，保持重試後原位置
  // 該型別無來源（如 thermal 攝影機沒裝）：預期狀態，無訊號佔位、不建播放器、無重試鈕。
  // active_types 是 live 概念（近期是否有送幀）；回放模式的訊號有無由該時段是否有
  // 錄影（VOD 404 探測）決定，不能沿用 live 的 active_types 判斷——否則目前離線／
  // 型別暫無來源的攝影機即使該小時確實有錄影，也會被這道守衛提早擋掉。
  if (_gridHourTs === null && !hasActiveType(cam, S.currentType)) { tileNoSignal(tile); return; }
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

async function loadGridCalendar(gen) {
  const requestedMonth = _gridMonth;   // 換月 race（Task 7 Minor）：記錄當下月份，
                                        // Promise.all 期間若月份已再度切換，_gridGen
                                        // 不變（不誤傷並行中的 tile build），改比對月份
  const y = requestedMonth.getFullYear(), m = requestedMonth.getMonth();
  const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
  const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
  const sets = await Promise.all(_cams.map(async cam => {
    try {
      const { hours } = await getJSON(
        `/stream/${cam}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
      return hours;
    } catch (_) { return []; }
  }));
  if (gen !== _gridGen) return;
  if (_gridMonth !== requestedMonth) return;   // 換月 race：非目前顯示月份的回應直接丟棄
  _unionHours = new Set(sets.flat());
  renderGridCalendar();
  renderGridDayBar();
}

function gridDayHasData(dayTs) {
  for (let h = 0; h < 24; h++) if (_unionHours.has(dayTs + h * 3600)) return true;
  return false;
}

function renderGridCalendar() {
  const y = _gridMonth.getFullYear(), m = _gridMonth.getMonth();
  document.getElementById('grid-cal-label').textContent = `${y} 年 ${m + 1} 月`;
  const grid = document.getElementById('grid-cal-grid');
  grid.innerHTML = '';
  const firstDow = new Date(y, m, 1).getDay();
  const daysInMonth = new Date(y, m + 1, 0).getDate();
  for (let i = 0; i < firstDow; i++) {
    const e = document.createElement('div');
    e.className = 'cal-day empty';
    grid.appendChild(e);
  }
  for (let d = 1; d <= daysInMonth; d++) {
    const dayTs = Math.floor(new Date(y, m, d).getTime() / 1000);
    const cell = document.createElement('div');
    cell.className = 'cal-day in-month';
    cell.textContent = d;
    if (gridDayHasData(dayTs)) cell.classList.add('has-rec');
    if (dayTs === _gridDay) cell.classList.add('day-selected');
    cell.addEventListener('click', () => {
      _gridDay = dayTs;
      renderGridCalendar(); renderGridDayBar(); updateGridDateLabel();
      setGridCalOpen(false);
    });
    grid.appendChild(cell);
  }
  const thisMonthFirst = new Date();
  thisMonthFirst.setDate(1); thisMonthFirst.setHours(0, 0, 0, 0);
  document.getElementById('grid-next-month').disabled =
    new Date(y, m, 1) >= thisMonthFirst;
}

function renderGridDayBar() {
  const bar = document.getElementById('grid-timeline-bar');
  bar.innerHTML = '';
  for (let h = 0; h < 24; h++) {
    const slotTs = _gridDay + h * 3600;
    const hasData = _unionHours.has(slotTs);
    const slot = document.createElement('div');
    slot.className = 'grid-slot' + (hasData ? ' has-data' : '');
    slot.setAttribute('role', 'listitem');
    slot.textContent = String(h).padStart(2, '0');
    slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
    if (slotTs === _gridHourTs) slot.classList.add('playing');
    if (hasData) slot.addEventListener('click', () => setGridPlayback(slotTs));
    bar.appendChild(slot);
  }
  updateGridDateLabel();
}

function updateGridDateLabel() {
  const d = new Date(_gridDay * 1000);
  document.getElementById('grid-date-btn-label').textContent =
    `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
}

async function refreshTileBadges() {
  for (const tile of document.querySelectorAll('.grid-tile:not(.offline)')) {
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

// ── grid 月曆 popover（一次性綁定；元素常駐 DOM，hidden 控制顯示）──────
function setGridCalOpen(open) {
  const pop = document.getElementById('grid-calendar');
  const btn = document.getElementById('grid-date-btn');
  pop.hidden = !open;
  btn.setAttribute('aria-expanded', String(open));
  if (open) setTimeout(() => document.addEventListener('click', _onGridCalOutside), 0);
  else document.removeEventListener('click', _onGridCalOutside);
}
function _onGridCalOutside(e) {
  const pop = document.getElementById('grid-calendar');
  const btn = document.getElementById('grid-date-btn');
  if (!pop.contains(e.target) && !btn.contains(e.target)) setGridCalOpen(false);
}
document.getElementById('grid-date-btn').addEventListener('click', () => {
  setGridCalOpen(document.getElementById('grid-calendar').hidden);
});
document.getElementById('grid-prev-month').addEventListener('click', () => {
  _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() - 1, 1);
  loadGridCalendar(_gridGen);
});
document.getElementById('grid-next-month').addEventListener('click', () => {
  _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() + 1, 1);
  loadGridCalendar(_gridGen);
});
document.getElementById('grid-live-btn').addEventListener('click', () => setGridPlayback(null));
