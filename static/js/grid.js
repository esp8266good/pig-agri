// static/js/grid.js — 多畫面純監看。每格：live RGB（靜音、無 bbox），
// 資訊列：名稱＋追蹤數＋LIVE 狀態；有異常告警 → 紅框＋角標。
// 離開時銷毀全部播放器。點格 → main.js 切回單畫面選該攝影機。
import { getJSON } from './api.js';

let _players = [];        // [{hls, video}]
let _anomalyTimer = null;
let _onPickCamera = null; // main.js 注入的 callback
let _gridGen = 0;         // generation token（Finding 4）：leaveGrid() 遞增使進行中的
                           // enterGrid() await 結束後偵測到過期、直接放棄不建 tiles

export function bindGridPick(fn) { _onPickCamera = fn; }

export async function enterGrid() {
  const gen = ++_gridGen;
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = false;
  let cameras;
  try {
    ({ cameras } = await getJSON('/cameras'));
  } catch (e) {
    if (gen !== _gridGen) return;   // 已被 leaveGrid/另一次 enterGrid 取代，不再顯示過期的錯誤畫面
    renderGridError(root);
    return;
  }
  if (gen !== _gridGen) return;     // 等待 /cameras 期間已離開 grid（Finding 4），放棄 append tiles
  root.style.setProperty('--grid-cols',
    cameras.length <= 2 ? 2 : cameras.length <= 4 ? 2 : 3);
  for (const cam of cameras) buildTile(root, cam);
  _anomalyTimer = setInterval(refreshTileBadges, 30000);
  refreshTileBadges();
}

export function leaveGrid() {
  _gridGen++;   // 讓進行中的 enterGrid() resume 後 abort（見上）
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = true;
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

async function buildTile(root, cam, beforeNode = null) {
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
  tile.querySelector('.tile-name').textContent = cam;
  tile.addEventListener('click', () => _onPickCamera && _onPickCamera(cam));
  root.insertBefore(tile, beforeNode);   // beforeNode=null 時等同 appendChild，保持重試後原位置
  try {
    const live = await getJSON(`/stream/${cam}/live?type=rgb`);
    const video = tile.querySelector('video');
    if (window.Hls && Hls.isSupported()) {
      const hls = new Hls({ lowLatencyMode: false, liveSyncDurationCount: 3,
                            maxBufferLength: 20 });
      hls.loadSource(live.url);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) tileError(tile, cam); });
      _players.push({ hls, video });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = live.url; video.play().catch(() => {});
      _players.push({ hls: null, video });
    }
  } catch (_) {
    // 攝影機斷線最常見的路徑就是這裡（GET /stream/{cam}/live 404 等）——
    // 沿用 tileError()（同一份「無訊號＋重試鈕」邏輯），讓使用者不用離開
    // grid 再進來才能重試，直接點格內按鈕重建該格播放器。
    tileError(tile, cam);
  }
}

function tileOffline(tile) {
  tile.classList.add('offline');
  tile.querySelector('.tile-live').innerHTML = '無訊號';
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
    tile.querySelector('.tile-live').innerHTML =
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
  await buildTile(root, cam, nextSibling);
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
