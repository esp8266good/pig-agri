# Phase 5 — 分析與通知 Design Spec

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 以 3σ 異常偵測分析豬隻活動量與體溫，將結果寫入 `health_alerts`，前端即時標示異常 bbox 並提供豬隻狀態面板與通知中心。

**Architecture:** asyncio 定時排程（lifespan task）每 `ANALYSIS_INTERVAL_MINUTES` 分鐘掃描過去 30 分鐘的 `tracking_logs`，對每個 `(camera_id, object_id)` 跑 3σ 分析，結果寫入 DB 並更新 in-memory `anomaly_cache`。前端 Live 模式每 30 秒 poll `/alerts/active` 取得快取，VOD 模式在 `loadVod()` 時一次抓歷史 alert，在 `onVodTimeUpdate()` 依播放時間動態套用。

**Tech Stack:** asyncpg pool、numpy（已有）、FastAPI、原生 JS

---

## 檔案清單

| 動作 | 檔案 | 說明 |
|------|------|------|
| 修改 | `config.py` | 移除 `analysis_window_hours`，改為 `analysis_window_minutes: int = 30` |
| 新增 | `analysis/scheduler.py` | 排程迴圈、3σ 分析、in-memory anomaly_cache |
| 修改 | `analysis/__init__.py` | export `Scheduler` class |
| 修改 | `db_writer.py` | 新增 `write_health_alert`、`query_health_alerts`、`mark_alert_read` |
| 修改 | `routers/alerts.py` | 實作 `GET /alerts`、`GET /alerts/active`、`PUT /alerts/{id}/read` |
| 修改 | `main.py` | lifespan 啟動 / 停止 Scheduler |
| 修改 | `static/index.html` | 底部 tab、豬隻狀態面板、通知中心、異常 bbox overlay、鈴鐺 badge |
| 新增 | `tests/test_analysis_scheduler.py` | 分析邏輯單元測試 |
| 新增 | `tests/test_alerts_router.py` | alerts endpoint 整合測試 |
| 修改 | `tests/test_db_writer.py` | 新增三個 DB 函式的測試 |
| 修改 | `tests/test_main.py` | 移除 `/alerts` stub 斷言 |

---

## 資料庫

`health_alerts` 表已在 `sql/init.sql` 建立，欄位如下：

```sql
id            BIGSERIAL PRIMARY KEY
camera_id     VARCHAR(16) NOT NULL
object_id     INTEGER NOT NULL
triggered_at  TIMESTAMPTZ DEFAULT NOW()
metric        VARCHAR(32) NOT NULL   -- 'activity' | 'temperature'
current_value REAL
mean_value    REAL
std_value     REAL
is_read       BOOLEAN DEFAULT FALSE
```

---

## config.py 變更

移除：
```python
analysis_window_hours: int = 6
```

新增：
```python
analysis_window_minutes: int = 30
```

---

## db_writer.py 新增函式

### `write_health_alert`

```python
async def write_health_alert(
    pool: asyncpg.Pool,
    *,
    camera_id: str,
    object_id: int,
    metric: str,          # 'activity' | 'temperature'
    current_value: float,
    mean_value: float,
    std_value: float,
) -> int:
    """Insert a health alert. Returns the new alert id."""
    row = await pool.fetchrow(
        """INSERT INTO health_alerts
           (camera_id, object_id, metric, current_value, mean_value, std_value)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        camera_id, object_id, metric, current_value, mean_value, std_value,
    )
    return row["id"]
```

### `query_health_alerts`

```python
async def query_health_alerts(
    pool: asyncpg.Pool,
    camera_id: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: float | None = None,   # Unix epoch 秒，過濾 triggered_at
    end_ts: float | None = None,
) -> list[dict]:
    """
    Returns list of dicts:
      id, camera_id, object_id, metric, current_value, mean_value, std_value,
      is_read, triggered_at_unix (float, Unix epoch seconds)
    """
    conditions = []
    params: list = []
    idx = 1

    if camera_id is not None:
        conditions.append(f"camera_id=${idx}"); params.append(camera_id); idx += 1
    if unread_only:
        conditions.append("is_read = FALSE")
    if start_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) >= ${idx}"); params.append(start_ts); idx += 1
    if end_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) < ${idx}"); params.append(end_ts); idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit); limit_ph = f"${idx}"

    sql = f"""
        SELECT id, camera_id, object_id, metric,
               current_value, mean_value, std_value, is_read,
               EXTRACT(EPOCH FROM triggered_at)::float AS triggered_at_unix
        FROM health_alerts
        {where}
        ORDER BY triggered_at DESC
        LIMIT {limit_ph}
    """
    rows = await pool.fetch(sql, *params)
    return [dict(r) for r in rows]
```

