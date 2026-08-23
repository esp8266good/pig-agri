// static/js/help.js — 說明模式與 tooltip。
//
// 桌機用原生 title（滑鼠移上去就看得到），觸控裝置沒有 hover，所以另外做一個
// 「說明模式」：開啟後點任何元件都只顯示它的說明、不執行它的動作。豬場現場多半是
// 拿手機在看，沒有這個等於現場人員完全沒有說明。
//
// 說明文字集中放在這裡而不是散在 HTML 的 data-help 屬性上：一個地方改完，
// 桌機 title 與說明模式兩邊同時更新，也不必為了改一句話去動版面結構。
import { S, syncBottomInset } from './state.js';

const HELP = {
  // ── 頂部 ──
  'cam-select':          '選擇要看哪一台攝影機。切換後畫面、時間軸、關注清單都會跟著換。',
  'btn-rgb':             '一般彩色畫面。',
  'btn-thermal':         '熱像畫面。熱像沒有豬隻方框，它只用來看體溫。',
  'view-toggle-btn':     '切換單一畫面與多畫面同時監看。',
  'video-max-btn':       '把右邊的清單和下面的時間軸收起來，讓影片吃滿整個畫面。再按一次（或按 Esc）回到原本的版面。只影響顯示，不會停掉錄影或分析。',
  'bell-btn':            '通知中心。上面的數字是未讀的告警筆數。',
  'manual-link':         '操作手冊，會開新分頁。裡面有完整的使用說明與每一項設定的後果。',
  'settings-btn':        '系統設定。裡面有幾項改錯會刪掉錄影，每一項都有說明。',
  'logout-btn':          '登出。',
  'storage-pill':        '硬碟狀態。變色代表空間不足或寫入異常，此時直播還在，但影像不會存檔。',
  'help-btn':            '說明模式。開啟後點畫面上任何東西都只會顯示它的用途，不會真的執行。再按一次離開。',

  // ── 播放控制 ──
  'play-btn':            '播放或暫停。',
  'seek-track':          '拖曳可以跳到這個小時裡的任一時間點。',
  'transport-live-btn':  '回到直播的最新畫面。',
  'vod-banner-live-btn': '離開回放，回到直播。',

  // ── 時間軸 ──
  'date-btn':            '選擇要看哪一天的回放。',
  'live-btn':            '回到直播。',
  'timeline-bar':        '這一天的 24 個小時。顏色較亮的代表有錄到影像，點下去就開始播。可以按住多選。',
  'btn-retain':          '把選取的時段上鎖。上鎖的影像不會被自動清理刪掉。',
  'btn-bookmark':        '幫選取的時段加上星號標記，方便日後找回來。',
  'btn-delete-rec':      '永久刪除選取時段的影像。刪掉救不回來。',
  'btn-clear-sel':       '取消目前的時段選取。',

  // ── 多畫面監看 ──
  'grid-date-btn':       '選擇要看哪一天的回放。所有畫面會一起換到那一天。',
  'grid-timeline-bar':   '這一天的 24 個小時，只要有任何一台攝影機錄到就會亮起來。點一下，上面所有畫面一起跳到那個時段。這裡不能多選，也不能保留或刪除影像；要做那些事請先切回單畫面。',
  'grid-timeline-hint':  '這條時間軸管的是所有畫面，不是單獨一台。',
  'grid-playback-banner': '現在所有畫面都在回放同一個時段，不是即時畫面。',
  'grid-live-btn':       '離開回放，所有畫面回到即時直播。',

  // ── 右欄 ──
  'box-mode-row':        '影片上要畫幾種框。「只畫重點」＝直播只留關注清單的紅橘綠框、回放只留異常的紅框，其餘的豬不畫（左下角會告訴你藏了幾個）。「其餘畫成淡框」＝一般的豬留一圈很淡的線。「全部照畫」＝每一隻都畫。'
  'solo-checkbox':       '先點下面清單裡的一隻豬，再勾這裡，畫面上就只留牠一隻的框。',
  'show-read-toggle':    '連已讀的通知一起顯示。',
  'clear-read-btn':      '刪除所有已讀的通知。未讀的不會被刪。',
  'alert-load-more':     '往回載入更早的通知。',

  // ── 遮罩 ──
  'mask-edit-btn':       '編輯這台相機的遮罩，用來擋掉走道、牆面這種不該被當成豬的區域。遮罩不會改變畫面。',
  'mask-show-overlay':   '在畫面上用紅色半透明區塊顯示遮罩範圍。平常關著就好，確認位置時才需要。',
  'mask-commit-btn':     '把剛才點出來的那些點收成一塊遮罩區域。至少要三個點。',
  'mask-undo-btn':       '退回剛才點的最後一個點。',
  'mask-save-btn':       '儲存並立即套用。存檔後下一幀就生效，不用重新啟動。',
  'mask-cancel-btn':     '放棄這次的修改，回到上次儲存的狀態。',

  // ── 設定 ──
  'set-analysis-interval':   '多久重算一次活動量。',
  'set-analysis-window':     '每次計算往回看多久的資料。視窗太短時，被擋住一下子的豬就算不出活動量。',
  'set-temp-enabled':        '關掉之後系統只看活動量，畫面上的體溫旗標也會全部清掉。',
  'set-anomaly-threshold':   '體溫要偏離平均多少才算異常。數字越大越不敏感。',
  'set-focus_lowest_enabled': '沒有任何異常時，要不要列出活動量最低的幾隻豬。',
  'set-focus_lowest_n':      '沒有異常時要列幾隻。列太多會失去「該去看哪幾隻」的意義。',
  'set-focus_top_n':         '對照組要列幾隻活動量最高的豬。填 0 就是不顯示對照組。',
  'set-mask_enabled':        '遮罩總開關。發現有真的豬被遮掉時，把這個關掉就立刻回到完全不過濾的狀態。',
  'set-recording_schedule_enabled': '在指定時段內不把影像存到硬碟，但直播照常可以看。',
  'set-recording_off_start': '開始停止存檔的時間。',
  'set-recording_off_end':   '恢復存檔的時間。',
  'set-hls-retention':       '⚠ 超過這個天數的錄影會被自動刪除，刪掉就沒了。調小之前先確認不再需要那段影像。',
  'set-storage_min_free_gb': '剩餘空間低於這個數字時停止存檔並發出警告，直播仍然正常。',
  'set-storage_check_interval_seconds': '多久檢查一次硬碟狀態。',
  'set-ntfy_enabled':        '關掉就完全不推播。',
  'set-ntfy_url':            '⚠ 推播要送到哪裡。這串網址等同密碼，知道的人可以看到你全部的告警。',
  'set-ntfy_revive_priority': '攝影機斷線自動重連時要不要推播。攝影機不穩定時設成「最低」可以讓通知不要一直響。',
  'set-gpu_off_schedule_enabled': '在指定時段停止影像辨識以節省電力。停止期間完全沒有偵測：沒有方框、也不會累積活動量。',
  'set-gpu_off_start':       '開始停止辨識的時間。',
  'set-gpu_off_end':         '恢復辨識的時間。',
};

