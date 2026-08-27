// static/js/align.js — 熱像對位校正。
//
// 熱像與 RGB 是兩顆分開的鏡頭：視角不一樣、鏡頭位置也差幾公分，所以同一隻豬在
// 兩張圖上不會落在同一個位置。bbox 是在 RGB 上算出來的，直接畫到熱像上會整體偏
// 掉一點。這裡讓人用眼睛把它推回去：拖曳畫面平移、按鈕縮放，看框對不對得上豬。
//
// ⚠ 存下來的四個數字同時決定兩件事：熱像畫面上框畫在哪，以及那隻豬的體溫從熱像
// 的哪一塊取樣（後端 `_compute_thermal_celsius` 用同一組參數）。所以校正不只是
// 讓畫面好看，它會改變寫進 DB 的體溫數值。
//
// ⛔ 沒有做自動校正。RGB 與熱像的成像原理不同（反射光 vs 輻射），灰階之間沒有
// 穩定的對應關係，一般的特徵點比對在這裡不成立；理論上可用互資訊配準，但熱像
// 只有 160x120、豬體在上面是一團沒有內部紋理的均勻亮區，而夜間 RGB 全黑根本沒
// 有影像可配。鏡頭是鎖死的，校正一次可以用很久，不值得養一條會安靜給出錯誤結果
// 的自動流程。
import { S, els, showToast } from './state.js';

const IDENTITY = { off_x: 0, off_y: 0, scale_x: 1, scale_y: 1 };
// 一次按鈕點擊調整多少。0.01 的平移在 640 寬的畫面上約 6px，看得出來又不會過頭。
const OFF_STEP = 0.01;
const SCALE_STEP = 0.02;
// 與後端 thermal_align.py 的上下限一致。前端先夾住，使用者才不會存不了檔。
const OFF_MIN = -0.5, OFF_MAX = 0.5;
const SCALE_MIN = 0.2, SCALE_MAX = 3.0;

// 進入校正模式時的原始值，取消時回到這裡。
let _snapshot = null;
let _dragging = false;
let _dragStart = null;
// 對位底圖：半透明疊在熱像上的那張 RGB 影像。只在校正模式存在。
let _underlay = null;
// 固定 0.35，不做成滑桿。多一顆旋鈕就多一個「怎麼調都對不準」的干擾變因，
// 而遮罩的雙線本來就是比底圖更精確的判準，底圖只是給眼睛一個粗略的錨。
const UNDERLAY_ALPHA = 0.35;

function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }

// 換相機的當下就丟掉上一台的對位參數，不等 fetch 回來（同 mask.js 的理由）。
// 拿 A 相機的參數去畫 B 相機的熱像，框會整批偏掉而且看起來像是校正沒做好。
export function clearThermalAlign() {
  S.thermalAlign = { ...IDENTITY };
  renderReadout();
}

export async function loadThermalAlign() {
  if (!S.currentCamera) return;
  try {
    const d = await fetch(`/thermal-align/${S.currentCamera}`).then(r => r.json());
    S.thermalAlign = { ...IDENTITY, ...(d.align || {}) };
  } catch (_) {
    // 讀不到就當作沒校正過。畫得跟改版前一樣，總比整片框消失好。
    S.thermalAlign = { ...IDENTITY };
  }
  renderReadout();
}

function renderReadout() {
  if (!els.alignReadout) return;
  const a = S.thermalAlign;
  els.alignReadout.textContent =
    `平移 ${a.off_x.toFixed(3)}, ${a.off_y.toFixed(3)}　` +
    `縮放 ${a.scale_x.toFixed(3)} × ${a.scale_y.toFixed(3)}`;
}

function nudge(patch) {
  const a = { ...S.thermalAlign };
  for (const [k, dv] of Object.entries(patch)) {
    const isOff = k.startsWith('off');
    a[k] = clamp(a[k] + dv, isOff ? OFF_MIN : SCALE_MIN, isOff ? OFF_MAX : SCALE_MAX);
  }
  S.thermalAlign = a;
  renderReadout();
}

// ── 拖曳平移 ────────────────────────────────────────────────
// 位移量要換算成「畫面比例」而不是像素：影片在 <video> 裡是置中留黑邊的，
// 用像素算的話同一個手勢在不同視窗寬度下會推得不一樣遠。
function videoRenderRect() {
  const elW = els.video.offsetWidth || 1;
  const elH = els.video.offsetHeight || 1;
  const vw = els.video.videoWidth || elW;
  const vh = els.video.videoHeight || elH;
  const scale = Math.min(elW / vw, elH / vh);
  return { renderW: vw * scale, renderH: vh * scale };
}

function onPointerDown(e) {
  if (!S.alignEditing) return;
  _dragging = true;
  _dragStart = { x: e.clientX, y: e.clientY, align: { ...S.thermalAlign } };
  e.target.setPointerCapture?.(e.pointerId);
  e.preventDefault();
}

function onPointerMove(e) {
  if (!_dragging || !_dragStart) return;
  const { renderW, renderH } = videoRenderRect();
  const dx = (e.clientX - _dragStart.x) / renderW;
  const dy = (e.clientY - _dragStart.y) / renderH;
  S.thermalAlign = {
    ...S.thermalAlign,
    off_x: clamp(_dragStart.align.off_x + dx, OFF_MIN, OFF_MAX),
    off_y: clamp(_dragStart.align.off_y + dy, OFF_MIN, OFF_MAX),
  };
  renderReadout();
}

function onPointerUp() { _dragging = false; _dragStart = null; }

