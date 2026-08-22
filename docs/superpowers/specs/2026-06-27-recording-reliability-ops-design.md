# 錄影可靠性 + ops 推播 + 夜間省電 設計

**日期**：2026-06-27
**分支**：`feat/recording-reliability-ops`
**承上**：`2026-06-13-storage-resilience-monitoring`（儲存韌性子系統）

## 1. 背景與問題

使用者已把 `HLS_BASE_DIR` 換成穩定內接碟以降低硬碟導致的錄影問題，但仍回報四件事：

1. **錄影偶爾突然停止，需重啟程式才恢復**（已確認**白天也會發生**，與夜間 no-record 排程無關 → 存在真實 bug）。
2. 需確認「最低可用空間」偵測的是**影像儲存碟**而非只看系統碟。
3. RAMFS（ephemeral）狀態下發生告警或錄影異常時，希望用 **ntfy** 推播到手機（endpoint：`https://ntfy.example.com/your-topic`）。
4. 夜間希望可選擇**停止調用 GPU 運算**省電。

本 spec 涵蓋四者：`#1` 為核心修復，`#2` 為確認（無需改 code），`#3`/`#4` 為附加功能。順序 `#1 → #3 → #4`（`#3` 依賴 `#1` 產生的錄影停止/恢復事件）。

## 2. 範圍

* **In scope**：錄影監督者 + 例外硬化 + 可觀測性；ntfy 推播通知；夜間停 GPU 排程開關；相關 DB-backed 設定接線與前端面板欄位。
* **Out of scope**：完整卸載/重載 GPU 模型（YAGNI，閒置 GPU 已省大部分功耗）；豬隻健康告警推播 ntfy（使用者選擇不含，避免推播被洗版）；系統碟死亡的防護（DB+app 本身都死，監控無從運作，沿用既有限制）。

---

## 3. #1 錄影監督者 + 硬化（核心修復）

### 3.1 根因（架構性，非單一行）

錄影 == live HLS 管線：同一條 ffmpeg 既寫 `.ts` 到磁碟（錄影）也供前端播放（live）。這條串流的脆弱點有三條互相疊加的死路：

1. **無監督者**：串流只由 `/live` 請求（開直播頁 / 切攝影機）經 `ensure_started` 建立。`zmq_receiver` 持續餵幀只餵給「已存在」的 stream，找不到就丟棄（`HLSManager.feed` 找不到 key → log debug → drop）。
2. **逐出後無人重建**：watchdog 在 `last_feed_time` 超過 `NO_FRAME_TIMEOUT`（30s）沒更新時把 stream 逐出（`_evict_stale`）。逐出後**沒有任何背景機制重建它來繼續錄影**，只能等下一個 `/live`。這正好解釋「重啟才好 / 切鏡頭有時救回」（切鏡頭觸發 `ensure_started`）。
3. **feed 例外殺掉接收 thread**：`HLSStream._restart`（每整點換目錄）裡的 `mkdir` / `_start_ffmpeg` 若丟例外，會一路往上傳到 `zmq_receiver._on_frame`；而 `_source_worker` 對 `on_frame(...)` 的呼叫**沒有 try/except**（`zmq_receiver.py:83`）→ 該攝影機接收 thread 整個死掉 → 不再餵幀 → 30s 後被逐出 → 錄影永久停。這會產生**整點對齊的空檔**。

先前 `5c8b733` 的「writer 自癒」只保護了 writer thread（BrokenPipe 不殺 writer + poll-based revive），沒保護上面 (2)(3) 兩條路徑。

### 3.2 修法（使用者選定：監督者 + 硬化）

**A. 錄影監督者**（`main.py` 新增 `_recording_supervisor_loop`）

* 背景 async task，每 `_SUPERVISOR_INTERVAL_SECONDS`（預設 10s）一輪。
* 對每個 `app_settings.zmq_sources` 的攝影機 label：當 `storage_monitor.get_target_mode() != "drop"` 時，呼叫 `hls_manager.ensure_started(label, "rgb")`。`ensure_started` 既有語意是「key 不存在才建」→ 已存在的健康 stream 不受影響；被逐出/不存在的會被重建。
* **thermal**：僅當該攝影機近期有送 thermal 幀時才一併 `ensure_started(label, "thermal")`。為避免對「從未裝 thermal」的攝影機平白起一條永遠無資料的 ffmpeg，由 `HLSManager` 記錄「最近是否餵過該 (cam, stream_type)」的時間戳，監督者只 ensure「近 N 秒餵過」或「rgb（一定有）」的串流。rgb 一律 ensure。
* 結果：錄影自此 24/7 獨立於觀看者；夜間照常由 target_mode 轉 ephemeral；任一環節出錯，下一輪（≤10s）自動重建。
* DB 不可用不影響本 loop（只依賴 `app_settings.zmq_sources` 與 `target_mode`，皆記憶體內）。

**B. 硬化例外邊界**