### `mark_alert_read`

```python
async def mark_alert_read(pool: asyncpg.Pool, alert_id: int) -> bool:
    """Mark alert as read. Returns True if found, False if not found."""
    result = await pool.execute(
        "UPDATE health_alerts SET is_read = TRUE WHERE id = $1",
        alert_id,
    )
    return result != "UPDATE 0"
```

---

## analysis/scheduler.py

### In-memory cache 結構

```python
# module-level dict, 由 Scheduler 獨佔寫入，routers 只讀
_anomaly_cache: dict[str, dict[int, dict]] = {}
# _anomaly_cache[camera_id][object_id] = {
#   "activity_anomaly": bool,
#   "temp_anomaly": bool,
#   "activity_current": float | None,
#   "activity_mean": float | None,
#   "activity_std": float | None,
#   "temp_current": float | None,
#   "temp_mean": float | None,
#   "temp_std": float | None,
# }

def get_anomaly_cache() -> dict:
    return _anomaly_cache
```

### Scheduler class

```python
class Scheduler:
    def __init__(self, pool: asyncpg.Pool, settings) -> None: ...
    async def start(self) -> None:
        # 1. 從 DB 重建 cache（讀各 camera/object 最新一筆 alert）
        # 2. asyncio.create_task(self._loop())
    async def stop(self) -> None:
        # cancel task
    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self._interval_seconds)
            await self._run_analysis()
    async def _run_analysis(self) -> None: ...
```

### `_run_analysis` 邏輯

```python
async def _run_analysis(self) -> None:
    now = time.time()
    window_start = now - self._settings.analysis_window_minutes * 60

    # 取所有 camera 在視窗內出現的 (camera_id, object_id) 組合
    rows = await self._pool.fetch(
        """SELECT DISTINCT camera_id, object_id
           FROM tracking_logs
           WHERE timestamp >= $1 AND timestamp < $2""",
        window_start, now,
    )

    for r in rows:
        camera_id, object_id = r["camera_id"], r["object_id"]
        logs = await self._pool.fetch(
            """SELECT bb_left, bb_top, bb_width, bb_height, thermal_intensity, timestamp
               FROM tracking_logs
               WHERE camera_id=$1 AND object_id=$2
                 AND timestamp >= $3 AND timestamp < $4
               ORDER BY timestamp""",
            camera_id, object_id, window_start, now,
        )
        if len(logs) < self._settings.anomaly_min_samples:
            continue

        # 活動量：連續 frame bbox 中心位移
        centers = [
            (log["bb_left"] + log["bb_width"] / 2, log["bb_top"] + log["bb_height"] / 2)
            for log in logs
        ]
        displacements = [
            math.hypot(centers[i][0] - centers[i-1][0], centers[i][1] - centers[i-1][1])
            for i in range(1, len(centers))
        ]

        # 體溫：thermal_intensity 序列（過濾 None）
        temps = [log["thermal_intensity"] for log in logs if log["thermal_intensity"] is not None]

        entry = _anomaly_cache.setdefault(camera_id, {}).setdefault(object_id, {
            "activity_anomaly": False, "temp_anomaly": False,
            "activity_current": None, "activity_mean": None, "activity_std": None,
            "temp_current": None, "temp_mean": None, "temp_std": None,
        })

        # 活動量 — 單尾低
        if len(displacements) >= 2:
            mean_a = float(np.mean(displacements))
            std_a = float(np.std(displacements))
            current_a = displacements[-1]
            entry.update({"activity_current": current_a, "activity_mean": mean_a, "activity_std": std_a})
            if std_a > 0 and current_a < mean_a - self._settings.anomaly_std_threshold * std_a:
                entry["activity_anomaly"] = True
                await write_health_alert(self._pool, camera_id=camera_id, object_id=object_id,
                    metric="activity", current_value=current_a, mean_value=mean_a, std_value=std_a)
            else:
                entry["activity_anomaly"] = False

        # 體溫 — 雙尾
        if len(temps) >= 2:
            mean_t = float(np.mean(temps))
            std_t = float(np.std(temps))
            current_t = temps[-1]
            entry.update({"temp_current": current_t, "temp_mean": mean_t, "temp_std": std_t})
            if std_t > 0 and abs(current_t - mean_t) > self._settings.anomaly_std_threshold * std_t:
                entry["temp_anomaly"] = True
                await write_health_alert(self._pool, camera_id=camera_id, object_id=object_id,
                    metric="temperature", current_value=current_t, mean_value=mean_t, std_value=std_t)
            else:
                entry["temp_anomaly"] = False
```

