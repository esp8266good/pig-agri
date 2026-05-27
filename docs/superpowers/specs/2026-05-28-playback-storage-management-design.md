# 回放儲存管理（保留 / 書籤 / 批量刪除）設計

> 狀態：設計已確認，待寫實作計畫。子系統 B（「設定優先序 + 回放儲存管理 + 前端體驗」三子系統 A→B→C 的第二塊）。承接子系統 A（retention 已讀 DB、`docs/superpowers/specs/2026-05-28-settings-wiring-retention-design.md`）。

## 1. 動機

採血需回看歷史，但 HLS 錄影受 `hls_retention_days` 週期性自動刪除（子系統 A 修好生效後更會準時刪）。操作者需要：
- **保留**：標記某鏡頭某時段，避免被自動刪除（例如採血當天的關鍵片段）。
- **書籤**：標記 + 命名某時段，日後從清單一鍵導航回去。書籤時段同樣不被自動刪除。
- **刪除**：批量選取鏡頭時段、確認後刪除存檔，回收磁碟；若誤選到保留/書籤項，強提醒防呆。

## 2. 範圍與決策（brainstorm 已確認）

- **時段最小單位 = 整小時**。對齊 HLS 儲存（`<cam>/<type>/<YYYY-MM-DD-HH>/`）、retention 刪除單位、timeline 格子。
- **保留與書籤合一張表**，`label` 可選：有 `label` = 書籤（命名、可導航）；`label` NULL = 純保留。兩者都擋自動刪除。
- **刪除連 DB 一起刪**：移除 HLS 小時目錄（rgb + thermal）+ 該時段 `tracking_logs` + `health_alerts` + 對應 `saved_segments` 列。不可逆。
- **保留/刪除涵蓋該攝影機該小時的所有 stream type**（rgb + thermal）。使用者想的是「鏡頭的某個時段」，非分型別。
- **書籤導航目標 = 該小時 VOD 起點**（`loadVod(hour_ts)`）。
- **互動 = timeline 多選 + 浮出操作列 + 書籤清單面板 + 格子鎖/星標記**。
- **檔案/邊界**：新 router `routers/storage.py`、`db_writer.py` 新增函式、`hls_retention.py` 擴充、`sql/init.sql` 新表、`static/index.html` 前端。
- **與子系統 C 相容**：選取/操作列/標記掛在「每小時格子」上；C 改的是日期導航（週→月曆），格子渲染仍在，B 不被 C 大改。

## 3. 資料模型

`sql/init.sql` 新增：

```sql
CREATE TABLE IF NOT EXISTS saved_segments (
    id         BIGSERIAL PRIMARY KEY,
    camera_id  VARCHAR(16) NOT NULL,
    hour_ts    BIGINT NOT NULL,        -- 該小時起點的 unix 秒（本地時區小時，對齊 timeline 格子與 hls 目錄）
    label      TEXT,                   -- 非 NULL = 書籤（命名、可導航）；NULL = 純保留
    note       TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (camera_id, hour_ts)
);
CREATE INDEX IF NOT EXISTS idx_saved_segments_cam ON saved_segments (camera_id, hour_ts);
```

語意：一列 = 「此攝影機這小時受保護、不被自動刪除」。`UNIQUE(camera_id, hour_ts)` 使「保留」與「書籤」同一小時 = 同一列（upsert）。
- 對已保留（label NULL）的小時下書籤 → upsert 補上 label/note（升級成書籤）。
- 對已書籤的小時按保留 → no-op（已受保護；不覆蓋 label）。
- 編輯把 label 清成空 → 降級成純保留。
- 刪標記（DELETE）→ 該小時不再受保護、不再是書籤（不動影片檔）。

`hour_ts` 與既有時間軸一致：timeline 格子的 `slotTs = currentWeekStart + i*3600`（unix 秒），前端傳什麼存什麼，避免時區換算分歧。

## 4. DB 函式（`db_writer.py`）

全為 async、用既有 asyncpg pool 慣例（與 `get_all_settings` 等同風格）：

- `list_saved_segments(pool, camera_id, start_ts, end_ts) -> list[dict]`
  `SELECT id, camera_id, hour_ts, label, note FROM saved_segments WHERE camera_id=$1 AND hour_ts >= $2 AND hour_ts < $3 ORDER BY hour_ts`。
- `list_bookmarks(pool, camera_id=None) -> list[dict]`
  書籤清單面板用：`WHERE label IS NOT NULL`（可選 camera_id 過濾），`ORDER BY hour_ts DESC`。
- `upsert_saved_segment(pool, camera_id, hour_ts, label=None, note=None) -> int`
  `INSERT ... ON CONFLICT (camera_id, hour_ts) DO UPDATE SET label=COALESCE($3,saved_segments.label), note=COALESCE($4,saved_segments.note) RETURNING id`。
  保留（label/note 皆 None）對既有列 = no-op 升級（保留原 label）；書籤（label 非 None）= 設定/覆蓋 label。
