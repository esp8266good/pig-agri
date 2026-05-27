# 設定接線修正 + retention 生效 + 移除 jpeg_quality 死設定 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓前端設的 `hls_retention_days` 下一輪巡檢即生效（免重啟），巡檢間隔 6h→1h，並移除全程無人消費的 `jpeg_quality` 死設定。

**Architecture:** retention loop 每輪從 DB（`user_settings` 表）讀有效保留天數，DB 不可用/缺鍵/壞值則回退建構時的 `app_settings` 值（單一權威＝DB，與 `scheduler._apply_db_settings` 同模式）。解析邏輯抽成 `hls_retention.py` 內的純函式 `effective_retention_days(db_settings, fallback_days)`（顯式 fallback 參數，免在測試 import 整個 app；此為對 spec「函式置於 main.py」的測試性精煉，行為相同）。jpeg_quality 從 config/settings router/init.sql seed/前端 UI 逐處移除。

**Tech Stack:** Python 3 (FastAPI, asyncpg, loguru), pytest（以 `uv run pytest` 執行——asyncpg 只在 uv venv 內）, vanilla JS（`node --check` 驗語法）。

**測試基線備註：** 本專案測試必須用 `uv run pytest`。既有基線有 4 個失敗（待辦 #12：`.env` 缺 `ZMQ_SOURCES` 的 OS-env gap，`test_config::test_default_mot_worker_threads` + 3 個 `test_stream_router`），與本計畫無關。執行全套件時排除需要實機/實 DB 的檔：`--ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`。`tests/test_database.py` 需要實際 Postgres 連線（`database.connect()`），無 DB 環境會 error；其 jpeg_quality 斷言仍須改正（見 Task 3），有 DB 時另行驗證。

---

### Task 1: `effective_retention_days` 純函式

**Files:**
- Modify: `hls_retention.py`（在 `purge_expired_hls` 之後新增函式）
- Test: `tests/test_hls_retention.py`（新增 4 個測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_hls_retention.py` 結尾（import 行同步加上 `effective_retention_days`）：

```python
from hls_retention import (
    find_expired_hour_dirs,
    purge_expired_hls,
    effective_retention_days,
)


def test_effective_retention_uses_db_value_when_present():
    assert effective_retention_days({"hls_retention_days": "30"}, 90.0) == 30.0


def test_effective_retention_falls_back_when_key_missing():
    assert effective_retention_days({"other": "1"}, 90.0) == 90.0


def test_effective_retention_falls_back_on_unparsable_value():
    assert effective_retention_days({"hls_retention_days": "abc"}, 90.0) == 90.0


def test_effective_retention_falls_back_when_db_settings_none():
    assert effective_retention_days(None, 90.0) == 90.0
```

（注意：`from hls_retention import find_expired_hour_dirs, purge_expired_hls` 已存在於檔首；改成同一 import 三件，不要重複 import 行。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_hls_retention.py -v`
Expected: FAIL — `ImportError: cannot import name 'effective_retention_days'`

- [ ] **Step 3: 實作純函式**

在 `hls_retention.py` 的 `purge_expired_hls` 之後新增：

```python
def effective_retention_days(
    db_settings: dict | None, fallback_days: float
) -> float:
    """DB 有 hls_retention_days 且可解析 → 用 DB 值（單一權威）；
    否則回退 fallback_days（呼叫端傳入 app_settings 建構時值）。"""
    if db_settings is not None:
        raw = db_settings.get("hls_retention_days")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return float(fallback_days)
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_hls_retention.py -v`
Expected: PASS（原 4 測試 + 新 4 測試 = 8 passed）

- [ ] **Step 5: Commit**

```bash
git add hls_retention.py tests/test_hls_retention.py
git commit -m "feat(retention): effective_retention_days 純函式（DB 值優先，可測）"
```

---

### Task 2: retention loop 讀 DB + 間隔改 1h

**Files:**
- Modify: `main.py`（import、`_RETENTION_INTERVAL_SECONDS`、`_retention_loop`）
- Test: `tests/test_settings_router.py`（新增 1 個間隔常數測試）

- [ ] **Step 1: 寫失敗測試**

加到 `tests/test_settings_router.py` 結尾（`_dummy_zmq_sources` 已定義於該檔上方）：

