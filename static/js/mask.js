// static/js/mask.js — 遮罩編輯器與遮罩疊圖。
//
// 遮罩不會改變任何影像：錄影、直播、回放看到的畫面完全不受影響。它唯一的作用是
// 讓與它重疊超過 60% 的偵測框被丟掉、不進追蹤，用來擋掉走道、牆面這種
// 不該被當成豬的區域。座標一律存正規化的 0..1，換相機解析度不用重畫。
import { S, els, showToast } from './state.js';
import { closeSettingsDrawer } from './panels.js';

const COLOR_FILL   = 'rgba(255, 80, 80, 0.22)';
const COLOR_STROKE = '#ff5050';
const COLOR_DRAFT  = '#ffd166';

// 畫面在 <video> 元素裡是置中留黑邊的，點擊座標要先換算回「影像內的比例」，
// 否則畫出來的遮罩在不同視窗寬度下會跑掉。
function geometry(canvas) {
  const elW = els.video.offsetWidth  || 1;
  const elH = els.video.offsetHeight || 1;
  if (canvas.width  !== elW) canvas.width  = elW;
  if (canvas.height !== elH) canvas.height = elH;
  const vw = els.video.videoWidth  || elW;
  const vh = els.video.videoHeight || elH;
  const scale = Math.min(elW / vw, elH / vh);
  return {
    elW, elH, scale,
    offX: (elW - vw * scale) / 2,
    offY: (elH - vh * scale) / 2,
    renderW: vw * scale,
    renderH: vh * scale,
  };
}

function toCanvas(pt, g) {
  return [g.offX + pt[0] * g.renderW, g.offY + pt[1] * g.renderH];
}

function toNormalized(x, y, g) {
  const nx = (x - g.offX) / g.renderW;
  const ny = (y - g.offY) / g.renderH;
  // 夾在 0..1：後端會擋掉界外座標，前端先夾住比較不會讓使用者存不了檔。
  return [Math.min(1, Math.max(0, nx)), Math.min(1, Math.max(0, ny))];
}

function strokePolygon(ctx, points, g, { fill, stroke, dashed = false }) {
  if (points.length < 2) return;
  ctx.save();
  ctx.beginPath();
  points.forEach((pt, i) => {
    const [x, y] = toCanvas(pt, g);
    if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  });
  if (!dashed) ctx.closePath();
  if (fill) { ctx.fillStyle = fill; ctx.fill(); }
  ctx.strokeStyle = stroke;
  ctx.lineWidth = 2;
  if (dashed) ctx.setLineDash([6, 4]);
  ctx.stroke();
  ctx.restore();
}

function drawVertices(ctx, points, g, color) {
  ctx.save();
  ctx.fillStyle = color;
  for (const pt of points) {
    const [x, y] = toCanvas(pt, g);
    ctx.beginPath();
    ctx.arc(x, y, 5, 0, Math.PI * 2);
    ctx.fill();
  }
  ctx.restore();
}

// ── 非編輯狀態的疊圖 ────────────────────────────────────────
// 預設關閉：遮罩正常運作時使用者不需要看到它，一直蓋著色塊只會干擾看豬。
export function drawMaskRegions(ctx, g) {
  if (!S.showMaskOverlay || S.maskEditing) return;
  for (const region of S.maskRegions) {
    if (!region.enabled) continue;
    strokePolygon(ctx, region.points, g,
                  { fill: COLOR_FILL, stroke: COLOR_STROKE });
  }
}

export async function loadMaskRegions() {
  if (!S.currentCamera) return;
  try {
    const d = await fetch(`/masks/${S.currentCamera}`).then(r => r.json());
    S.maskRegions = d.regions || [];
  } catch (_) { S.maskRegions = []; }
}

// ── 編輯器 ──────────────────────────────────────────────────
let _draft = [];          // 正在畫的多邊形（還沒收成一塊區域）
let _rafId = null;

function canvasEl() { return document.getElementById('mask-canvas'); }

function renderLoop() {
  const canvas = canvasEl();
  if (!canvas || !S.maskEditing) { _rafId = null; return; }
  const g = geometry(canvas);
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, g.elW, g.elH);
  for (const region of S.maskRegions) {
    strokePolygon(ctx, region.points, g, {
      fill: region.enabled ? COLOR_FILL : 'rgba(128,128,128,0.15)',
      stroke: region.enabled ? COLOR_STROKE : '#888',
    });
    drawVertices(ctx, region.points, g, region.enabled ? COLOR_STROKE : '#888');
  }
  if (_draft.length) {
    strokePolygon(ctx, _draft, g, { stroke: COLOR_DRAFT, dashed: true });
    drawVertices(ctx, _draft, g, COLOR_DRAFT);
  }
  _rafId = requestAnimationFrame(renderLoop);
}