### 啟動時重建 cache

從 `health_alerts` 讀各 `(camera_id, object_id, metric)` 的最新一筆，重建 `_anomaly_cache`（只設 `*_anomaly: bool`，不設 current/mean/std 數值，等下次分析補齊）。

---

## routers/alerts.py

```python
GET  /alerts/active?camera_id=
     → from analysis.scheduler import get_anomaly_cache
     → 若有 camera_id 只回傳該 camera；否則回傳全部
     → 格式：{ "cache": { "cam_01": { "3": {...}, "7": {...} } } }

GET  /alerts?camera_id=&unread_only=false&limit=50&start_ts=&end_ts=
     → query_health_alerts(pool, ...)
     → 格式：{ "alerts": [...], "total": n }
     → 每筆含 triggered_at_unix (float)

PUT  /alerts/{id}/read
     → mark_alert_read(pool, id)
     → 404 if not found
     → 200 { "ok": true }
```

---

## main.py 變更

```python
# lifespan 中
scheduler = Scheduler(pool, settings)
await scheduler.start()
...
await scheduler.stop()
```

---

## 前端設計

### Header

```html
<header>
  <span>豬隻疾病監測系統</span>
  <button id="bell-btn">🔔 <span id="bell-badge">0</span></button>
</header>
```

`bell-badge` 數字來自 `GET /alerts?unread_only=true`（不帶 `camera_id`，顯示所有 camera 的未讀總數），每 30 秒更新（與 anomalyMap 同一個 interval）。

---

### 底部 Tab 區（新增）

```html
<div id="bottom-panel">
  <div id="tab-bar">
    <button class="tab-btn active" data-tab="pig-status">豬隻狀態</button>
    <button class="tab-btn" data-tab="notifications">通知中心</button>
  </div>
  <div id="tab-pig-status" class="tab-content active">
    <table id="pig-status-table">...</table>
  </div>
  <div id="tab-notifications" class="tab-content">
    <ul id="alert-list">...</ul>
  </div>
</div>
```

---

### 豬隻狀態面板

- 資料來源：`GET /alerts/active?camera_id={cam}`
- 只顯示「最近一次 WS frame 中出現的 object_id」（`currentObjectIds` Set，每次 WS 訊息更新）
- 每列顯示：豬隻 ID、活動量數值 + 狀態（正常 / ⚠ 異常）、體溫數值 + 狀態（正常 / 🌡 異常）
- 異常列背景色標記（淡紅）

---

### 通知中心

- 資料來源：`GET /alerts?camera_id={cam}&unread_only=false&limit=50`
- 每筆顯示：camera ID、豬隻 ID、metric（活動量/體溫）、時間、偏差 σ 數（`(current - mean) / std`）、[標記已讀] 按鈕
- 點擊整列：切換至對應 camera + `loadVod(triggered_at_unix - 1800, triggered_at_unix + 300)` 跳至異常時段
- 未讀筆數 badge 同步更新

---

### Bbox 與異常狀態同步保證

`drawBoxes(objects)` 在每次呼叫時同時取用兩個資料來源：

| 資料 | Live 來源 | VOD 來源 |
|------|-----------|----------|
| bbox 座標 | WebSocket `objects` payload（即時） | `/tracking/{cam}?start=&end=` 查詢結果 |
| 異常狀態 | `anomalyMap`（30 秒 poll） | `vodAlerts` 依播放時間計算 |

由於圖示畫在當次 `objects` 裡的 `(x, y)` 座標上，不論異常狀態多久更新一次，位置永遠對應當下可見的 bbox。30 秒的 anomaly 狀態落差對 30 分鐘視窗的異常判斷而言可忽略不計。

**必須清空 anomalyMap 的時機：**
- 切換 camera（`camSelect` onChange）：先清空 `anomalyMap = {}`，再呼叫 `refreshAnomalyMap()`，避免舊 camera 的異常狀態套用到新 camera 的 bbox
- 切換至 Live 模式（`switchToLive()`）：先清空 `anomalyMap = {}`，再啟動 30 秒 poll
- 進入 VOD 模式（`loadVod()`）：停止 Live poll，清空 `anomalyMap = {}`，改由 `updateVodAnomalyMap()` 管理

