# Phase 6：設定頁面 + VOD/BBox 修復 設計文件

## 目標

完成最後一個 Phase：實作設定頁面（前後端），同時修復兩個已知的歷史回放 bug（manifestLoadError、bbox 不同步）。

## 範疇

1. **Bug 修復**
   - VOD `manifestLoadError`（時區不一致 + 時間軸資料來源錯誤）
   - VOD bbox 混在一起不跟隨畫面
   - `switchToLive()` 缺少 `refreshNotifications()` 呼叫

2. **Phase 6：設定頁面**
   - `GET /settings`、`PUT /settings` 後端實作
   - Scheduler 即時熱重載
   - 前端設定 Tab UI

---

## Bug 修復設計

### Bug 1：VOD manifestLoadError

**根本原因 A（主因）：時區不一致**

`hls_manager._hour_dir()` 使用 `datetime.now()` 產生**本地時間**目錄名（例如 `2026-05-05-22`），但 `vod_generator.build_vod_m3u8()` 使用 `datetime.fromtimestamp(current_hour, tz=timezone.utc)` 產生 **UTC 時間**目錄名（例如 `2026-05-05-14`）。在 UTC+8 伺服器上，VOD generator 永遠找不到正確目錄，回傳 404，導致 `manifestLoadError`。

**修復：`vod_generator.py`**

```python
# 修改前
dt = datetime.fromtimestamp(current_hour, tz=timezone.utc)

# 修改後（對齊 hls_manager 的本地時間）
dt = datetime.fromtimestamp(current_hour)  # local time, no tz
```

PDT（`EXT-X-PROGRAM-DATE-TIME`）標籤需改為本地時間加時區偏移：

```python
from datetime import timezone as _tz
first_dt_local = datetime.fromtimestamp(first_ts).astimezone()
pdt = first_dt_local.strftime("%Y-%m-%dT%H:%M:%S") + first_dt_local.strftime("%z")[:3] + ":" + first_dt_local.strftime("%z")[3:]
```

**根本原因 B（次因）：時間軸來源為 DB，非磁碟**

前端時間軸呼叫 `/tracking/{camera_id}/timeline`（查 DB），但 VOD 回放依賴磁碟上的 `.ts` 檔案。兩者可能不同步。

**修復：新增 `/stream/{camera_id}/timeline` endpoint（掃磁碟）**

```
GET /stream/{camera_id}/timeline?start_ts=...&end_ts=...
→ { "hours": [1746403200, 1746406800, ...] }
```

實作：掃 `{HLS_BASE_DIR}/{camera_id}/rgb/` 下的子目錄名（以 `rgb` 為正式串流型別，thermal 不列入時間軸），用 `datetime.strptime(dir_name, "%Y-%m-%d-%H")` 解析為本地時間，再轉 Unix 整點時間戳，回傳落在 `[start_ts, end_ts)` 的小時列表。若目錄不存在則回傳空列表。

前端 `loadTimeline()` 改呼叫 `/stream/{camera_id}/timeline`，時間軸只標示真正有 HLS 資料可回放的小時。

---

### Bug 2：VOD bbox 混在一起

**根本原因：** `onVodTimeUpdate` 查詢 `±0.5` 秒視窗，在 25 FPS 下約取得 25 幀的 tracking 記錄，全部渲染導致同一隻豬的多個位置同時顯示。

**修復：前端 closest-frame 篩選（純 JS，無 API 變動）**

取得 `/tracking/{camera_id}?start=...&end=...` 結果後，依 `frame_id` 分組並選出 `timestamp` 最接近當前影像時間戳的 frame：

```js
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
```

`onVodTimeUpdate` 中將 `latestBoxes = data.logs || []` 改為 `latestBoxes = pickClosestFrame(data.logs || [], ts)`。

---

### Bug 3：switchToLive 缺少 refreshNotifications

`switchToLive()` 末尾加：

```js
refreshNotifications();
```

---

## Settings API 設計

### 資料層：`db_writer.py` 新增兩個函式

```python
async def get_all_settings(pool: asyncpg.Pool) -> dict[str, str]:
    rows = await pool.fetch("SELECT key, value FROM user_settings")
    return {r["key"]: r["value"] for r in rows}

async def upsert_settings(pool: asyncpg.Pool, updates: dict[str, str]) -> None:
    await pool.executemany(
        """INSERT INTO user_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
        [(k, v) for k, v in updates.items()],
    )
```