// ── 對位底圖 ────────────────────────────────────────────────
// 取的是「現在」的 live RGB，不是回放中那個時間點的畫面。鏡頭鎖死不會動，
// 所以今天的 live 跟上週的錄影拍到的是同一片固定背景；校正的是一組幾何關係，
// 兩張圖不必同時間。要對到任意時間點就得為一張幀另開 ffmpeg 解 .ts。
function loadUnderlay() {
  const cam = S.currentCamera;
  fetch(`/stream/${cam}/snapshot?type=rgb`)
    .then(r => {
      if (!r.ok) throw new Error(String(r.status));
      return r.blob();
    })
    .then(blob => {
      if (!S.alignEditing || S.currentCamera !== cam) return;   // 已經離開了
      const img = new Image();
      const url = URL.createObjectURL(blob);
      img.onload = () => {
        if (!S.alignEditing || S.currentCamera !== cam) { URL.revokeObjectURL(url); return; }
        _underlay = { img, url };
      };
      img.onerror = () => URL.revokeObjectURL(url);
      img.src = url;
    })
    .catch(() => {
      // 相機斷線或夜間 rgb 全黑，這招現在沒用。講出來，讓人改用遮罩對位，
      // 而不是對著一片黑硬推。
      showToast('現在取不到 RGB 畫面（相機斷線或夜間），請改用遮罩對位');
    });
}

function dropUnderlay() {
  if (_underlay) { URL.revokeObjectURL(_underlay.url); _underlay = null; }
}

// 底圖畫在哪，就是這四個參數說了算：所見即所存。
// 這跟後端 thermal_align.map_box 是同一條換算，只是把整張圖當成一個 bbox 來推。
export function drawAlignUnderlay(ctx, g) {
  if (!S.alignEditing || !_underlay) return;
  const a = S.thermalAlign;
  ctx.save();
  ctx.globalAlpha = UNDERLAY_ALPHA;
  ctx.drawImage(
    _underlay.img,
    g.offX + a.off_x * g.renderW,
    g.offY + a.off_y * g.renderH,
    a.scale_x * g.renderW,
    a.scale_y * g.renderH,
  );
  ctx.restore();
}

// ── 開關 ────────────────────────────────────────────────────
export function openAlignEditor() {
  if (!S.currentCamera) return;
  if (S.currentType !== 'thermal') {
    showToast('請先切到熱像畫面再校正');
    return;
  }
  // 回放照樣能校正，而且通常比 LIVE 好用：對位是一組固定的幾何關係（鏡頭鎖死
  // 不會動），在回放上可以停在一幀有豬的畫面慢慢對，不必等豬走到定位。夜間
  // 更是只有回放校得了——rgb 全黑偵測不到豬，LIVE 的熱像上一個框都沒有。
  // 這跟遮罩編輯器不同：遮罩要看的是「現在這台相機拍到什麼」，所以它限定 LIVE。
  const wrap = document.getElementById('video-wrap');
  _snapshot = { ...S.thermalAlign };
  S.alignEditing = true;
  els.alignPanel?.removeAttribute('hidden');
  wrap?.classList.add('align-editing');
  wrap?.addEventListener('pointerdown', onPointerDown);
  wrap?.addEventListener('pointermove', onPointerMove);
  wrap?.addEventListener('pointerup', onPointerUp);
  wrap?.addEventListener('pointercancel', onPointerUp);
  renderReadout();
  loadUnderlay();
}

function closeAlignEditor() {
  const wrap = document.getElementById('video-wrap');
  S.alignEditing = false;
  _dragging = false;
  dropUnderlay();
  els.alignPanel?.setAttribute('hidden', '');
  wrap?.classList.remove('align-editing');
  wrap?.removeEventListener('pointerdown', onPointerDown);
  wrap?.removeEventListener('pointermove', onPointerMove);
  wrap?.removeEventListener('pointerup', onPointerUp);
  wrap?.removeEventListener('pointercancel', onPointerUp);
}

async function saveAlign() {
  const a = S.thermalAlign;
  try {
    const resp = await fetch(`/thermal-align/${S.currentCamera}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(a),
    });
    if (!resp.ok) {
      const d = await resp.json().catch(() => ({}));
      showToast(`儲存失敗：${d.detail || resp.status}`);
      return;
    }
    showToast('熱像對位已儲存，體溫取樣位置一併跟著改');
    closeAlignEditor();
  } catch (_) { showToast('儲存失敗'); }
}

// 熱像以外的畫面沒有對位可言，入口按鈕跟著隱藏（顯示出來只會讓人按了沒反應）。
// 回放不隱藏：見 openAlignEditor 的說明，夜間只有回放校得了。
export function syncAlignButtonVisibility() {
  if (!els.alignToggleBtn) return;
  const show = S.currentType === 'thermal';
  els.alignToggleBtn.hidden = !show;
  if (!show && S.alignEditing) closeAlignEditor();
}

export function initAlignEditor() {
  els.alignToggleBtn?.addEventListener('click', () => openAlignEditor());
  document.getElementById('align-save-btn')?.addEventListener('click', saveAlign);
  document.getElementById('align-cancel-btn')?.addEventListener('click', () => {
    if (_snapshot) S.thermalAlign = { ..._snapshot };
    renderReadout();
    closeAlignEditor();
  });
  document.getElementById('align-reset-btn')?.addEventListener('click', () => {
    S.thermalAlign = { ...IDENTITY };
    renderReadout();
  });
  // dx/dy/dsx/dsy 寫在 HTML 的 data- 屬性上，八顆按鈕共用一個 handler。
  document.querySelectorAll('#align-panel [data-nudge]').forEach(btn => {
    btn.addEventListener('click', () => {
      const [k, sign] = btn.dataset.nudge.split(':');
      const step = k.startsWith('off') ? OFF_STEP : SCALE_STEP;
      nudge({ [k]: step * Number(sign) });
    });
  });
}
