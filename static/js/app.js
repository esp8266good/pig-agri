    // ── State ─────────────────────────────────────────────────
    let hls = null;
    let ws = null;
    let wsGeneration = 0;   // increment on every connectWS; onclose checks this
    let wsRetryTimer = null;
    let wsRetryCount = 0;
    const MAX_WS_RETRY = 5;
    const WS_RETRY_BASE_MS = 2000;

    let latestBoxes = [];
    let vodStartTs = 0;
    let bboxHistory = [];   // live mode: [{ts, boxes}] ring buffer for HLS latency alignment
    let _dbg = null;        // live-sync diagnostics snapshot (press 'd' to toggle HUD)
    let currentCamera = null;
    let currentType = 'rgb';
    let animFrameId = null;
    let isLive = true;
    let currentMonth = null;        // Date：顯示中月份（本地，設為該月 1 號）
    let selectedDay = null;         // 選取日本地午夜的 Unix 秒（3600 倍數）
    let monthHoursSet = new Set();  // 當月有資料的 hour_ts（3600 倍數）
    let vodDebounceTimer = null;
    let vodFetching = false;   // prevent overlapping VOD tracking requests
    let anomalyMap = {};           // { object_id: { activity_anomaly, temp_anomaly, ... } }
    let vodAlerts = [];            // VOD 模式下的歷史 alerts
    let showReadAlerts = false;    // 通知中心「顯示已讀」toggle 狀態
    let liveAnomalyIntervalId = null;
    let liveHandoffIntervalId = null;  // 偵測整點 rollover → 重抓 /live 換新小時 URL
    let currentLiveUrl = null;         // 目前 live 載入的 m3u8 URL（含小時段）
    let currentObjectIds = new Set();  // 最近一次 WS frame 出現的 object_id
    let selectedObjectId = null;   // 點選要強調的豬 object_id（null = 未選）
    let soloMode = false;          // 「只顯示選取」開關
    let sortKey = 'activity';      // 'activity' | 'temp' | 'id'
    let sortDir = 1;               // 1 = 升序（活動/溫度低先、ID 小先）；-1 = 降序
    let selectMode = false;
    let selectedHours = new Set();        // hour_ts (number)
    let savedSegmentsMap = new Map();     // hour_ts -> {id, label, note}

    // ── Transport / scrubber state ────────────────────────────
    let transportDragging = false;
    let dragFrac = 0;
    let seekCommitTimer = null;
    let dragSeekPending = false;
    let trackingFetchTimer = null;
    let trackingCache = new Map();   // VOD: "cam|0.5s-bucket" -> boxes[]

    // ── DOM refs ──────────────────────────────────────────────
    const video     = document.getElementById('video');
    const camSelect = document.getElementById('cam-select');
    const statusEl  = document.getElementById('status');
    const statusTxt = document.getElementById('status-text');
    const skeleton  = document.getElementById('skeleton');
    const countBadge = document.getElementById('count-badge');
    const latencyChip = document.getElementById('latency-chip');
    const latencyVal  = document.getElementById('latency-val');
    const toastEl   = document.getElementById('toast');
    const liveBtn        = document.getElementById('live-btn');
    const calLabelEl     = document.getElementById('calendar-label');
    const calGridEl      = document.getElementById('calendar-grid');
    const prevMonthBtn   = document.getElementById('prev-month-btn');
    const nextMonthBtn   = document.getElementById('next-month-btn');
    const timelineBar    = document.getElementById('timeline-bar');
    const bellBadge      = document.getElementById('bell-badge');
    const pigStatusBody  = document.getElementById('pig-status-body');
    const alertListEl    = document.getElementById('alert-list');
    const transportEl    = document.getElementById('transport');
    const playBtn        = document.getElementById('play-btn');
    const timeCurEl      = document.getElementById('time-cur');
    const timeDurEl      = document.getElementById('time-dur');
    const seekTrack      = document.getElementById('seek-track');
    const seekBuffered   = document.getElementById('seek-buffered');
    const seekProgress   = document.getElementById('seek-progress');
    const seekHandle     = document.getElementById('seek-handle');
    const liveBtnT       = document.getElementById('transport-live-btn');
    const liveLabelT     = document.getElementById('transport-live-label');

    // ── Status helpers ────────────────────────────────────────
    function setStatus(msg, cls = '') {
      statusTxt.textContent = msg;
      statusEl.className = 'status-pill' + (cls ? ' ' + cls : '');
    }

    let toastTimer = null;
    function showToast(msg, duration = 3000) {
      toastEl.textContent = msg;
      toastEl.classList.add('show');
      clearTimeout(toastTimer);
      toastTimer = setTimeout(() => toastEl.classList.remove('show'), duration);
    }

    function setSkeleton(visible) {
      skeleton.classList.toggle('visible', visible);
    }

    // ── Timeline helpers ──────────────────────────────────────
    function localDayStart(date) {
      const d = new Date(date);
      d.setHours(0, 0, 0, 0);
      return Math.floor(d.getTime() / 1000);
    }

    function dayHasData(dayTs) {
      for (let h = 0; h < 24; h++) {
        if (monthHoursSet.has(dayTs + h * 3600)) return true;
      }
      return false;
    }

    async function loadCalendar() {
      monthHoursSet = new Set();
      if (!currentCamera || !currentMonth) return;
      const y = currentMonth.getFullYear(), m = currentMonth.getMonth();
      const monthStart = Math.floor(new Date(y, m, 1).getTime() / 1000);
      const monthEnd   = Math.floor(new Date(y, m + 1, 1).getTime() / 1000);
      try {
        const resp = await fetch(`/stream/${currentCamera}/timeline?start_ts=${monthStart}&end_ts=${monthEnd}`);
        if (resp.ok) {
          const { hours } = await resp.json();
          hours.forEach(h => monthHoursSet.add(h));
        }
      } catch (_) {}
      renderCalendar();
    }

    function renderCalendar() {
      if (!currentMonth) return;
      const y = currentMonth.getFullYear(), m = currentMonth.getMonth();
      calLabelEl.textContent = `${y} 年 ${m + 1} 月`;
      const firstDow = new Date(y, m, 1).getDay();        // 0=Sun
      const daysInMonth = new Date(y, m + 1, 0).getDate();
      calGridEl.innerHTML = '';
      for (let i = 0; i < firstDow; i++) {
        const e = document.createElement('div');
        e.className = 'cal-day empty';
        calGridEl.appendChild(e);
      }
      for (let d = 1; d <= daysInMonth; d++) {
        const dayTs = Math.floor(new Date(y, m, d).getTime() / 1000);
        const cell = document.createElement('div');
        cell.className = 'cal-day in-month';
        cell.textContent = d;
        if (dayHasData(dayTs)) cell.classList.add('has-rec');
        if (dayTs === selectedDay) cell.classList.add('day-selected');
        cell.addEventListener('click', () => selectDay(dayTs));
        calGridEl.appendChild(cell);
      }
      const thisMonthFirst = new Date();
      thisMonthFirst.setDate(1); thisMonthFirst.setHours(0, 0, 0, 0);
      nextMonthBtn.disabled = new Date(y, m, 1) >= thisMonthFirst;
    }

    function prevMonth() {
      clearSelection();
      if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
      currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() - 1, 1);
      loadCalendar();
    }

    function nextMonth() {
      clearSelection();
      if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
      currentMonth = new Date(currentMonth.getFullYear(), currentMonth.getMonth() + 1, 1);
      loadCalendar();
    }

    async function selectDay(dayTs) {
      selectedDay = dayTs;
      clearSelection();
      if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
      await loadDaySegments();
      renderDayBar();
      renderCalendar();
    }

    function renderDayBar() {
      timelineBar.innerHTML = '';
      if (!selectedDay) return;
      for (let h = 0; h < 24; h++) {
        const slotTs = selectedDay + h * 3600;
        const hasData = monthHoursSet.has(slotTs);
        const slot = document.createElement('div');
        slot.className = 'timeline-slot' + (hasData ? ' has-data' : '');
        slot.setAttribute('role', 'listitem');
        slot.title = new Date(slotTs * 1000).toLocaleString('zh-TW');
        const seg = savedSegmentsMap.get(slotTs);
        if (seg) {
          slot.classList.add(seg.label ? 'bookmarked' : 'protected');
          const marker = document.createElement('button');
          marker.className = 'slot-marker' + (seg.label ? '' : ' protected-marker');
          marker.textContent = seg.label ? '★' : '🔒';
          marker.title = seg.label ? `書籤:${seg.label}(點擊管理)` : '保留中(點擊管理)';
          marker.onclick = (e) => {
            e.stopPropagation();
            openSlotActionMenu(marker, seg);
          };
          slot.appendChild(marker);
        }
        if (selectedHours.has(slotTs)) slot.classList.add('slot-selected');
        if (hasData) {
          slot.addEventListener('click', () => {
            if (selectMode) {
              if (selectedHours.has(slotTs)) { selectedHours.delete(slotTs); slot.classList.remove('slot-selected'); }
              else { selectedHours.add(slotTs); slot.classList.add('slot-selected'); }
              updateActionBar();
            } else {
              document.querySelectorAll('.timeline-slot.selected')
                .forEach(s => s.classList.remove('selected'));
              slot.classList.add('selected');
              loadVod(slotTs);
            }
          });
        }
        timelineBar.appendChild(slot);
      }
    }

    let _slotActionMenuEl = null;
    function closeSlotActionMenu() {
      if (_slotActionMenuEl) {
        _slotActionMenuEl.remove();
        _slotActionMenuEl = null;
        document.removeEventListener('click', _onSlotMenuOutside);
      }
    }
    function _onSlotMenuOutside(e) {
      if (_slotActionMenuEl && !_slotActionMenuEl.contains(e.target)) {
        closeSlotActionMenu();
      }
    }
    function openSlotActionMenu(anchor, seg) {
      closeSlotActionMenu();
      const menu = document.createElement('div');
      menu.className = 'slot-action-menu';
      const mkBtn = (label, onClick) => {
        const b = document.createElement('button');
        b.textContent = label;
        b.onclick = (e) => { e.stopPropagation(); closeSlotActionMenu(); onClick(); };
        return b;
      };
      if (seg.label) {
        menu.appendChild(mkBtn('編輯書籤', () => openBookmarkEditModal(seg)));
        menu.appendChild(mkBtn('取消書籤', () => onUnmarkSlot(seg, '取消書籤')));
      } else {
        menu.appendChild(mkBtn('取消保留', () => onUnmarkSlot(seg, '取消保留')));
      }
      menu.appendChild(mkBtn('關閉', () => {}));
      document.body.appendChild(menu);
      const rect = anchor.getBoundingClientRect();
      const mw = menu.offsetWidth;
      let left = rect.left + window.scrollX;
      if (left + mw > window.innerWidth - 8) left = window.innerWidth - mw - 8;
      if (left < 8) left = 8;
      menu.style.left = `${left}px`;
      menu.style.top = `${rect.bottom + window.scrollY + 4}px`;
      _slotActionMenuEl = menu;
      // 延遲掛 listener,避免本次 click 立刻被當「outside」
      setTimeout(() => document.addEventListener('click', _onSlotMenuOutside), 0);
    }
    async function onUnmarkSlot(seg, label) {
      if (!confirm(`${label}此小時?(不刪影片)`)) return;
      try {
        const resp = await fetch(`/storage/segments/${seg.id}`, { method: 'DELETE' });
        if (!resp.ok) throw new Error();
        await loadTimeline();
        if (typeof loadBookmarks === 'function') await loadBookmarks();
      } catch (_) { alert('操作失敗'); }
    }

    async function loadTimeline() {
      if (!currentCamera) return;
      await loadCalendar();
      if (selectedDay) {
        await loadDaySegments();
        renderDayBar();
      }
    }

    function updateActionBar() {
      const bar = document.getElementById('storage-action-bar');
      document.getElementById('storage-sel-count').textContent = `已選 ${selectedHours.size} 小時`;
      bar.classList.toggle('visible', selectMode && selectedHours.size > 0);
    }

    function clearSelection() {
      selectedHours.clear();
      document.querySelectorAll('.timeline-slot.slot-selected')
        .forEach(s => s.classList.remove('slot-selected'));
      updateActionBar();
    }

    async function loadDaySegments() {
      savedSegmentsMap = new Map();
      if (!currentCamera || !selectedDay) return;
      try {
        const resp = await fetch(`/storage/segments?camera_id=${currentCamera}&start_ts=${selectedDay}&end_ts=${selectedDay + 86400}`);
        if (!resp.ok) return;
        const { segments } = await resp.json();
        segments.forEach(s => savedSegmentsMap.set(s.hour_ts, s));
      } catch (_) {}
    }

    async function onRetainClick() {
      if (selectedHours.size === 0) return;
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/segments', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours }),
        });
        if (!resp.ok) throw new Error();
        clearSelection();
        await loadTimeline();
      } catch (_) { alert('保留失敗'); }
    }

    async function onBookmarkClick() {
      if (selectedHours.size === 0) return;
      const label = prompt('書籤名稱：');
      if (label === null || label.trim() === '') return;
      const note = prompt('備註（可留空）：') || null;
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/segments', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours, label: label.trim(), note }),
        });
        if (!resp.ok) throw new Error();
        clearSelection();
        await loadTimeline();
        if (typeof loadBookmarks === 'function') loadBookmarks();
      } catch (_) { alert('書籤失敗'); }
    }

    async function loadBookmarks() {
      const ul = document.getElementById('bookmark-list');
      if (!ul || !currentCamera) return;
      try {
        const resp = await fetch(`/storage/bookmarks?camera_id=${currentCamera}`);
        if (!resp.ok) return;
        const { bookmarks } = await resp.json();
        ul.innerHTML = '';
        if (bookmarks.length === 0) {
          ul.innerHTML = '<li style="opacity:.6">尚無書籤</li>';
          return;
        }
        bookmarks.forEach(b => {
          const li = document.createElement('li');
          li.style.cssText = 'display:flex;flex-direction:column;gap:4px;padding:8px 0;border-bottom:1px solid var(--surface-3)';
          const row = document.createElement('div');
          row.style.cssText = 'display:flex;gap:8px;align-items:center';
          const when = new Date(b.hour_ts * 1000).toLocaleString('zh-TW',
            { month: '2-digit', day: '2-digit', hour: '2-digit' });
          const link = document.createElement('a');
          link.href = '#'; link.textContent = `★ ${b.label}`;
          link.style.cssText = 'color:var(--accent);text-decoration:none;flex:1';
          link.onclick = (e) => { e.preventDefault(); loadVod(b.hour_ts); };
          const time = document.createElement('span');
          time.textContent = when; time.style.cssText = 'opacity:.7;font-size:12px';
          const edit = document.createElement('button');
          edit.textContent = '編輯'; edit.style.cssText = 'padding:2px 8px;cursor:pointer';
          edit.onclick = () => openBookmarkEditModal(b);
          const del = document.createElement('button');
          del.textContent = '移除'; del.style.cssText = 'padding:2px 8px;cursor:pointer';
          del.onclick = async () => {
            if (!confirm(`移除書籤「${b.label}」?(不刪影片)`)) return;
            await fetch(`/storage/segments/${b.id}`, { method: 'DELETE' });
            loadBookmarks(); loadTimeline();
          };
          row.append(link, time, edit, del);
          li.appendChild(row);
          if (b.note) {
            const noteDiv = document.createElement('div');
            noteDiv.textContent = b.note;
            noteDiv.style.cssText = 'font-size:12px;opacity:.75;padding-left:8px;white-space:pre-wrap;color:var(--text-muted)';
            li.appendChild(noteDiv);
          }
          ul.appendChild(li);
        });
      } catch (_) {}
    }

    let _editingBookmarkId = null;
    function openBookmarkEditModal(seg) {
      _editingBookmarkId = seg.id;
      document.getElementById('bookmark-edit-label').value = seg.label || '';
      document.getElementById('bookmark-edit-note').value = seg.note || '';
      const m = document.getElementById('bookmark-edit-modal');
      m.style.display = 'flex';
    }
    function closeBookmarkEditModal() {
      _editingBookmarkId = null;
      const m = document.getElementById('bookmark-edit-modal');
      if (m) m.style.display = 'none';
    }
    async function saveBookmarkEdit() {
      if (_editingBookmarkId === null) return;
      const label = document.getElementById('bookmark-edit-label').value.trim();
      const note = document.getElementById('bookmark-edit-note').value.trim() || null;
      if (!label) { alert('名稱不可空白'); return; }
      const btn = document.getElementById('bookmark-edit-save-btn');
      btn.disabled = true;
      try {
        const resp = await fetch(`/storage/segments/${_editingBookmarkId}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label, note }),
        });
        if (!resp.ok) throw new Error();
        closeBookmarkEditModal();
        await loadBookmarks();
        await loadTimeline();  // savedSegmentsMap 也要刷新(label 可能變)
      } catch (_) { alert('儲存失敗'); }
      finally { btn.disabled = false; }
    }

    function onDeleteRecClick() {
      if (selectedHours.size === 0) return;
      const hours = [...selectedHours].sort((a, b) => a - b);
      const fmt = ts => new Date(ts * 1000).toLocaleString('zh-TW',
        { month: '2-digit', day: '2-digit', hour: '2-digit' });
      document.getElementById('delete-modal-summary').textContent =
        `將刪除 ${hours.length} 個小時：${hours.map(fmt).join('、')}`;
      const protectedSel = hours.filter(h => savedSegmentsMap.has(h));
      const warn = document.getElementById('delete-modal-warn');
      const check = document.getElementById('delete-confirm-check');
      const btn = document.getElementById('delete-confirm-btn');
      if (protectedSel.length > 0) {
        const ul = document.getElementById('delete-modal-protected');
        ul.innerHTML = '';
        protectedSel.forEach(h => {
          const seg = savedSegmentsMap.get(h);
          const li = document.createElement('li');
          li.textContent = `${fmt(h)}${seg.label ? '（書籤：' + seg.label + '）' : '（保留）'}`;
          ul.appendChild(li);
        });
        warn.style.display = '';
        check.checked = false;
        btn.disabled = true;
        check.onchange = () => { btn.disabled = !check.checked; };
      } else {
        warn.style.display = 'none';
        btn.disabled = false;
        check.onchange = null;
      }
      document.getElementById('delete-modal').style.display = 'flex';
    }

    function closeDeleteModal() {
      document.getElementById('delete-modal').style.display = 'none';
    }

    async function confirmDeleteRecordings() {
      const hours = [...selectedHours];
      try {
        const resp = await fetch('/storage/recordings/delete', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ camera_id: currentCamera, hours }),
        });
        if (!resp.ok) throw new Error();
        const r = await resp.json();
        closeDeleteModal();
        clearSelection();
        await loadTimeline();
        if (typeof loadBookmarks === 'function') loadBookmarks();
        alert(`已刪除 ${r.deleted_hours} 小時（影片目錄 ${r.dirs_removed}、軌跡 ${r.tracking_logs}、告警 ${r.health_alerts}）`);
      } catch (_) { alert('刪除失敗'); }
    }

    // ── VOD mode ──────────────────────────────────────────────
    function loadVod(startTs) {
      isLive = false;
      clearPigSelection();
      vodStartTs = startTs;
      vodFetching = false;
      bboxHistory = [];
      liveBtn.style.display = '';
      latestBoxes = [];
      countBadge.textContent = '—';
      latencyChip.style.display = 'none';
      clearTimeout(vodDebounceTimer);
      stopLiveTimers();
      anomalyMap = {};
      vodAlerts  = [];
      currentObjectIds.clear();
      trackingCache.clear();
      transportDragging = false;
      seekTrack.classList.remove('dragging');
      clearTimeout(trackingFetchTimer);

      // 抓此 VOD 時段的歷史 alerts（含前 30 分鐘）
      const vodEnd = startTs + 3600;
      fetch(`/alerts?camera_id=${currentCamera}&start_ts=${startTs - 1800}&end_ts=${vodEnd + 300}`)
        .then(r => r.json())
        .then(data => { vodAlerts = data.alerts || []; })
        .catch(() => {});

      // 斷開 WS（不重連）
      clearTimeout(wsRetryTimer);
      wsGeneration++;
      if (ws) { ws.close(); ws = null; }

      detachVodListeners();
      if (hls) { hls.destroy(); hls = null; }
      video.src = '';
      setSkeleton(true);
      setStatus('載入回放...', '');

      const vodUrl = `/stream/${currentCamera}/vod?start=${startTs}&end=${startTs + 3600}&type=${currentType}`;

      if (Hls.isSupported()) {
        hls = new Hls({ lowLatencyMode: false, backBufferLength: 0 });
        hls.loadSource(vodUrl);
        hls.attachMedia(video);

        hls.on(Hls.Events.MANIFEST_PARSED, () => {
          video.play().catch(() => {});
          setSkeleton(false);
          const dt = new Date(startTs * 1000);
          const label = dt.toLocaleString('zh-TW', {
            year: 'numeric', month: '2-digit', day: '2-digit',
            hour: '2-digit', minute: '2-digit',
          });
          setStatus(`回放中 ${label}`, 'vod');
        });

        hls.on(Hls.Events.ERROR, (_, data) => {
          if (data.fatal) {
            setSkeleton(false);
            setStatus(`回放錯誤：${data.details}`, 'error');
          }
        });

        attachVodListeners();
      }
    }

    function switchToLive() {
      if (isLive) return;
      isLive = true;
      liveBtn.style.display = 'none';
      detachVodListeners();
      clearTimeout(vodDebounceTimer);
      clearTimeout(trackingFetchTimer);
      trackingCache.clear();
      transportDragging = false;
      seekTrack.classList.remove('dragging');
      stopLiveTimers();
      anomalyMap = {};
      vodAlerts  = [];
      latestBoxes = [];
      bboxHistory = [];
      currentObjectIds.clear();
      document.querySelectorAll('.timeline-slot.selected')
        .forEach(s => s.classList.remove('selected'));
      wsRetryCount = 0;
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
      video.addEventListener('timeupdate', onVodTimeUpdate);
      video.addEventListener('seeking',   onVodSeeking);
      video.addEventListener('seeked',    onVodSeeked);
    }
    function detachVodListeners() {
      video.removeEventListener('timeupdate', onVodTimeUpdate);
      video.removeEventListener('seeking',   onVodSeeking);
      video.removeEventListener('seeked',    onVodSeeked);
    }

    // Fetch the tracking frame nearest the current VOD playhead and draw it.
    // Debounced 100ms by default; pass immediate=true on discrete seeks.
    // A small LRU cache keyed by 0.5s buckets avoids re-fetching while scrubbing.
    function scheduleTrackingFetch(immediate = false) {
      if (isLive || !hls || !currentCamera) return;
      clearTimeout(trackingFetchTimer);
      const run = async () => {
        let ts;
        const pd = hls && hls.playingDate;
        if (pd && !isNaN(pd.getTime())) ts = pd.getTime() / 1000;          // 每段重錨、不累積
        else ts = vodStartTs + (video.currentTime || 0);                   // 舊錄影/無 PDT 回退
        if (!ts) return;
        const key = currentCamera + '|' + Math.round(ts * 2);
        if (trackingCache.has(key)) { applyVodBoxes(trackingCache.get(key), ts); return; }
        if (vodFetching) { trackingFetchTimer = setTimeout(run, 60); return; }
        vodFetching = true;
        try {
          const resp = await fetch(`/tracking/${currentCamera}?start=${ts - 2}&end=${ts + 2}`);
          if (resp.ok) {
            const boxes = pickClosestFrame((await resp.json()).logs || [], ts);
            trackingCache.set(key, boxes);
            if (trackingCache.size > 400) trackingCache.delete(trackingCache.keys().next().value);
            applyVodBoxes(boxes, ts);
          }
        } catch (_) {}
        vodFetching = false;
      };
      if (immediate) run(); else trackingFetchTimer = setTimeout(run, 100);
    }

    function applyVodBoxes(boxes, ts) {
      latestBoxes = boxes;
      countBadge.textContent = boxes.length;
      currentObjectIds = new Set(boxes.map(o => o.object_id));
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

    // ── Anomaly map ───────────────────────────────────────────
    async function refreshAnomalyMap() {
      if (!currentCamera) return;
      try {
        const data = await fetch(`/alerts/active?camera_id=${currentCamera}`).then(r => r.json());
        const camCache = data.cache?.[currentCamera] ?? {};
        anomalyMap = {};
        for (const [oid, info] of Object.entries(camCache)) {
          anomalyMap[parseInt(oid)] = info;
        }
        renderPigStatus();
      } catch (_) {}
    }

    function updateVodAnomalyMap(currentTs) {
      anomalyMap = {};
      for (const alert of vodAlerts) {
        const winStart = alert.triggered_at_unix - 1800;
        const winEnd   = alert.triggered_at_unix;
        if (currentTs >= winStart && currentTs <= winEnd) {
          const entry = anomalyMap[alert.object_id] ?? { activity_anomaly: false, temp_anomaly: false };
          if (alert.metric === 'activity')    entry.activity_anomaly = true;
          if (alert.metric === 'temperature') entry.temp_anomaly = true;
          anomalyMap[alert.object_id] = entry;
        }
      }
      renderPigStatus();
    }

    // ── Tab panel ─────────────────────────────────────────────
    function switchTab(tabName) {
      document.querySelectorAll('.tab-btn').forEach(btn => {
        btn.classList.toggle('active', btn.dataset.tab === tabName);
      });
      document.querySelectorAll('.tab-content').forEach(c => {
        c.classList.toggle('active', c.id === `tab-${tabName}`);
      });
    }

    // 依 sortKey/sortDir 排序；null（未分析到）值永遠沉底，避免誤導採血。
    function sortPigRows(rows) {
      return rows.sort((a, b) => {
        if (sortKey === 'id') return (a.oid - b.oid) * sortDir;
        const va = sortKey === 'activity' ? a.act : a.temp;
        const vb = sortKey === 'activity' ? b.act : b.temp;
        if (va == null && vb == null) return a.oid - b.oid;
        if (va == null) return 1;
        if (vb == null) return -1;
        return (va - vb) * sortDir;
      });
    }

    function onSortHeaderClick(key) {
      if (sortKey === key) sortDir = -sortDir;
      else { sortKey = key; sortDir = 1; }   // 換欄一律回升序
      renderPigStatus();
    }

    function updateSortIndicators() {
      document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
        const ind = th.querySelector('.sort-ind');
        if (!ind) return;
        ind.textContent = (th.dataset.sort === sortKey) ? (sortDir === 1 ? ' ▲' : ' ▼') : '';
      });
    }

    function togglePigSelection(oid) {
      selectedObjectId = (selectedObjectId === oid) ? null : oid;
      renderPigStatus();   // 重繪反映 .selected；bbox 強調由 drawBoxes 每幀自然反映
    }

    function renderPigStatus() {
      if (!pigStatusBody) return;
      pigStatusBody.innerHTML = '';
      updateSortIndicators();
      if (currentObjectIds.size === 0) {
        pigStatusBody.innerHTML =
          '<tr><td colspan="3" class="pig-empty-msg">目前無偵測到豬隻</td></tr>';
        return;
      }
      const rows = [];
      for (const oid of currentObjectIds) {
        const a = anomalyMap[oid] ?? null;
        rows.push({
          oid,
          act:  a?.activity_current ?? null,
          temp: a?.temp_current ?? null,
          actAnomaly:  a?.activity_anomaly ?? false,
          tempAnomaly: a?.temp_anomaly ?? false,
        });
      }
      sortPigRows(rows);
      for (const r of rows) {
        const actVal  = r.act  != null ? r.act.toFixed(1)  : '—';
        const tempVal = r.temp != null ? r.temp.toFixed(1) : '—';
        const row = document.createElement('tr');
        row.classList.add('pig-row');
        row.dataset.oid = r.oid;
        if (selectedObjectId === r.oid) row.classList.add('selected');
        if (r.actAnomaly || r.tempAnomaly) row.classList.add('anomaly-row');
        row.addEventListener('click', () => togglePigSelection(r.oid));
        row.innerHTML = `
          <td>#${r.oid}</td>
          <td class="${r.actAnomaly ? 'anomaly-cell' : ''}">
            ${r.actAnomaly ? '⚠ ' : ''}${actVal}
          </td>
          <td class="${r.tempAnomaly ? 'anomaly-cell' : ''}">
            ${r.tempAnomaly ? '🌡 ' : ''}${tempVal}
          </td>`;
        pigStatusBody.appendChild(row);
      }
    }

    function renderNotifications(alerts) {
      if (!alertListEl) return;
      alertListEl.innerHTML = '';
      if (!alerts.length) {
        alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
        return;
      }
      for (const alert of alerts) {
        const dt = new Date(alert.triggered_at_unix * 1000)
          .toLocaleString('zh-TW', {year:'numeric',month:'2-digit',day:'2-digit',
                                    hour:'2-digit',minute:'2-digit'});
        const sigma = alert.std_value > 0
          ? ((alert.current_value - alert.mean_value) / alert.std_value).toFixed(1)
          : '—';
        const metricLabel = alert.metric === 'activity' ? '活動量偏低' : '體溫異常';
        const li = document.createElement('li');
        li.className = 'alert-item' + (alert.is_read ? '' : ' unread');
        li.innerHTML = `
          <div class="alert-info">
            <span class="alert-cam">${alert.camera_id} #${alert.object_id}</span>
            <span class="alert-metric">${metricLabel}</span>
            <span class="alert-time">${dt}</span>
            <span class="alert-sigma">偏差 ${sigma}σ</span>
          </div>
          <button class="mark-read-btn"
                  onclick="markAlertRead(${alert.id}, this)"
                  ${alert.is_read ? 'disabled' : ''}>
            ${alert.is_read ? '已讀' : '標記已讀'}
          </button>
          <button class="mark-read-btn"
                  onclick="onDeleteAlertClick(${alert.id}, this)">
            刪除
          </button>`;
        li.addEventListener('click', e => {
          if (e.target.classList.contains('mark-read-btn')) return;
          if (alert.camera_id !== currentCamera) {
            camSelect.value = alert.camera_id;
            currentCamera = alert.camera_id;
          }
          loadVod(alert.triggered_at_unix - 1800);
        });
        alertListEl.appendChild(li);
      }
    }

    async function _refreshBellBadge() {
      try {
        const d = await fetch('/alerts?unread_only=true').then(r => r.json());
        const n = (d.alerts || []).length;
        bellBadge.textContent = n;
        bellBadge.style.display = n > 0 ? '' : 'none';
      } catch (_) {}
    }

    function _emptyNotifIfNeeded() {
      if (alertListEl && alertListEl.children.length === 0) {
        alertListEl.innerHTML = '<li class="notif-empty">目前無警示記錄</li>';
      }
    }

    async function markAlertRead(alertId, btn) {
      try {
        const resp = await fetch(`/alerts/${alertId}/read`, { method: 'PUT' });
        if (!resp.ok) return;
        const li = btn.closest('.alert-item');
        if (!showReadAlerts) {
          li.remove();
          _emptyNotifIfNeeded();
        } else {
          btn.textContent = '已讀';
          btn.disabled = true;
          li.classList.remove('unread');
        }
        await _refreshBellBadge();
      } catch (_) {}
    }

    async function onDeleteAlertClick(alertId, btn) {
      if (!confirm('永久刪除此警示記錄?')) return;
      try {
        const resp = await fetch(`/alerts/${alertId}`, { method: 'DELETE' });
        if (!resp.ok) { alert('刪除失敗'); return; }
        btn.closest('.alert-item').remove();
        _emptyNotifIfNeeded();
        await _refreshBellBadge();
      } catch (_) { alert('刪除失敗'); }
    }

    async function onClearReadClick() {
      const target = currentCamera ? `「${currentCamera}」` : '所有攝影機';
      if (!confirm(`永久清空${target}的所有已讀警示?(未讀不會被刪)`)) return;
      try {
        const url = currentCamera
          ? `/alerts?read_only=true&camera_id=${currentCamera}`
          : '/alerts?read_only=true';
        const resp = await fetch(url, { method: 'DELETE' });
        if (!resp.ok) { alert('清空失敗'); return; }
        const { deleted } = await resp.json();
        if (deleted === 0) {
          alert('沒有已讀可清除');
        } else {
          alert(`已刪除 ${deleted} 筆已讀警示`);
        }
        refreshNotifications();
      } catch (_) { alert('清空失敗'); }
    }

    async function refreshNotifications() {
      if (!currentCamera) return;
      try {
        const unread = showReadAlerts ? '' : '&unread_only=true';
        const d = await fetch(`/alerts?camera_id=${currentCamera}&limit=50${unread}`)
          .then(r => r.json());
        renderNotifications(d.alerts || []);
        await _refreshBellBadge();
      } catch (_) {}
    }

    // ── Settings ──────────────────────────────────────────────
    async function loadSettings() {
      try {
        const resp = await fetch('/settings');
        if (!resp.ok) return;
        const data = await resp.json();
        const a = document.getElementById('set-analysis-interval');
        const t = document.getElementById('set-anomaly-threshold');
        const r = document.getElementById('set-hls-retention');
        const w = document.getElementById('set-analysis-window');
        const te = document.getElementById('set-temp-enabled');
        if (a && data.analysis_interval_minutes !== undefined) a.value = data.analysis_interval_minutes;
        if (t && data.anomaly_std_threshold !== undefined)     t.value = data.anomaly_std_threshold;
        if (r && data.hls_retention_days !== undefined)        r.value = data.hls_retention_days;
        if (w && data.analysis_window_minutes !== undefined)   w.value = data.analysis_window_minutes;
        if (te && data.temp_anomaly_enabled !== undefined)
          te.value = String(data.temp_anomaly_enabled).toLowerCase() === 'true' ? 'true' : 'false';
        const _sse = document.getElementById('set-recording_schedule_enabled');
        if (_sse) _sse.checked = String(data.recording_schedule_enabled) === 'true';
        const _smap = {
          'set-recording_off_start': 'recording_off_start',
          'set-recording_off_end': 'recording_off_end',
          'set-storage_min_free_gb': 'storage_min_free_gb',
          'set-storage_check_interval_seconds': 'storage_check_interval_seconds',
          'set-ntfy_url': 'ntfy_url',
          'set-ntfy_revive_priority': 'ntfy_revive_priority',
          'set-gpu_off_start': 'gpu_off_start',
          'set-gpu_off_end': 'gpu_off_end',
        };
        for (const [id, key] of Object.entries(_smap)) {
          const el = document.getElementById(id);
          if (el && data[key] != null) el.value = data[key];
        }
        const _ne = document.getElementById('set-ntfy_enabled');
        if (_ne) _ne.checked = String(data.ntfy_enabled) === 'true';
        const _ge = document.getElementById('set-gpu_off_schedule_enabled');
        if (_ge) _ge.checked = String(data.gpu_off_schedule_enabled) === 'true';
      } catch (_) {}
    }

    async function saveSettings() {
      const body = {
        analysis_interval_minutes: document.getElementById('set-analysis-interval').value,
        analysis_window_minutes:   document.getElementById('set-analysis-window').value,
        anomaly_std_threshold:     document.getElementById('set-anomaly-threshold').value,
        hls_retention_days:        document.getElementById('set-hls-retention').value,
        temp_anomaly_enabled:      document.getElementById('set-temp-enabled').value,
        recording_schedule_enabled: String(document.getElementById('set-recording_schedule_enabled').checked),
        recording_off_start:        document.getElementById('set-recording_off_start').value,
        recording_off_end:          document.getElementById('set-recording_off_end').value,
        storage_min_free_gb:        document.getElementById('set-storage_min_free_gb').value,
        storage_check_interval_seconds: document.getElementById('set-storage_check_interval_seconds').value,
        ntfy_enabled:               String(document.getElementById('set-ntfy_enabled').checked),
        ntfy_url:                   document.getElementById('set-ntfy_url').value,
        ntfy_revive_priority:       document.getElementById('set-ntfy_revive_priority').value,
        gpu_off_schedule_enabled:   String(document.getElementById('set-gpu_off_schedule_enabled').checked),
        gpu_off_start:              document.getElementById('set-gpu_off_start').value,
        gpu_off_end:                document.getElementById('set-gpu_off_end').value,
      };
      const statusEl = document.getElementById('settings-status');
      try {
        const resp = await fetch('/settings', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (resp.ok) {
          statusEl.textContent = '✓ 已儲存';
          setTimeout(() => { statusEl.textContent = ''; }, 3000);
        } else {
          const err = await resp.json();
          statusEl.textContent = `✗ ${err.detail || '儲存失敗'}`;
        }
      } catch (e) {
        statusEl.textContent = `✗ 網路錯誤`;
      }
    }

    // ── Storage health pill ────────────────────────────────────
    async function pollStorageHealth() {
      try {
        const r = await fetch('/storage/health');
        if (!r.ok) return;
        const h = await r.json();
        const pill = document.getElementById('storage-pill');
        if (!pill) return;
        const mode = h.target_mode || 'record';
        const recState = h.recording_state || 'ok';
        let cls = 'ok', label = '錄影中';
        if (mode === 'drop') { cls = 'down'; label = '儲存故障：丟幀'; }
        else if (recState === 'down') { cls = 'down'; label = '錄影碟故障 → ephemeral live'; }
        else if (mode === 'ephemeral') { cls = 'degraded'; label = h.recording_time === false ? '夜間不錄影（live 中）' : 'ephemeral live'; }
        else if (recState === 'degraded') { cls = 'degraded'; label = `空間不足（剩 ${h.recording_free_gb}GB）`; }
        pill.className = 'storage-pill ' + cls;
        pill.title = label;
        pill.style.display = '';
      } catch (e) { /* 靜默：監控小燈非關鍵路徑 */ }
    }

    // ── Type toggle ───────────────────────────────────────────
    function setType(type) {
      currentType = type;
      const btnRgb     = document.getElementById('btn-rgb');
      const btnThermal = document.getElementById('btn-thermal');
      btnRgb.classList.toggle('active', type === 'rgb');
      btnThermal.classList.toggle('active', type === 'thermal');
      btnRgb.setAttribute('aria-pressed', type === 'rgb');
      btnThermal.setAttribute('aria-pressed', type === 'thermal');
      if (!isLive) {
        switchToLive();
      } else {
        loadStream();
      }
    }

    // ── WebSocket (tracking) with auto-reconnect ──────────────
    function connectWS(cameraId) {
      clearTimeout(wsRetryTimer);
      if (ws) { ws.close(); ws = null; }
      if (!cameraId) return;

      const gen = ++wsGeneration;  // each call gets a unique generation token
      const wsProtocol = location.protocol === "https:" ? "wss:" : "ws:";
      const url = `${wsProtocol}//${location.host}/ws/tracking/${cameraId}`;
      ws = new WebSocket(url);

      ws.onopen = () => {
        if (gen !== wsGeneration) return;
        wsRetryCount = 0;
      };

      ws.onmessage = (e) => {
        if (gen !== wsGeneration) return;  // stale connection, discard
        try {
          const data = JSON.parse(e.data);
          latestBoxes = data.objects || [];
          countBadge.textContent = latestBoxes.length;
          currentObjectIds = new Set(latestBoxes.map(o => o.object_id));
          renderPigStatus();
          if (data.timestamp) {
            const delay = Date.now() - data.timestamp * 1000;
            latencyChip.style.display = '';
            latencyVal.textContent = Math.round(delay);
            // Buffer bbox with timestamp for HLS live latency alignment.
            // Keep enough history to cover the HLS back-buffer (~90s) so
            // scrubbing back in live mode still has matching boxes.
            bboxHistory.push({ ts: data.timestamp, boxes: latestBoxes });
            if (bboxHistory.length > 1000) bboxHistory.shift();
          }
        } catch (_) {}
      };

      ws.onclose = () => {
        // If gen no longer matches, this close was intentional (camera switched).
        // Do NOT reconnect — that would fight the new connection.
        if (gen !== wsGeneration) return;
        latestBoxes = [];
        countBadge.textContent = '—';
        if (wsRetryCount < MAX_WS_RETRY) {
          const delay = WS_RETRY_BASE_MS * Math.pow(1.5, wsRetryCount);
          wsRetryCount++;
          showToast(`追蹤連線中斷，${(delay/1000).toFixed(1)}s 後重試...`);
          wsRetryTimer = setTimeout(() => connectWS(cameraId), delay);
        } else {
          showToast('追蹤連線失敗，請重新整理頁面', 8000);
        }
      };

      ws.onerror = () => {
        // onerror 後必定會觸發 onclose，交給 onclose 處理
      };
    }

    // ── Canvas overlay ────────────────────────────────────────
    function getBoxColor() {
      return currentType === 'thermal' ? '#ff8c42' : '#22bb77';
    }

    function drawBoxes() {
      updateTransport();
      const canvas = document.getElementById('overlay');
      const elW = video.offsetWidth  || 1;
      const elH = video.offsetHeight || 1;
      if (canvas.width  !== elW) canvas.width  = elW;
      if (canvas.height !== elH) canvas.height = elH;
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, elW, elH);
      const vidW = video.videoWidth;
      const vidH = video.videoHeight;

      // In live mode, pick the bbox entry whose timestamp best matches the
      // wall-clock time of the frame actually on screen. Prefer hls.playingDate
      // (derived from the segment's #EXT-X-PROGRAM-DATE-TIME) — it travels with
      // the media, so network jitter only changes how much is buffered, never
      // which frame a given timestamp maps to. Fall back to (now - hls.latency)
      // only if PDT is unavailable; that estimate misses the server-side
      // pipeline delay (buffer + encode + segmentation) and runs ~3-5s ahead.
      let displayBoxes = latestBoxes;
      if (isLive && bboxHistory.length) {
        let targetTs = null;
        let dbgSrc = 'latest';
        let chosenTs = bboxHistory[bboxHistory.length - 1].ts;
        const pd = hls && hls.playingDate;
        if (pd && !isNaN(pd.getTime())) {
          targetTs = pd.getTime() / 1000;      // PDT≡真實擷取時間，不再減 offset
          dbgSrc = 'PDT';
        } else {
          const latency = (hls && hls.latency != null) ? hls.latency : 0;
          if (latency > 1) { targetTs = Date.now() / 1000 - latency; dbgSrc = 'latency'; }
        }
        if (targetTs != null) {
          let best = bboxHistory[bboxHistory.length - 1];
          let bestDist = Infinity;
          for (const entry of bboxHistory) {
            const d = Math.abs(entry.ts - targetTs);
            if (d < bestDist) { bestDist = d; best = entry; }
          }
          displayBoxes = best.boxes;
          chosenTs = best.ts;
        }
        if (window.__bboxDebug) {
          const now = Date.now() / 1000;
          _dbg = {
            src: dbgSrc, now,
            latency: (hls && hls.latency != null) ? hls.latency : null,
            playingDate: (pd && !isNaN(pd.getTime())) ? pd.getTime() / 1000 : null,
            targetTs, chosenTs,
            newestTs: bboxHistory[bboxHistory.length - 1].ts,
            histLen: bboxHistory.length,
          };
        } else { _dbg = null; }
      } else { _dbg = null; }

      // 只顯示選取：有選取且開關開時，只畫選取的框；無選取則開關不生效（畫全部）。
      if (soloMode && selectedObjectId != null) {
        displayBoxes = displayBoxes.filter(o => o.object_id === selectedObjectId);
      }

      if (!vidW || !vidH || !displayBoxes.length) {
        drawDbgHud();
        animFrameId = requestAnimationFrame(drawBoxes);
        return;
      }
      const scale   = Math.min(elW / vidW, elH / vidH);
      const renderW = vidW * scale;
      const renderH = vidH * scale;
      const offX = (elW - renderW) / 2;
      const offY = (elH - renderH) / 2;
      const baseColor = getBoxColor();
      ctx.lineWidth = 1.5;
      ctx.font = 'bold 11px "DM Sans", monospace';

      for (const o of displayBoxes) {
        const [x, y, w, h] = o.bbox;
        const px = offX + x * scale;
        const py = offY + y * scale;
        const pw = w * scale;
        const ph = h * scale;
        const anomaly     = anomalyMap[o.object_id];
        const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);
        const color       = isAnomalous ? '#ff4444' : baseColor;

        // 選取強調：選取的框加粗全亮，其餘淡化（selectedObjectId 為 null 時不變）
        const isSel  = selectedObjectId != null && o.object_id === selectedObjectId;
        const dimmed = selectedObjectId != null && !isSel;
        ctx.save();
        if (dimmed) ctx.globalAlpha = 0.25;
        if (isSel)  ctx.lineWidth = 4;

        ctx.strokeStyle = color;
        ctx.fillStyle   = color;
        roundRect(ctx, px, py, pw, ph, 3);
        ctx.stroke();

        // 豬隻 ID 標籤
        const label = `#${o.object_id}`;
        const tw = ctx.measureText(label).width;
        ctx.fillStyle = color;
        ctx.fillRect(px - 0.5, py - 16, tw + 6, 15);
        ctx.fillStyle = '#000';
        ctx.fillText(label, px + 2, py - 4);
        ctx.fillStyle = color;

        // 異常圖示（bbox 左下角）
        if (anomaly) {
          let icons = '';
          if (anomaly.activity_anomaly) icons += '⚠';
          if (anomaly.temp_anomaly)     icons += '🌡';
          if (icons) ctx.fillText(icons, px + 2, py + ph - 2);
        }
        ctx.restore();
      }
      drawDbgHud();
      animFrameId = requestAnimationFrame(drawBoxes);
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
      const d = _dbg;
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

    animFrameId = requestAnimationFrame(drawBoxes);

    // ── HLS stream ────────────────────────────────────────────
    // 整點 rollover：後端 ffmpeg 換到新小時目錄、舊小時 playlist 凍結（不再
    // append、無 ENDLIST）。前端定期重抓 /live，若 URL（含小時段）變了就
    // loadSource 新小時 → 避免 live 卡死在凍結 playlist 直到手動重整。
    async function checkLiveHandoff() {
      if (!isLive || !hls || !currentCamera) return;
      try {
        const res = await fetch(`/stream/${currentCamera}/live?type=${currentType}`);
        if (!res.ok) return;
        const live = await res.json();
        if (live.url && live.url !== currentLiveUrl) {
          currentLiveUrl = live.url;
          hls.loadSource(live.url);   // 沿用 attachMedia，重解析新小時 playlist
          video.play().catch(() => {});
        }
      } catch (_) { /* 暫時性網路錯誤，下個 tick 再試 */ }
    }

    // live 模式的兩個週期任務（異常地圖刷新 + 整點小時交接）統一管理，
    // 避免散落的 setInterval/clearInterval 漏清。
    function startLiveTimers() {
      stopLiveTimers();
      liveAnomalyIntervalId = setInterval(refreshAnomalyMap, 30000);
      liveHandoffIntervalId = setInterval(checkLiveHandoff, 12000);
    }
    function stopLiveTimers() {
      clearInterval(liveAnomalyIntervalId); liveAnomalyIntervalId = null;
      clearInterval(liveHandoffIntervalId); liveHandoffIntervalId = null;
    }

    // 切攝影機 / RGB↔Thermal / Live↔VOD 時清掉選取與 solo（避免殘留別來源的高亮）。
    // sortKey/sortDir 為使用者偏好，不重置。
    function clearPigSelection() {
      selectedObjectId = null;
      soloMode = false;
      const cb = document.getElementById('solo-checkbox');
      if (cb) cb.checked = false;
    }

    async function loadStream() {
      if (!currentCamera) return;
      clearPigSelection();

      // 清理舊的 HLS instance
      if (hls) { hls.destroy(); hls = null; }
      video.src = '';
      setSkeleton(true);
      setStatus('正在連線...');
      connectWS(currentCamera);

      try {
        const res = await fetch(`/stream/${currentCamera}/live?type=${currentType}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const live = await res.json();
        const url = live.url;
        currentLiveUrl = url;

        if (Hls.isSupported()) {
          hls = new Hls({
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

          hls.loadSource(url);
          hls.attachMedia(video);

          hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play().catch(() => {});
            setSkeleton(false);
            setStatus('連線中', 'live');
          });

          hls.on(Hls.Events.LEVEL_LOADED, () => {
            // 每次 segment 載入成功就更新一下狀態（讓人知道串流仍在進行）
            if (statusEl.classList.contains('live')) {
              setStatus('連線中', 'live');
            }
          });

          hls.on(Hls.Events.ERROR, (_, data) => {
            if (data.fatal) {
              setSkeleton(true);
              if (data.type === Hls.ErrorTypes.NETWORK_ERROR) {
                setStatus('網路錯誤，嘗試重新載入...', '');
                setTimeout(() => hls && hls.startLoad(), 1500);
              } else if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
                setStatus('影像錯誤，嘗試修復...', '');
                hls.recoverMediaError();
              } else {
                setStatus(`串流錯誤：${data.details}`, 'error');
              }
            }
          });

          // video 恢復播放後隱藏 skeleton
          video.addEventListener('playing', () => setSkeleton(false), { once: false });
          video.addEventListener('waiting', () => setSkeleton(true),   { once: false });

        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          // Safari native HLS
          video.src = url;
          video.play().catch(() => {});
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
    function fmtClock(sec) {
      if (!isFinite(sec) || sec < 0) sec = 0;
      sec = Math.floor(sec);
      const m = Math.floor(sec / 60), s = sec % 60;
      return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
    }

    // Range over which the user may scrub, expressed in *video time*.
    function getSeekRange() {
      let start = 0, end = 0;
      if (!isLive && isFinite(video.duration) && video.duration > 0) {
        start = 0; end = video.duration;
      } else if (video.seekable && video.seekable.length) {
        start = video.seekable.start(0);
        end   = video.seekable.end(video.seekable.length - 1);
      } else if (isFinite(video.duration) && video.duration > 0) {
        end = video.duration;
      }
      const hi = end > start ? end : Infinity;
      const cur = Math.min(Math.max(video.currentTime || 0, start), hi);
      return { start, end, cur };
    }

    function updateTransport() {
      if (!transportEl) return;
      playBtn.textContent = video.paused ? '▶' : '⏸';
      const { start, end, cur } = getSeekRange();
      const span = Math.max(end - start, 0.001);
      const frac = transportDragging ? dragFrac : (cur - start) / span;
      const cf   = Math.min(Math.max(frac, 0), 1);
      const pct  = (cf * 100) + '%';
      seekProgress.style.width = pct;
      seekHandle.style.left    = pct;

      let bEnd = start;
      for (let i = 0; i < video.buffered.length; i++) {
        if (video.buffered.start(i) <= cur + 0.25 && video.buffered.end(i) >= cur - 0.25) {
          bEnd = Math.max(bEnd, video.buffered.end(i));
        }
      }
      seekBuffered.style.width = (Math.min(Math.max((bEnd - start) / span, 0), 1) * 100) + '%';
      seekTrack.setAttribute('aria-valuenow', String(Math.round(cf * 100)));

      transportEl.classList.toggle('is-vod', !isLive);

      if (!isLive) {
        timeCurEl.textContent = fmtClock(cur - start);
        timeDurEl.textContent = fmtClock(span);
        timeCurEl.title = new Date((vodStartTs + cur) * 1000).toLocaleString('zh-TW');
        liveBtnT.dataset.state = 'vod';
        liveLabelT.textContent = '即時串流';
      } else {
        const ideal  = (hls && isFinite(hls.liveSyncPosition)) ? hls.liveSyncPosition : end;
        const behind = Math.max(ideal - cur, 0);
        if (behind > 4) {
          timeCurEl.textContent = '-' + fmtClock(behind);
          timeDurEl.textContent = 'LIVE';
          timeCurEl.title = '已往回 ' + fmtClock(behind);
          liveBtnT.dataset.state = 'behind';
          liveLabelT.textContent = '回到即時';
        } else {
          timeCurEl.textContent = 'LIVE';
          timeDurEl.textContent = 'LIVE';
          timeCurEl.title = '';
          liveBtnT.dataset.state = 'at-edge';
          liveLabelT.textContent = 'LIVE';
        }
      }
    }

    function seekToFraction(frac) {
      frac = Math.min(Math.max(frac, 0), 1);
      const { start, end } = getSeekRange();
      if (end <= start) return;
      let target = start + frac * (end - start);
      // Avoid seeking to the exact live edge — it stalls; back off ~0.5s.
      if (isLive && end - target < 1) target = Math.max(start, end - 0.5);
      try { video.currentTime = target; } catch (_) {}
      scheduleTrackingFetch(true);
    }

    function commitDragSeek() {
      if (seekCommitTimer) { dragSeekPending = true; return; }
      seekToFraction(dragFrac);
      seekCommitTimer = setTimeout(() => {
        seekCommitTimer = null;
        if (dragSeekPending) { dragSeekPending = false; commitDragSeek(); }
      }, 150);
    }

    function fracFromEvent(ev) {
      const rect = seekTrack.getBoundingClientRect();
      const clientX = ev.clientX != null ? ev.clientX
        : (ev.touches && ev.touches[0] ? ev.touches[0].clientX : rect.left);
      return Math.min(Math.max((clientX - rect.left) / Math.max(rect.width, 1), 0), 1);
    }

    seekTrack.addEventListener('pointerdown', (ev) => {
      ev.preventDefault();
      transportDragging = true;
      seekTrack.classList.add('dragging');
      try { seekTrack.setPointerCapture(ev.pointerId); } catch (_) {}
      dragFrac = fracFromEvent(ev);
      commitDragSeek();
    });
    seekTrack.addEventListener('pointermove', (ev) => {
      if (!transportDragging) return;
      dragFrac = fracFromEvent(ev);
      commitDragSeek();
    });
    function endTransportDrag() {
      if (!transportDragging) return;
      transportDragging = false;
      seekTrack.classList.remove('dragging');
      clearTimeout(seekCommitTimer); seekCommitTimer = null; dragSeekPending = false;
      seekToFraction(dragFrac);
    }
    seekTrack.addEventListener('pointerup', endTransportDrag);
    seekTrack.addEventListener('pointercancel', endTransportDrag);
    seekTrack.addEventListener('lostpointercapture', endTransportDrag);

    seekTrack.addEventListener('keydown', (ev) => {
      const { start, end, cur } = getSeekRange();
      const span = Math.max(end - start, 0.001);
      if      (ev.key === 'ArrowLeft')  seekToFraction(((cur - start) - 5) / span);
      else if (ev.key === 'ArrowRight') seekToFraction(((cur - start) + 5) / span);
      else if (ev.key === 'Home')       seekToFraction(0);
      else if (ev.key === 'End')        seekToFraction(1);
      else return;
      ev.preventDefault();
    });

    playBtn.addEventListener('click', () => {
      if (video.paused) video.play().catch(() => {}); else video.pause();
    });

    function goToLiveEdge() {
      if (hls && isFinite(hls.liveSyncPosition)) {
        try { video.currentTime = hls.liveSyncPosition; } catch (_) {}
      } else if (video.seekable && video.seekable.length) {
        try { video.currentTime = video.seekable.end(video.seekable.length - 1) - 0.5; } catch (_) {}
      }
      video.play().catch(() => {});
    }

    function onLiveBtnClick() {
      if (!isLive) { switchToLive(); return; }
      goToLiveEdge();
    }

    // ── Init ──────────────────────────────────────────────────
    async function init() {
      try {
        const res = await fetch('/cameras');
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const { cameras } = await res.json();
        if (cameras.length === 0) throw new Error('no cameras');

        cameras.forEach(cam => {
          const opt = document.createElement('option');
          opt.value = cam;
          opt.textContent = cam;
          camSelect.appendChild(opt);
        });

        const cachedCam = (() => { try { return localStorage.getItem('lastCamera'); } catch (_) { return null; } })();
        currentCamera = (cachedCam && cameras.includes(cachedCam)) ? cachedCam : cameras[0];
        camSelect.value = currentCamera;
        loadStream();
        const today = new Date();
        currentMonth = new Date(today.getFullYear(), today.getMonth(), 1);
        selectedDay = localDayStart(today);
        loadTimeline();
        startLiveTimers();
        refreshAnomalyMap();
        refreshNotifications();
        loadSettings();
        pollStorageHealth();
        setInterval(pollStorageHealth, 20000);
      } catch (e) {
        setSkeleton(false);
        setStatus(`無法取得 camera 清單：${e.message}`, 'error');
      }
    }

    {
      const showReadEl = document.getElementById('show-read-toggle');
      if (showReadEl) {
        showReadEl.addEventListener('change', e => {
          showReadAlerts = e.target.checked;
          refreshNotifications();
        });
      }
    }

    camSelect.addEventListener('change', () => {
      currentCamera = camSelect.value;
      try { localStorage.setItem('lastCamera', currentCamera); } catch (_) {}
      stopLiveTimers();
      anomalyMap = {};
      vodAlerts  = [];
      currentObjectIds.clear();
      wsRetryCount = 0;
      latestBoxes = [];
      bboxHistory = [];
      countBadge.textContent = '—';
      if (!isLive) {
        isLive = true;
        liveBtn.style.display = 'none';
        detachVodListeners();
        clearTimeout(vodDebounceTimer);
        clearTimeout(trackingFetchTimer);
        trackingCache.clear();
        document.querySelectorAll('.timeline-slot.selected')
          .forEach(s => s.classList.remove('selected'));
      }
      clearSelection();
      if (typeof closeDeleteModal === 'function') closeDeleteModal();
      if (typeof closeBookmarkEditModal === 'function') closeBookmarkEditModal();
      if (typeof closeSlotActionMenu === 'function') closeSlotActionMenu();
      loadStream();
      loadTimeline();
      startLiveTimers();
      refreshAnomalyMap();
      refreshNotifications();
      if (typeof loadBookmarks === 'function') loadBookmarks();
    });

    // pig 列表：可點欄頭排序 + 「只顯示選取」開關（一次性綁定）
    document.querySelectorAll('#pig-status-table th.sortable').forEach(th => {
      th.addEventListener('click', () => onSortHeaderClick(th.dataset.sort));
    });
    {
      const soloCb = document.getElementById('solo-checkbox');
      if (soloCb) soloCb.addEventListener('change', e => { soloMode = e.target.checked; });
    }
    {
      const t = document.getElementById('select-mode-toggle');
      if (t) t.addEventListener('change', e => {
        selectMode = e.target.checked;
        clearSelection();
      });
    }

    init();