- `update_saved_segment(pool, seg_id, label, note) -> bool`
  明確 `SET label=$2, note=$3`（允許清空 label 成 NULL，即降級保留）。
- `delete_saved_segment(pool, seg_id) -> bool`。
- `get_protected_hours(pool) -> set[tuple[str, int]]`
  retention 用：`SELECT camera_id, hour_ts FROM saved_segments`，回 `{(camera_id, hour_ts)}`。
- `delete_recordings_in_range(pool, camera_id, start_ts, end_ts) -> dict`
  刪 DB 軌跡/告警：
  `DELETE FROM tracking_logs WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3`（timestamp 為 DOUBLE PRECISION unix 秒）；
  `DELETE FROM health_alerts WHERE camera_id=$1 AND EXTRACT(EPOCH FROM triggered_at) >= $2 AND EXTRACT(EPOCH FROM triggered_at) < $3`；
  回 `{"tracking_logs": n1, "health_alerts": n2}`（用 `result` 字串解析或 `RETURNING` 計數）。

## 5. 檔案系統函式（`hls_retention.py`）

- 擴充 `find_expired_hour_dirs(base_dir, retention_days, now, protected=None)`：
  `protected: set[tuple[str, int]] | None`（(camera_id, hour_unix)）。掃到過期小時目錄時，先把目錄名解析回 unix 小時、組 `(camera_id, hour_unix)`，若在 `protected` 內則**跳過不列入刪除**。`camera_id` 從路徑 `base/<cam>/<type>/<hour>` 的 `<cam>` 取得。預設 `None` = 不保護（向後相容既有呼叫與測試）。
- 擴充 `purge_expired_hls(base_dir, retention_days, now=None, protected=None)`：把 `protected` 透傳給 `find_expired_hour_dirs`。
- 新增 `delete_recording_hours(base_dir, camera_id, hour_ts_list) -> list[Path]`：
  對每個 `hour_ts`，把 unix 秒轉本地時區 `%Y-%m-%d-%H` 目錄名，刪 `base/<cam>/rgb/<name>` 與 `base/<cam>/thermal/<name>`（存在才刪、`shutil.rmtree`、容錯 log），回實際刪除的目錄清單。純函式（base_dir 注入），可測。

時區一致性：`hour_ts` 是前端 timeline 算出的 unix 秒；目錄名由 `hls_manager` 用 `datetime.now().strftime("%Y-%m-%d-%H")`（本地時區）產生。`delete_recording_hours` 與 `find_expired_hour_dirs` 都用 `datetime.fromtimestamp(hour_ts)`（本地時區）↔ 目錄名互轉，與產生端一致。

## 6. API（新 router `routers/storage.py`，prefix `/storage`）

無 pool（DB 不可用）一律回 503（與 settings router 慣例一致）。`camera_id` 驗證沿用 `settings.zmq_sources` label 檢查。

- `GET /storage/segments?camera_id=&start_ts=&end_ts=` → `{"segments": [...]}`（`list_saved_segments`）。供 timeline 標記。
- `GET /storage/bookmarks?camera_id=（可選）` → `{"bookmarks": [...]}`（`list_bookmarks`）。供書籤清單面板。
- `POST /storage/segments` body `{camera_id, hours:[hour_ts...], label?, note?}` → 對每個 hour `upsert_saved_segment`。回 `{"ok": true, "count": n}`。無 label = 保留；有 label = 書籤。
- `PUT /storage/segments/{id}` body `{label?, note?}` → `update_saved_segment`。404 若不存在。
- `DELETE /storage/segments/{id}` → `delete_saved_segment`。404 若不存在。
- `POST /storage/recordings/delete` body `{camera_id, hours:[hour_ts...]}` →
  對每 hour：`delete_recording_hours(base_dir, camera_id, [hour])` + `delete_recordings_in_range(pool, camera_id, hour, hour+3600)`；再 `delete_saved_segment` 該 (camera, hour) 對應列（若有）。回
  `{"ok": true, "deleted_hours": n, "dirs_removed": m, "tracking_logs": x, "health_alerts": y}`。

## 7. retention 整合（`main.py`）

`_retention_loop` 每輪：取得 pool 後，若可用則 `protected = await get_protected_hours(pool)`（失敗回退 `set()` 並 log warning）；`purge_expired_hls(app_settings.hls_base_dir, days, protected=protected)`。沿用子系統 A 的雙層 try/except。

## 8. 前端（`static/index.html`）

### 8.1 選取模式
- timeline 區上方加「選取」切換鈕（`#select-mode-toggle`）。`selectMode=false`（預設）時點 has-data 格子 = 既有 `loadVod(slotTs)`（行為不變）；`selectMode=true` 時點格子 = 切 `.slot-selected`、維護 `selectedHours: Set<number>`（hour_ts）。
- 切換到選取模式 / 關閉選取模式時清空 `selectedHours` 與視覺。切攝影機 / 切週 / 切 RGB↔Thermal / Live↔VOD 一律重置選取模式（沿用既有重置點）。

