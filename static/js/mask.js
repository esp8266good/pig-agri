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
// 停用的區域不擋任何偵測框，所以不能用飽和色：這個專案的慣例是飽和色只代表
// 「這裡會發生事情」。它照樣要畫出來，因為它多半蓋著固定不動的背景物件，
// 正好是熱像對位最好用的基準。
const COLOR_OFF    = '#8a9490';
// 熱像畫面上「這塊遮罩在 RGB 上的位置」。中性灰白，與 bbox 的一般框同色。
const COLOR_RAW    = '#c9d1cd';

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

function strokePolygon(ctx, points, g, { fill, stroke, dashed = false, width = 2 }) {
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
  ctx.lineWidth = width;
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

// 遮罩座標是 RGB 畫面上的正規化位置。要畫到熱像上就得先過對位換算，
// 跟後端 thermal_align.map_box 同一條公式（也跟體溫取樣用的是同一組參數）。
function toThermal(pt) {
  const a = S.thermalAlign;
  return [a.off_x + pt[0] * a.scale_x, a.off_y + pt[1] * a.scale_y];
}

// ── 非編輯狀態的疊圖 ────────────────────────────────────────
// 預設關閉：遮罩正常運作時使用者不需要看到它，一直蓋著色塊只會干擾看豬。
//
// 熱像畫面上一塊區域畫兩條線：虛線灰白是它在 RGB 上的位置，實線是套過對位之後
// 的位置。兩條線各自貼齊同一個固定背景物件時，對位就對了。只畫一條的話看得到
// 框在哪，卻看不出該往哪邊推，等於還是在猜。
// 兩條線平常也照畫（不限校正模式）：在熱像上看到它們分得很開，就是這台相機
// 對位歪掉的免費警示，不必特地進校正模式才發現。
export function drawMaskRegions(ctx, g) {
  // 校正模式一律顯示，不管那個勾勾有沒有勾：遮罩就是這個模式最好用的對位基準，
  // 進來卻看不到它等於這個功能不存在。
  if ((!S.showMaskOverlay && !S.alignEditing) || S.maskEditing) return;
  const onThermal = S.currentType === 'thermal';
  for (const region of S.maskRegions) {
    const pts = region.points || [];
    if (pts.length < 3) continue;
    if (onThermal) {
      strokePolygon(ctx, pts, g,
                    { stroke: COLOR_RAW, dashed: true, width: 1 });
    }
    const shown = onThermal ? pts.map(toThermal) : pts;
    if (region.enabled) {
      strokePolygon(ctx, shown, g, { fill: COLOR_FILL, stroke: COLOR_STROKE });
    } else {
      strokePolygon(ctx, shown, g, { stroke: COLOR_OFF, width: 1 });
    }
  }
}

export async function loadMaskRegions() {
  if (!S.currentCamera) return;
  try {
    const d = await fetch(`/masks/${S.currentCamera}`).then(r => r.json());
    S.maskRegions = d.regions || [];
  } catch (_) { S.maskRegions = []; }
}

// 換相機的當下就丟掉上一台的遮罩，不等 fetch 回來：那兩支 fetch 是非同步的，
// 空窗期間畫面上畫的會是前一台相機的遮罩，畫在一個完全不相干的畫面上。
export function clearMaskRegions() { S.maskRegions = []; }

// ── 編輯器 ──────────────────────────────────────────────────
let _draft = [];          // 正在畫的多邊形（還沒收成一塊區域）
let _rafId = null;
// 開啟編輯器當下的內容，用來判斷「有沒有改過」。存檔與離開都要問，但只在真的
// 改過時才問：無條件跳確認很快就會被當成雜訊按掉，那等於沒有防呆。
let _openSnapshot = null;

function serialize(regions) {
  return JSON.stringify((regions || []).map(r => ({
    label: r.label || '', enabled: !!r.enabled, points: r.points,
  })));
}

function isDirty() {
  return _openSnapshot !== null
      && (serialize(S.maskRegions) !== _openSnapshot || _draft.length > 0);
}

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
  // 遮罩的座標是 RGB 畫面上的位置（偵測跑在 RGB 上）。對著熱像畫面點，點出來的
  // 每一個點都會偏掉一整個對位的量，而且畫完當下看起來完全正常。
  if (S.currentType !== 'rgb') {
    showToast('請先切到 RGB 畫面再編輯遮罩');
    return;
  }
  // 畫多邊形必須看得到真實畫面，所以入口雖然在設定抽屜，按下去要把抽屜收起來。
  closeSettingsDrawer();
  await loadMaskRegions();
  // 編輯器直接畫在正在播的畫面上，不另外抓截圖：live 本來就在播，
  // 而截圖端點還得處理「相機斷線時回什麼」。
  S.maskEditing = true;
  _draft = [];
  _openSnapshot = serialize(S.maskRegions);
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
  _openSnapshot = null;
  const canvas = canvasEl();
  if (canvas) {
    canvas.removeEventListener('click', onCanvasClick);
    canvas.setAttribute('hidden', '');
    const g = geometry(canvas);
    canvas.getContext('2d').clearRect(0, 0, g.elW, g.elH);
  }
  document.getElementById('mask-editor')?.setAttribute('hidden', '');
}

// 換相機、或任何要離開這台相機的動作，都先問過這裡。
// 回傳 false = 使用者反悔，呼叫端要原地不動（連 select 的值都要復原）。
export function canLeaveMaskEditor() {
  if (!S.maskEditing) return true;
  if (isDirty() && !confirm('遮罩有未儲存的改動，換相機會丟掉，確定嗎？')) return false;
  closeEditor();
  return true;
}

async function saveMasks() {
  // 遮罩是唯一會直接改變偵測結果的前端設定：存下去的下一幀就開始丟框，而被丟掉
  // 的框不會留下任何痕跡。確認擋在這一步，因為在編輯器裡怎麼畫都還沒碰到推論。
  const before = JSON.parse(_openSnapshot || '[]').length;
  const after  = S.maskRegions.length;
  const enabled = S.maskRegions.filter(r => r.enabled).length;
  if (!confirm(
        `要更新「${S.currentCamera}」的遮罩嗎？\n\n` +
        `${before} 塊 → ${after} 塊（其中 ${enabled} 塊會擋掉偵測框）\n` +
        `存檔後下一幀立即生效。`)) return;
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
      if (isDirty() && !confirm('遮罩有未儲存的改動，要放棄嗎？')) return;
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