const FALLBACK = '這個位置沒有說明。再按一次右上角的「?」可以離開說明模式。';

function sheet() { return document.getElementById('help-sheet'); }

function showHelp(text) {
  const el = sheet();
  if (!el) return;
  el.textContent = text;
  el.removeAttribute('hidden');
  syncBottomInset();
}

function hideSheet() {
  sheet()?.setAttribute('hidden', '');
  syncBottomInset();
}

// 這些按鈕的作用只是「打開或關閉某個面板」，本身不改變任何設定或資料。
// 說明模式下要讓它們照常運作，否則使用者根本走不到設定抽屜裡面，
// 就看不到抽屜裡那些欄位的說明——而那正是最需要說明的地方。
// 放行的同時仍然顯示它們自己的說明。
const PASSTHROUGH = [
  '#settings-btn', '#settings-close-btn', '#settings-overlay',
  '#bell-btn', '.tab-btn', '#view-toggle-btn', '#manual-link',
  // 放大影片只是收合版面，不改任何設定或資料，說明模式下照樣放行。
  '#video-max-btn',
].join(',');

// 用捕獲階段攔截：要在元件自己的 handler 之前把事件吃掉，
// 否則「點了才發現不該點」就來不及了。pointerdown 一起攔是為了 <select>，
// 只擋 click 的話原生下拉選單還是會展開。
function intercept(e) {
  if (!S.helpMode) return;
  if (e.target.closest?.('#help-btn')) return;   // 「?」本身要能按，否則出不去
  if (e.target.closest?.('#help-sheet')) return; // 說明條本身可捲動

  const pass = e.target.closest?.(PASSTHROUGH);
  if (!pass) {
    e.preventDefault();
    e.stopPropagation();
  }
  if (e.type === 'pointerdown' || e.type === 'click') {
    const el = e.target.closest?.('[data-help]');
    showHelp(el ? el.dataset.help : FALLBACK);
  }
}

export function setHelpMode(on) {
  S.helpMode = !!on;
  document.body.classList.toggle('help-mode', S.helpMode);
  const btn = document.getElementById('help-btn');
  if (btn) btn.setAttribute('aria-pressed', String(S.helpMode));
  if (S.helpMode) {
    showHelp('說明模式：點畫面上任何東西都只會顯示用途，不會真的執行。再按一次「?」離開。');
  } else {
    hideSheet();
  }
}

export function initHelp() {
  for (const [id, text] of Object.entries(HELP)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.dataset.help = text;
    // 桌機的 hover tooltip 直接沿用同一份文字；已經有 title 的不覆蓋
    // （那些多半是更貼近該處的短提示）。
    if (!el.title) el.title = text;
  }
  // 分頁按鈕沒有固定 id，用 data-tab 認。
  document.querySelectorAll('.tab-btn').forEach(btn => {
    const text = {
      'pig-status': '每隻豬的活動量與體溫，最上面是關注清單。',
      'notifications': '所有告警記錄。連續的同源告警會合併成一條。',
      'bookmarks': '加過星號的時段。',
    }[btn.dataset.tab];
    if (text) { btn.dataset.help = text; if (!btn.title) btn.title = text; }
  });

  document.getElementById('help-btn')
    ?.addEventListener('click', () => setHelpMode(!S.helpMode));
  for (const type of ['pointerdown', 'mousedown', 'click', 'keydown', 'change']) {
    document.addEventListener(type, intercept, true);
  }
}
