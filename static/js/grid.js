// static/js/grid.js — 多畫面純監看。每格：live RGB（靜音、無 bbox），
// 資訊列：名稱＋追蹤數＋LIVE 狀態；有異常告警 → 紅框＋角標。
// 離開時銷毀全部播放器。點格 → main.js 切回單畫面選該攝影機。
import { getJSON } from './api.js';

let _players = [];        // [{hls, video}]
let _anomalyTimer = null;
let _onPickCamera = null; // main.js 注入的 callback

export function bindGridPick(fn) { _onPickCamera = fn; }

export async function enterGrid() {
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = false;
  const { cameras } = await getJSON('/cameras');
  root.style.setProperty('--grid-cols',
    cameras.length <= 2 ? 2 : cameras.length <= 4 ? 2 : 3);
  for (const cam of cameras) buildTile(root, cam);
  _anomalyTimer = setInterval(refreshTileBadges, 30000);
  refreshTileBadges();
}

export function leaveGrid() {
  clearInterval(_anomalyTimer); _anomalyTimer = null;
  for (const p of _players) { try { p.hls?.destroy(); } catch (_) {} }
  _players = [];
  const root = document.getElementById('grid-view');
  root.innerHTML = '';
  root.hidden = true;
}

async function buildTile(root, cam) {
  const tile = document.createElement('div');
  tile.className = 'grid-tile';
  tile.dataset.cam = cam;
  tile.innerHTML = `
    <video muted playsinline></video>
    <div class="tile-info">
      <span class="tile-name"></span>
      <span class="tile-count" title="追蹤中豬隻數"></span>
      <span class="tile-live"><span class="status-dot"></span>LIVE</span>
    </div>
    <span class="tile-alert" hidden><svg class="icon"><use href="#i-alert"/></svg></span>`;
  tile.querySelector('.tile-name').textContent = cam;
  tile.addEventListener('click', () => _onPickCamera && _onPickCamera(cam));
  root.appendChild(tile);
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
    tileOffline(tile);
  }
}

function tileOffline(tile) {
  tile.classList.add('offline');
  tile.querySelector('.tile-live').innerHTML = '無訊號';
}

function tileError(tile, cam) {
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
  const video = tile.querySelector('video');
  const old = _players.find(p => p.video === video);
  if (old) { try { old.hls?.destroy(); } catch (_) {} _players = _players.filter(p => p !== old); }
  const root = document.getElementById('grid-view');
  tile.remove();
  await buildTile(root, cam);
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