```python
def test_retention_interval_is_hourly():
    with _dummy_zmq_sources():
        import main
        assert main._RETENTION_INTERVAL_SECONDS == 3600
```

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings_router.py::test_retention_interval_is_hourly -v`
Expected: FAIL — `assert 21600 == 3600`（目前是 `6 * 3600`）

- [ ] **Step 3: 改 main.py**

3a. import 區（`from hls_retention import purge_expired_hls` 那行）改為：

```python
from hls_retention import effective_retention_days, purge_expired_hls
```

並在既有 `from db_writer import ...`（若無則新增一行）加入 `get_all_settings`。目前 `main.py` 沒有 import db_writer，新增：

```python
from db_writer import get_all_settings
```

3b. 常數：

```python
_RETENTION_INTERVAL_SECONDS = 1 * 3600
```

3c. `_retention_loop` 整段替換為：

```python
async def _retention_loop() -> None:
    """週期性刪除超過保留天數的 HLS 小時目錄，避免磁碟無限長大。
    每輪從 DB 讀 hls_retention_days（前端設定即時生效，免重啟）；DB 不可用 /
    讀取失敗 / 缺鍵 / 壞值 → 回退 app_settings 建構時值。
    先等一個間隔再首次巡檢（避免啟動時重磁碟 I/O；保留天數遠大於間隔，
    晚一輪清無妨），之後每 _RETENTION_INTERVAL_SECONDS 跑一次。"""
    while True:
        await asyncio.sleep(_RETENTION_INTERVAL_SECONDS)
        try:
            pool = database.get_pool()
            db_settings = None
            if pool is not None:
                try:
                    db_settings = await get_all_settings(pool)
                except Exception as e:
                    logger.warning(f"HLS retention 讀取 DB 設定失敗，回退 app_settings：{e}")
            days = effective_retention_days(
                db_settings, app_settings.hls_retention_days
            )
            purge_expired_hls(app_settings.hls_base_dir, days)
        except Exception as e:  # 巡檢失敗不可拖垮服務
            logger.warning(f"HLS retention 巡檢失敗：{e}")
```

3d. 更新檔首註解（約 line 21-23）說明天數每輪讀 DB：

```python
# HLS retention 巡檢間隔：每 1 小時掃一次過期小時目錄。保留天數每輪即時讀
# DB（user_settings.hls_retention_days），前端改設定免重啟生效；DB 不可用則
# 回退 app_settings 建構時值。
_RETENTION_INTERVAL_SECONDS = 1 * 3600
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_settings_router.py -v`
Expected: PASS（含新 `test_retention_interval_is_hourly`；其餘 settings 測試此刻仍綠——尚未動 ALLOWED_KEYS）

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_settings_router.py
git commit -m "feat(retention): loop 每輪讀 DB 保留天數 + 間隔 6h→1h"
```

---

### Task 3: 移除 jpeg_quality（後端）

**Files:**
- Modify: `config.py`（刪欄位）、`routers/settings.py`（ALLOWED_KEYS + GET 回退 dict）、`sql/init.sql`（seed）
- Test: `tests/test_settings_router.py`、`tests/test_database.py`、`tests/test_main.py`（改 jpeg_quality 斷言）

- [ ] **Step 1: 改測試（先讓它們反映新行為，預期先失敗）**

3-1a. `tests/test_settings_router.py`：
- `test_get_settings_no_pool_returns_env_defaults`（約 line 90-98）：刪掉 `assert "jpeg_quality" in data` 那行，其餘三個 key 斷言保留。
- `test_get_settings_with_pool_returns_db_values`（約 line 101-106）：把 `assert data.get("jpeg_quality") == "85"` 改為 `assert data.get("hls_retention_days") == "30"`（mock fetch 已含此鍵）。
- `test_put_settings_valid_keys_returns_ok`（約 line 109-125）：
  - `mock_get.return_value` 移除 `"jpeg_quality": "90",` 那行。
  - PUT json 改 `json={"analysis_interval_minutes": "30", "hls_retention_days": "7"}`。
  - 斷言改 `assert set(data["updated"]) == {"analysis_interval_minutes", "hls_retention_days"}`。
- `test_put_settings_no_pool_returns_503`（約 line 136-141）：PUT json 改 `json={"hls_retention_days": "90"}`（jpeg_quality 已非合法鍵；雖然 503 在鍵驗證前回傳，仍改成合法鍵避免誤導）。
- `test_put_settings_triggers_scheduler_reload`（約 line 144-160）與 `test_put_temp_toggle_in_allowed_keys`（約 line 163+）：`mock_get.return_value` 各移除 `"jpeg_quality": "85",` 那行（其餘不動；該 dict 是寫入後的完整設定快照，移除死鍵即可）。

3-1b. `tests/test_database.py` `test_user_settings_defaults_inserted`（約 line 43）：刪 `assert "jpeg_quality" in keys`，保留其餘三個 key 斷言。

3-1c. `tests/test_main.py` `test_settings_get_returns_defaults_when_no_pool`（約 line 67）：把 `assert "jpeg_quality" in data` 改為 `assert "hls_retention_days" in data`。

（`tests/test_db_writer.py` 的 `test_get_all_settings_returns_dict` / `test_upsert_settings_calls_executemany` 用 `"jpeg_quality"` 當泛用 key-value 範例字串測 db_writer 通用函式——這些函式對設定鍵不可知，不需更動。）

- [ ] **Step 2: 跑測試確認失敗**

Run: `uv run pytest tests/test_settings_router.py -v`
Expected: FAIL — `test_put_settings_valid_keys_returns_ok` 等因 jpeg_quality 仍在 ALLOWED_KEYS 而 `updated` 不符；GET 回退 dict 仍含 jpeg_quality。

- [ ] **Step 3: 移除後端 jpeg_quality**