---

### anomalyMap 管理

```javascript
let anomalyMap = {};        // { object_id: { activity_anomaly, temp_anomaly } }
let liveAnomalyTimer = null;
let vodAlerts = [];          // loadVod() 時抓，VOD 模式用

// Live 模式
async function refreshAnomalyMap() {
    const data = await fetch(`/alerts/active?camera_id=${currentCam}`).then(r => r.json());
    const camCache = data.cache?.[currentCam] ?? {};
    anomalyMap = {};
    for (const [oid, info] of Object.entries(camCache)) {
        anomalyMap[parseInt(oid)] = info;
    }
}

// VOD 模式：onVodTimeUpdate() 中
function updateVodAnomalyMap(currentTs) {
    anomalyMap = {};
    for (const alert of vodAlerts) {
        const winStart = alert.triggered_at_unix - 1800;
        const winEnd = alert.triggered_at_unix;
        if (currentTs >= winStart && currentTs <= winEnd) {
            const entry = anomalyMap[alert.object_id] ?? { activity_anomaly: false, temp_anomaly: false };
            if (alert.metric === "activity") entry.activity_anomaly = true;
            if (alert.metric === "temperature") entry.temp_anomaly = true;
            anomalyMap[alert.object_id] = entry;
        }
    }
}
```

---

### drawBoxes() 變更

```javascript
function drawBoxes(objects) {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const obj of objects) {
        const [x, y, w, h] = obj.bbox;
        const anomaly = anomalyMap[obj.object_id];
        const isAnomalous = anomaly && (anomaly.activity_anomaly || anomaly.temp_anomaly);

        ctx.strokeStyle = isAnomalous ? "#ff4444" : "#00ff88";
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, w, h);

        // 豬隻 ID 標籤
        ctx.fillStyle = isAnomalous ? "#ff4444" : "#00ff88";
        ctx.font = "12px monospace";
        ctx.fillText(`#${obj.object_id}`, x + 2, y - 4);

        // 異常圖示
        if (anomaly) {
            let icons = "";
            if (anomaly.activity_anomaly) icons += "⚠";
            if (anomaly.temp_anomaly) icons += "🌡";
            if (icons) ctx.fillText(icons, x + 2, y + 14);
        }
    }
}
```

---

## API 回傳格式確認

### `GET /alerts/active?camera_id=cam_01`

```json
{
  "cache": {
    "cam_01": {
      "3": {
        "activity_anomaly": true,
        "temp_anomaly": false,
        "activity_current": 12.4,
        "activity_mean": 38.1,
        "activity_std": 8.5,
        "temp_current": null,
        "temp_mean": null,
        "temp_std": null
      }
    }
  }
}
```

### `GET /alerts?camera_id=cam_01&limit=50`

```json
{
  "alerts": [
    {
      "id": 1,
      "camera_id": "cam_01",
      "object_id": 3,
      "metric": "activity",
      "current_value": 12.4,
      "mean_value": 38.1,
      "std_value": 8.5,
      "is_read": false,
      "triggered_at_unix": 1746444720.0
    }
  ],
  "total": 1
}
```

---

## 測試策略

### `tests/test_analysis_scheduler.py`（AsyncMock pool）

- 活動量低於 mean - 3σ → 觸發 alert，cache 設 `activity_anomaly=True`
- 活動量正常 → 不觸發，cache 設 False
- 體溫雙尾偏高 → 觸發
- 體溫雙尾偏低 → 觸發
- 體溫正常 → 不觸發
- 樣本不足（< `anomaly_min_samples`）→ 跳過

### `tests/test_alerts_router.py`（TestClient + mock db_writer）

- `GET /alerts/active` 無 camera_id → 回傳全部 cache
- `GET /alerts/active?camera_id=cam_01` → 回傳單 camera
- `GET /alerts` 基本查詢
- `GET /alerts?unread_only=true`
- `PUT /alerts/1/read` → 200
- `PUT /alerts/999/read` → 404

### `tests/test_db_writer.py` 補充

- `write_health_alert` 回傳 id
- `query_health_alerts` 時間過濾（`start_ts` / `end_ts`）
- `mark_alert_read` 成功回傳 True；不存在 id 回傳 False