* `zmq_receiver._source_worker`：把 `on_frame(...)` 呼叫包 try/except，例外只 log warning、`continue`，**不再 break 出接收迴圈**（單一 feed 例外不能殺掉整條攝影機接收 thread）。
* `HLSStream._restart`：`mkdir` / `_start_ffmpeg` 包 try/except。失敗時：log warning、**不向上拋**；保留 stream 於可由 writer `poll()` 自癒（下輪 `_writer_tick` 偵測 dead proc → `_restart_in_place`）或監督者重建的狀態。避免整點換目錄一次失敗就讓 feed 例外冒泡。

**C. 可觀測性**

* 監督者重建 stream、`_restart` 失敗、watchdog eviction：皆加明確 WARNING log（含 cam/stream_type/原因）。萬一仍有漏網之魚，下次發生能從 log 釘到精確觸發點。

### 3.3 取捨

* 每攝影機 24/7 常駐一條 ffmpeg（錄影系統本該如此，資源可接受）。
* 不刪 watchdog eviction（仍清理真死的 stream），靠監督者快速重建（≤10s 空檔）。
* `drop`（雙碟全死）時監督者不重建——無處可寫，沿用既有限制。

---

## 4. #2 最低可用空間偵測對象（確認，無需改 code）

**結論**：已正確偵測錄影碟。`main.py._storage_monitor_loop` 呼叫 `storage_monitor.monitor.run_once(recording_base=app_settings.hls_base_dir, ephemeral_base=...)`；`check_free_space` 對 `recording_base` 做 `os.statvfs()` → 量的是 `HLS_BASE_DIR` 所在那顆檔案系統（使用者換的內接碟），ephemeral（/dev/shm）另外獨立量，**不是只看系統碟**。

**唯一前提**：`HLS_BASE_DIR` 真的掛在那顆獨立碟上。若該路徑實際落在系統碟分割區，`statvfs` 就會反映系統碟。spec 僅記錄此結論並提醒部署時確認掛載；無 code 改動。

---

## 5. #3 ntfy 推播

### 5.1 模組

新模組 `ntfy_notifier.py`：

* `async def notify(title: str, message: str, *, priority: str = "default", tags: str = "") -> None`：對 `ntfy_url` 做非阻塞 POST（HTTP POST，body=message，headers `Title`/`Priority`/`Tags`）。timeout（如 5s）+ try/except——網路失敗只 `logger.warning`，**絕不拋例外、絕不拖垮事件迴圈**。
* 傳輸：以 `urllib.request` 包在 `asyncio.to_thread`（或 executor）執行，避免新增依賴；若專案已有 `httpx` 則用 async client（實作時確認）。
* `ntfy_url` 空 / `ntfy_enabled=False` → 直接 return（停用）。

### 5.2 推播事件 + 優先級（使用者選定範圍，不含豬隻健康）

| 事件 metric | 觸發 | priority | tags |
|---|---|---|---|
| `storage_unwritable` | 錄影碟不可寫（record 狀態 → down） | urgent | 🚨 rotating_light |
| `storage_low_space` | 空間/inode 低於門檻（ok → degraded） | high | ⚠️ warning |
| `storage_recovered` | 任何 → ok 恢復 | default | ✅ white_check_mark |
| `recording_paused`（夜間排程轉 ephemeral，**碟健康**） | target_mode record→ephemeral 且 record 狀態為 ok | low | 🌙 moon |
| `recording_resumed` | target_mode ephemeral→record | default | ✅ |
| `recording_supervisor_revive` | 監督者重建 stream / ffmpeg 反覆復生 | high | ⚠️ |

* 故障型 ephemeral（碟壞導致）已由 `storage_unwritable` 涵蓋，**不重複推**——靠「record 狀態為 ok」條件區分排程型 vs 故障型 ephemeral。

### 5.3 掛點

* `storage_unwritable` / `storage_low_space` / `storage_recovered`：沿用 `main.py._storage_alert` 既有轉換回呼，回呼內除了寫 `health_alert` 再呼叫 `ntfy_notifier.notify`。
* `recording_paused` / `recording_resumed`：`storage_monitor.run_once` 新增 **target_mode 轉換偵測**（記前一輪 target_mode，變化時經 `alert_cb` 發新 metric，附帶「record 狀態」以便區分排程/故障）。`_storage_alert` 對應 metric 推 ntfy（排程型也寫一筆 `health_alert` 進通知中心保持一致）。
* `recording_supervisor_revive`：監督者重建 stream 時，經一個輕量回呼（注入 `_storage_alert` 同款）發 metric。

### 5.4 設定

`config.py` 新增：`ntfy_url`（預設 `https://ntfy.example.com/your-topic`）、`ntfy_enabled`（預設 True；URL 空時實際停用）。皆 DB-backed（`routers/settings.py` `ALLOWED_KEYS` + 前端面板），storage loop / 推播點每次讀 DB 即時生效。

---

## 6. #4 夜間停 GPU 省電

### 6.1 排程與旗標