### 8.2 浮出操作列
- `selectedHours.size >= 1` 時顯示浮出列（`#storage-action-bar`），含：
  - **保留**：`POST /storage/segments {camera_id, hours:[...selected]}`（無 label）。成功後重載標記。
  - **書籤**：彈出命名輸入（label 必填、note 可選），`POST /storage/segments {camera_id, hours, label, note}`。
  - **刪除**：開確認 modal（見 8.4）。
  - **取消選取**：清空。

### 8.3 格子標記 + 書籤清單
- 載入 timeline 後 `GET /storage/segments?camera_id&start_ts&end_ts` → 受保護小時格子加 `.protected`（🔒）、書籤小時加 `.bookmarked`（★）的視覺標記（CSS class + 小圖示，疊在 has-data 樣式上）。
- 書籤清單面板（pig 區同層的一個分頁或側欄，沿用既有 tab 樣式）：`GET /storage/bookmarks?camera_id` 列出 `label`＋時間，點擊 → `loadVod(hour_ts)` 跳該小時回放。每項可「改名」（`PUT`）、「移除標記」（`DELETE`）。

### 8.4 刪除防呆 modal
- modal 列出將刪除的時段（日期＋小時，count）。
- 前端比對 `selectedHours` 與已載入的 protected/bookmarked 集合；若有交集 → modal 以**警示樣式**列出這些「已保留/書籤」時段，並顯示 checkbox「我了解這些時段已被保留/書籤，仍要刪除」，**未勾選則刪除鈕停用**。
- 無交集時仍需一次確認（一般確認鈕），避免誤刪。
- 確認 → `POST /storage/recordings/delete`，成功後刷新 timeline（`loadTimeline`）、標記、書籤清單，清空選取。提示刪除摘要。

## 9. 測試策略

- **`db_writer.py`**（`tests/test_db_writer.py`，沿用 mock asyncpg `AsyncMock` 慣例）：`upsert_saved_segment`（SQL 含 ON CONFLICT、參數正確）、`list_saved_segments`/`list_bookmarks`（回傳 dict 列）、`update_saved_segment`/`delete_saved_segment`（回傳 bool）、`get_protected_hours`（回 set of tuple）、`delete_recordings_in_range`（兩條 DELETE、回計數）。
- **`hls_retention.py`**（`tests/test_hls_retention.py`，沿用 tmp_path 慣例）：
  - `find_expired_hour_dirs` 帶 `protected` → 過期但受保護的目錄不在回傳；未保護的仍在；`protected=None` 行為同舊（向後相容既有 4 測試）。
  - `delete_recording_hours` → 刪 rgb + thermal 兩目錄、回清單；不存在的型別目錄跳過不報錯；不誤刪其他小時。
- **`routers/storage.py`**（新 `tests/test_storage_router.py`，沿用 `test_settings_router.py` 的 `_dummy_zmq_sources` + TestClient + mock pool 慣例）：各端點 happy path + 無 pool 503 + 不存在 404 + camera 驗證；`POST /storage/recordings/delete` mock 掉檔案刪除與 DB 刪除、驗證有被以正確參數呼叫。
- **`main.py` retention 整合**：`_retention_loop` 已是 daemon 迴圈不直接測；以 `get_protected_hours` 的 DB 函式測試 + retention `protected` 參數測試覆蓋。
- **前端**：`node --check`（抽 `<script>`）；無 JS 測試框架（沿用慣例）。
- **全套件**：`uv run pytest --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`，維持綠燈 + 新測試；4 既有 ZMQ_SOURCES 失敗無關。

## 10. 邊界與錯誤處理

- 刪除不可逆且連 DB；以 8.4 防呆 + 後端對未知 camera 回 404、無 pool 回 503。
- 刪除進行中目錄不存在（已被 retention 刪）→ `delete_recording_hours` 容錯跳過、不報錯。
- 受保護小時被使用者明確選入刪除並勾選確認 → 仍刪（使用者覆寫保留意圖；刪除同時移除其 saved_segments 列）。
- thermal 無資料的攝影機 → `delete_recording_hours` 該型別目錄不存在即跳過。
- `hour_ts` 對齊：前端送 timeline 同源 unix 秒；後端目錄名互轉一律本地時區，與 `hls_manager` 產生端一致。

## 11. 非目標（YAGNI）

- 跨攝影機一次批量（每次操作針對目前攝影機選取）。
- 任意分鐘級時段（整小時為單位）。
- 刪除後復原 / 回收筒。
- 書籤資料夾 / 標籤分類 / 排序偏好持久化。
- 保留/書籤的到期或配額管理。
- 後端對 `saved_segments` 做時區正規化（信任前端 timeline 同源 hour_ts）。
