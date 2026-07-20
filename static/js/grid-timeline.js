// static/js/grid-timeline.js — grid 模式的時段選擇器：日期列（24 格）＋月曆 popover。
// 從 grid.js 拆出（grid.js 已逾 400 行、承載兩塊關注點）。純重構，行為零變更。
//
// 依賴方向 grid.js → grid-timeline.js → api.js/state.js，本檔不 import grid.js：
// 需要 grid.js 的核心播放狀態（_gridHourTs/_cams/_gridGen）一律透過
// initGridTimeline() 注入的 callback 讀取；使用者點小時透過 onPickHour(ts)
// callback 交還 grid.js（實際等於呼叫 grid.js 的 setGridPlayback）。
// grid.js 端需要重繪日期列（setGridPlayback 換時段後）或關閉月曆 popover
// （leaveGrid()）時，呼叫本檔匯出的 renderGridDayBar()/setGridCalOpen()。
//
// 時段對齊（本檔 localDayStart/renderGridDayBar 的小時分桶）假設部署時區
// 無 DST：一律用「本地午夜 + h×3600」換算 epoch，換日光節約會有 1 小時
// 對齊誤差（與 grid.js 檔頭原註解同一份假設，拆檔後兩邊都保留一份）。
import { getJSON } from './api.js';

let _onPickHour = null;      // (hourTs) => void：使用者點時段格，grid.js 接手切換播放
let _getPlayingHour = null;  // () => hourTs|null：目前 grid 播放中的時段（LIVE 時為 null）
let _getCams = null;         // () => string[]：目前 grid 的攝影機清單
let _getGen = null;          // () => number：grid.js 目前的 _gridGen（比對過期回應用）

let _gridDay = null;          // 選中日 00:00 epoch
let _gridMonth = null;        // Date（該月 1 日）
let _unionHours = new Set();  // 該月「任一攝影機有錄影」的小時 ts 聯集

export function initGridTimeline({ onPickHour, getPlayingHour, getCams, getGen }) {
  _onPickHour = onPickHour;
  _getPlayingHour = getPlayingHour;
  _getCams = getCams;
  _getGen = getGen;
}

export function localDayStart(date) {
  const d = new Date(date); d.setHours(0, 0, 0, 0);
  return Math.floor(d.getTime() / 1000);
}

// grid.js 進場入口（enterGrid()）：首次進場（_gridDay 尚未選過）預設今天，
// 接著抓當月聯集。與原 enterGrid() 內「if (_gridDay === null) {...}; loadGridCalendar(gen)」
// 同一份時序——呼叫端（enterGrid）不 await 本函式，日期初始化仍在呼叫當下同步完成，
// 只有後續的月曆網路抓取是非同步。
export async function refreshGridCalendar(gen) {
  if (_gridDay === null) {
    const today = new Date();
    _gridDay = localDayStart(today);
    _gridMonth = new Date(today.getFullYear(), today.getMonth(), 1);
  }
  await loadGridCalendar(gen);
}

async function loadGridCalendar(gen) {
  const requestedMonth = _gridMonth;   // 換月 race（Task 7 Minor）：記錄當下月份，
                                        // Promise.all 期間若月份已再度切換，_gridGen
                                        // 不變（不誤傷並行中的 tile build），改比對月份
  const y = requestedMonth.getFullYear(), m = requestedMonth.getMonth();
  const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
  const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
  const sets = await Promise.all(_getCams().map(async cam => {
    try {
      const { hours } = await getJSON(
        `/stream/${cam}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
      return hours;
    } catch (_) { return []; }
  }));
  if (gen !== _getGen()) return;
  if (_gridMonth !== requestedMonth) return;   // 換月 race：非目前顯示月份的回應直接丟棄（identity 比對）
  _unionHours = new Set(sets.flat());
  renderGridCalendar();
  // M-1：只在 _gridDay 落在這次剛抓回的月份內才重繪日期列。換月按鈕只改
  // _gridMonth、不改 _gridDay（要點日期格才會改），若這裡無條件呼叫
  // renderGridDayBar()，會用「新月份的 _unionHours」去畫「舊月份那天」的
  // 24 格，導致全變無錄影、目前播放中的 `.playing` 高亮消失（VOD 其實還在播）。
  const gridDayInMonth = new Date(_gridDay * 1000);
  if (gridDayInMonth.getFullYear() === y && gridDayInMonth.getMonth() === m) {
    renderGridDayBar();
  }
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

// grid.js 呼叫入口：setGridPlayback() 換時段後重繪（同步、無網路），
// 用 _getPlayingHour() 取得剛更新的播放時段畫出 `.playing` 高亮。
export function renderGridDayBar() {
  const bar = document.getElementById('grid-timeline-bar');
  bar.innerHTML = '';
  const playingHour = _getPlayingHour();
  for (let h = 0; h < 24; h++) {
    const slotTs = _gridDay + h * 3600;
    const hasData = _unionHours.has(slotTs);
    const slot = document.createElement('div');
    slot.className = 'grid-slot' + (hasData ? ' has-data' : '');
    slot.setAttribute('role', 'listitem');
    slot.textContent = String(h).padStart(2, '0');
    slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
    if (slotTs === playingHour) slot.classList.add('playing');
    if (hasData) slot.addEventListener('click', () => _onPickHour(slotTs));
    bar.appendChild(slot);
  }
  updateGridDateLabel();
}

function updateGridDateLabel() {
  const d = new Date(_gridDay * 1000);
  document.getElementById('grid-date-btn-label').textContent =
    `${d.getFullYear()}/${String(d.getMonth() + 1).padStart(2, '0')}/${String(d.getDate()).padStart(2, '0')}`;
}

// ── grid 月曆 popover（一次性綁定；元素常駐 DOM，hidden 控制顯示）──────
// grid.js 呼叫入口：leaveGrid() 離開時關閉 popover、拔掉 outside-click listener。
export function setGridCalOpen(open) {
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
// M-3：module 求值期直接綁定；若這些元素缺失（index.html 結構異動）
// getElementById(...).addEventListener 會直接拋錯，整個 app（不只 grid）白屏。
// 比照 main.js 既有 `if (el)` 防禦模式。
{
  const dateBtn = document.getElementById('grid-date-btn');
  if (dateBtn) dateBtn.addEventListener('click', () => {
    setGridCalOpen(document.getElementById('grid-calendar').hidden);
  });
}
{
  const prevBtn = document.getElementById('grid-prev-month');
  if (prevBtn) prevBtn.addEventListener('click', () => {
    _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() - 1, 1);
    loadGridCalendar(_getGen());
  });
}
{
  const nextBtn = document.getElementById('grid-next-month');
  if (nextBtn) nextBtn.addEventListener('click', () => {
    _gridMonth = new Date(_gridMonth.getFullYear(), _gridMonth.getMonth() + 1, 1);
    loadGridCalendar(_getGen());
  });
}