3-3a. `config.py`：刪除這行（約 line 80）：

```python
    jpeg_quality: int = 70
```

（連同其上方 `# ── 影像 ──...` 註解區若只剩這行則一併刪該註解標題。）

3-3b. `routers/settings.py`：
- `ALLOWED_KEYS`（約 line 9-16）移除 `"jpeg_quality",` 一行。
- `get_settings` 的 pool-None 回退 dict（約 line 34-41）移除這行：

```python
            "jpeg_quality":              str(app_settings.jpeg_quality),
```

3-3c. `sql/init.sql`：seed INSERT（約 line 43-48）移除 `('jpeg_quality', '70', NOW()),` 一行，確保剩餘最後一列結尾正確（`('hls_retention_days', '90', NOW())` 後接 `ON CONFLICT`，無逗號）。結果：

```sql
INSERT INTO user_settings (key, value, updated_at) VALUES
    ('analysis_interval_minutes', '30', NOW()),
    ('anomaly_std_threshold', '3.0', NOW()),
    ('hls_retention_days', '90', NOW())
ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 4: 跑測試確認通過**

Run: `uv run pytest tests/test_settings_router.py tests/test_db_writer.py -v`
Expected: PASS（test_database.py 需實 DB，有環境時另跑應 PASS）

- [ ] **Step 5: Commit**

```bash
git add config.py routers/settings.py sql/init.sql tests/test_settings_router.py tests/test_database.py tests/test_main.py
git commit -m "refactor(settings): 移除無人消費的 jpeg_quality 死設定（後端+測試）"
```

---

### Task 4: 移除 jpeg_quality（前端）+ 全套件驗證

**Files:**
- Modify: `static/index.html`（UI 欄位 + load/save 參照）

- [ ] **Step 1: 移除前端 jpeg_quality**

4-1a. 移除設定面板欄位（約 line 819-820，整個 label+input；若外層有專屬包裹 div 也一併移除，但保留其他設定欄位）：

```html
          <label for="set-jpeg-quality">影像壓縮品質（50–95）</label>
          <input type="number" id="set-jpeg-quality" min="50" max="95" step="1">
```

4-1b. `loadSettings`（約 line 1373、1379）：移除取 `#set-jpeg-quality` 的那行（`const q = document.getElementById('set-jpeg-quality');`）與 `if (q && data.jpeg_quality !== undefined) q.value = data.jpeg_quality;` 那行。

4-1c. `saveSettings` PUT body（約 line 1391）：移除 `jpeg_quality: document.getElementById('set-jpeg-quality').value,` 一行。確認移除後物件 literal 逗號正確、無語法殘留。

- [ ] **Step 2: 驗證 JS 語法**

```bash
sed -n '/<script>/,/<\/script>/p' static/index.html > /tmp/_idx.js && node --check /tmp/_idx.js && echo JS_OK
```

Expected: `JS_OK`（若有多個 `<script>` 區塊，逐塊抽出檢查；本專案主邏輯在單一大 `<script>`）

- [ ] **Step 3: 確認無殘留 jpeg 參照**

```bash
grep -n "jpeg" static/index.html || echo "NO_JPEG_REFS"
```

Expected: `NO_JPEG_REFS`

- [ ] **Step 4: 全套件驗證**

```bash
uv run pytest --ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py -q
```

Expected: 既有通過數 + 本計畫新增 5 測試（Task1 的 4 + Task2 的 1）皆綠；維持 4 個既有 ZMQ_SOURCES 失敗（待辦 #12），**零本計畫新回歸**。

- [ ] **Step 5: Commit**

```bash
git add static/index.html
git commit -m "refactor(ui): 移除 jpeg_quality 設定欄位（死設定，純轉發架構無作用）"
```

---

## Self-Review（撰寫後對照 spec）

- **§3 retention 讀 DB 即時生效** → Task 1（純函式）+ Task 2（loop 接線、import、註解）。✅
- **§3.3 間隔 6h→1h** → Task 2 Step 3b/3d + 間隔測試。✅
- **§4 移除 jpeg_quality（config / settings router / init.sql / 前端）** → Task 3（後端三檔）+ Task 4（前端）。✅
- **§5 錯誤處理**（DB 失敗回退、壞值回退、殘留列無害）→ Task 1 函式 + Task 2 loop try/except；殘留列無 migration（spec §8 已定）。✅
- **§7 測試策略**（純函式 4 測試、間隔常數、既有測試更新、node --check、uv run pytest）→ Task 1/2/3/4 各步驟覆蓋。✅
- **Placeholder 掃描**：無 TBD/TODO，每個 code step 皆含實際程式碼。✅
- **型別/命名一致**：`effective_retention_days(db_settings, fallback_days)` 在 Task 1 定義、Task 2 以 `effective_retention_days(db_settings, app_settings.hls_retention_days)` 呼叫，簽名一致。✅
- **與 spec 的偏差**：純函式置於 `hls_retention.py`（非 spec 所寫的 main.py）並用顯式 `fallback_days` 參數——測試性精煉，行為相同，已於 Architecture 段標註。✅