* `config.py` 新增：`gpu_off_schedule_enabled`（**預設 False，不改現狀**）、`gpu_off_start`（預設 `"22:00"`）/ `gpu_off_end`（預設 `"06:00"`）（HH:MM，本地時間；預設停用故僅為佔位值）。DB-backed + 前端面板。
* 沿用 `storage_monitor.parse_hhmm` / 仿 `is_recording_time` 新增純函式 `is_inference_active(now, off_start_min, off_end_min, enabled) -> bool`（停用/無效/空窗 → 永遠 active；跨午夜邏輯同 `is_recording_time`）。
* `main.py._storage_monitor_loop` 既有每 ~20s DB tick 順便：resolve gpu 排程（從同一份 `db_settings`）→ 算 `is_inference_active(now, ...)` → 設 `inference_pipeline.set_active(bool)`。複用既有 DB-read 節奏，不另起 loop、不每 0.1s 讀 DB。

### 6.2 推論閘門

* `InferencePipeline` 新增 `self._active = True` + `set_active(bool)`（thread-safe 旗標）。
* `_process_batch`（或 `_loop` 取 snapshot 後）開頭檢查：`if not self._active: return`（不送 detector/ReID/tracker → GPU 閒置）。`_loop` 仍跑（廉價），只跳過 GPU 計算。

### 6.3 取捨

* 停用窗內無 tracking_logs、live bbox 凍結、無夜間活動量資料（夜間休息可接受，scheduler 既有夜間保護）。
* 模型仍常駐 VRAM（閒置 GPU 已省大部分功耗）；完整卸載/重載複雜且風險高，**不做**（YAGNI，已在 out-of-scope）。
* 預設 False → 上線零行為改變；使用者於設定面板自行啟用與調時段。

---

## 7. 設定接線總表（DB-backed，前端可即時調）

| key | 預設 | 消費點 | 生效延遲 |
|---|---|---|---|
| `ntfy_url` | `https://ntfy.example.com/your-topic` | `_storage_alert` / 推播點 | 即時（每次讀） |
| `ntfy_enabled` | True | 同上 | 即時 |
| `gpu_off_schedule_enabled` | False | storage loop tick → inference 旗標 | ≤20s |
| `gpu_off_start` | `"22:00"` | 同上 | ≤20s |
| `gpu_off_end` | `"06:00"` | 同上 | ≤20s |

皆加入 `routers/settings.py` `ALLOWED_KEYS`、GET 回退 dict、`sql/init.sql` seed（既有 live DB 需手動 upsert，沿用無 migration 系統的慣例）、前端 `static/index.html` 設定面板。

---

## 8. 測試策略

沿用既有 pytest 模式（純函式優先、subagent-driven 兩階段審查）：

* **#1**：`HLSManager` 監督者邏輯（ensure 對 drop/record/ephemeral 模式的行為、thermal 條件）；`_restart` 例外被吞不向上拋；`zmq_receiver` feed 例外不 break 接收迴圈（可用 mock feed 拋例外驗證迴圈續跑）。
* **#3**：`ntfy_notifier.notify` 在 URL 空/停用時 no-op、網路失敗只 log 不拋（mock POST）；storage_monitor target_mode 轉換偵測發對的 metric；排程型 vs 故障型 ephemeral 區分。
* **#4**：`is_inference_active` 純函式（跨午夜、停用、空窗、邊界）；`InferencePipeline.set_active(False)` 後 `_process_batch` 不呼叫 detector（mock detector 驗證 0 呼叫）。
* **settings**：新 key 可寫/讀回、`ALLOWED_KEYS` 擋未授權 key。

既有 4 個待辦 #12（ZMQ_SOURCES OS-env gap）失敗為已知非回歸。

---

## 9. 已知限制 / 待實測

* **#1**：`drop`（雙碟全死）+ 無觀看者的窄例外沿用既有限制；監督者重建後仍可能在同樣的下個整點再遇 `_restart` 失敗（但已不冒泡殺 thread，writer/監督者會持續嘗試）→ 若長時間實測仍有整點空檔，靠新增 log 釘精確觸發點再做針對性修正。
* **#3**：ntfy 推播依賴外網可達；endpoint 不可達時只 log，不重試佇列（YAGNI）。排程型夜間 ephemeral 每日推一則 low-priority，若嫌吵可後續加「排程型不推」開關。
* **#4**：時區僅固定 offset（UTC+8）正確，DST 會偏（沿用既有排程限制）；停用窗邊界判斷有 ≤20s（storage tick）延遲。

---

## 10. 驗收（使用者執行，現有測試無法涵蓋）

1. **#1**：白天長時間（≥24hr）運行不再出現需重啟才恢復的停錄；不開任何直播頁也持續錄影；故意製造整點換目錄失敗（如暫時改權限）→ 接收 thread 不死、≤10s 自動恢復、log 有明確訊息。
2. **#2**：確認 `HLS_BASE_DIR` 掛在內接碟；前端儲存燈/空間數字反映該碟。
3. **#3**：拔錄影碟 → 手機收到 `storage_unwritable` 推播；插回 → `storage_recovered`；夜間 17:00 → 收到 `recording_paused`（🌙）；空間低 → `storage_low_space`。
4. **#4**：啟用 gpu 排程 + 設時段 → 窗內 GPU 使用率/功耗下降、live bbox 凍結；窗外恢復；預設關閉時行為與現狀一致。