function renderRegionList() {
  const listEl = document.getElementById('mask-region-list');
  if (!listEl) return;
  listEl.innerHTML = '';
  if (!S.maskRegions.length) {
    listEl.innerHTML = '<li class="mask-empty">還沒有遮罩。在畫面上點出至少三個點，再按「完成這一塊」。</li>';
    return;
  }
  S.maskRegions.forEach((region, idx) => {
    const li = document.createElement('li');
    li.className = 'mask-region-item';

    const chk = document.createElement('input');
    chk.type = 'checkbox';
    chk.checked = region.enabled;
    chk.title = '停用這一塊（用來單獨排除某塊遮罩造成的問題）';
    chk.addEventListener('change', () => { region.enabled = chk.checked; });

    const name = document.createElement('input');
    name.type = 'text';
    name.value = region.label || '';
    name.placeholder = '名稱，例如「走道」';
    name.maxLength = 64;
    name.addEventListener('input', () => { region.label = name.value; });

    const del = document.createElement('button');
    del.textContent = '刪除';
    del.addEventListener('click', () => {
      S.maskRegions.splice(idx, 1);
      renderRegionList();
    });

    li.append(chk, name, del);
    listEl.appendChild(li);
  });
}

function onCanvasClick(e) {
  const canvas = canvasEl();
  const rect = canvas.getBoundingClientRect();
  const g = geometry(canvas);
  _draft.push(toNormalized(e.clientX - rect.left, e.clientY - rect.top, g));
}

function commitDraft() {
  if (_draft.length < 3) {
    showToast('至少要三個點才圍得出一塊區域');
    return;
  }
  S.maskRegions.push({ label: '', enabled: true, points: _draft });
  _draft = [];
  renderRegionList();
}

function undoPoint() {
  if (_draft.length) _draft.pop();
}

export async function openMaskEditor() {
  if (!S.currentCamera) return;
  if (!S.isLive) {
    showToast('請先回到 LIVE 再編輯遮罩');
    return;
  }
  // 畫多邊形必須看得到真實畫面，所以入口雖然在設定抽屜，按下去要把抽屜收起來。
  closeSettingsDrawer();
  await loadMaskRegions();
  // 編輯器直接畫在正在播的畫面上，不另外抓截圖：live 本來就在播，
  // 而截圖端點還得處理「相機斷線時回什麼」。
  S.maskEditing = true;
  _draft = [];
  document.getElementById('mask-editor')?.removeAttribute('hidden');
  const canvas = canvasEl();
  canvas.removeAttribute('hidden');
  canvas.addEventListener('click', onCanvasClick);
  renderRegionList();
  if (_rafId == null) renderLoop();
}

function closeEditor() {
  S.maskEditing = false;
  _draft = [];
  const canvas = canvasEl();
  if (canvas) {
    canvas.removeEventListener('click', onCanvasClick);
    canvas.setAttribute('hidden', '');
    const g = geometry(canvas);
    canvas.getContext('2d').clearRect(0, 0, g.elW, g.elH);
  }
  document.getElementById('mask-editor')?.setAttribute('hidden', '');
}

async function saveMasks() {
  const body = {
    regions: S.maskRegions.map(r => ({
      label: r.label || '', enabled: !!r.enabled, points: r.points,
    })),
  };
  try {
    const resp = await fetch(`/masks/${S.currentCamera}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      showToast(`儲存失敗：${d.detail || resp.status}`);
      return;
    }
    const d = await resp.json();
    showToast(`遮罩已儲存（${d.saved} 塊），立即生效`);
    closeEditor();
  } catch (_) { showToast('儲存失敗'); }
}

export function initMaskEditor() {
  document.getElementById('mask-edit-btn')
    ?.addEventListener('click', () => openMaskEditor());
  document.getElementById('mask-commit-btn')
    ?.addEventListener('click', commitDraft);
  document.getElementById('mask-undo-btn')
    ?.addEventListener('click', undoPoint);
  document.getElementById('mask-save-btn')
    ?.addEventListener('click', saveMasks);
  document.getElementById('mask-cancel-btn')
    ?.addEventListener('click', () => {
      closeEditor();
      loadMaskRegions();   // 丟掉未存的改動，回到 DB 裡的版本
    });
  const showChk = document.getElementById('mask-show-overlay');
  if (showChk) {
    showChk.checked = S.showMaskOverlay;
    showChk.addEventListener('change', e => {
      S.showMaskOverlay = e.target.checked;
      try { localStorage.setItem('showMaskOverlay', String(S.showMaskOverlay)); }
      catch (_) {}
    });
  }
}