### Scheduler 熱重載：`analysis/scheduler.py`

新增 `reload()` 方法：

```python
def reload(self, interval_minutes: int, std_threshold: float) -> None:
    self._interval = interval_minutes * 60
    self._threshold = std_threshold
```

`_loop()` 中的 `await asyncio.sleep(self._interval)` 每次循環都讀取 `self._interval`，因此下一輪 sleep 結束後自動套用新值，不需要中斷當前 sleep。

### app.state 存放 Scheduler：`main.py`

```python
scheduler = Scheduler(database.get_pool(), app_settings)
await scheduler.start()
app.state.scheduler = scheduler  # 讓 router 可存取
```

### `routers/settings.py` 完整實作

**允許修改的 key：**
```python
ALLOWED_KEYS = frozenset({
    "jpeg_quality",
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
})
```

**GET /settings**：優先讀 DB；DB 不可用時退回環境變數預設值：
```python
@router.get("")
async def get_settings():
    pool = database.get_pool()
    if pool is None:
        return {
            "jpeg_quality":               str(app_settings.jpeg_quality),
            "analysis_interval_minutes":  str(app_settings.analysis_interval_minutes),
            "anomaly_std_threshold":      str(app_settings.anomaly_std_threshold),
            "hls_retention_days":         str(app_settings.hls_retention_days),
        }
    return await get_all_settings(pool)
```

**PUT /settings**：寫 DB，觸發熱重載：
```python
@router.put("")
async def update_settings(request: Request, body: dict[str, str]):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    if "analysis_interval_minutes" in updates or "anomaly_std_threshold" in updates:
        current = await get_all_settings(pool)
        request.app.state.scheduler.reload(
            interval_minutes=int(current["analysis_interval_minutes"]),
            std_threshold=float(current["anomaly_std_threshold"]),
        )
    return {"ok": True, "updated": list(updates.keys())}
```

---

## 前端設定 UI 設計

### 底部面板新增第三個 Tab

```
[ 豬隻狀態 ]  [ 通知中心 ]  [ 設定 ]
```

### 設定表單

| 設定項 | HTML 元件 | 限制 |
|--------|-----------|------|
| 影像壓縮品質 | `<input type="number" min="50" max="95">` | 整數，50–95 |
| 分析間隔 | `<select>` 選項：15 / 30 / 60 分鐘 | 三選一 |
| 異常閾值（σ） | `<input type="number" min="1.0" step="0.1">` | ≥ 1.0 浮點數 |
| 影像保留天數 | `<input type="number" min="1" max="365">` | 整數，1–365 |

底部「儲存設定」按鈕：呼叫 `PUT /settings`，成功顯示「✓ 已儲存」toast（3 秒），失敗顯示錯誤 toast。

### 初始化

`init()` 呼叫 `loadSettings()` 從 `GET /settings` 讀取初始值填入表單。

---

## 測試計畫

### `tests/test_vod_generator.py`（新增）
- 本地時間目錄名能被正確解析（不再使用 UTC）
- `build_vod_m3u8` 在磁碟目錄存在時回傳正確 m3u8 內容
- PDT 標籤格式正確（包含時區偏移）

### `tests/test_settings_router.py`（新增）
- `GET /settings` pool=None 時回傳環境變數預設值
- `GET /settings` 有 pool 時回傳 DB 值
- `PUT /settings` 寫入有效 key 回傳 `{"ok": True, ...}`
- `PUT /settings` 傳入無效 key 回傳 400
- `PUT /settings` 更新 `analysis_interval_minutes` 時呼叫 `scheduler.reload()`

### `tests/test_scheduler_reload.py`（或併入現有 scheduler test）
- `reload()` 正確更新 `_interval` 和 `_threshold`

---

## 檔案異動摘要

| 檔案 | 異動類型 |
|------|----------|
| `vod_generator.py` | 修改：本地時間修正 + PDT 格式 |
| `routers/stream.py` | 修改：新增 `/stream/{camera_id}/timeline` |
| `db_writer.py` | 修改：新增 `get_all_settings`、`upsert_settings` |
| `analysis/scheduler.py` | 修改：新增 `reload()` |
| `main.py` | 修改：`app.state.scheduler` |
| `routers/settings.py` | 修改：實作 GET + PUT |
| `static/index.html` | 修改：設定 tab、closest-frame、timeline endpoint、switchToLive fix |
| `tests/test_vod_generator.py` | 新增 |
| `tests/test_settings_router.py` | 新增 |
