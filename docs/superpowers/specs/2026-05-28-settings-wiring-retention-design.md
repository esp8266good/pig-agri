# 設定接線修正 + retention 生效 + 移除 jpeg_quality 死設定 設計

> 狀態：設計已確認，待寫實作計畫。子系統 A（拆分自「設定優先序 + 回放儲存管理 + 前端體驗」三子系統，見決策記錄）。

## 1. 動機

兩個查證確認的缺陷：

1. **`hls_retention_days` 前端設定不生效**：`config.py` 的 `app_settings` 是 import
   當下從 `.env`/預設建一次的不可變物件。`main.py` 的 retention loop 直接讀
   `app_settings.hls_retention_days`（建構時值），DB 改了也讀不到 → 在前端把保留
   天數改短，週期性自動刪除完全沒效果，永遠用 `.env`/預設（90 天）判斷。
   （對照：scheduler 的 4 個鍵透過 `reload()` / `_apply_db_settings()` 主動讀 DB，
   故它們「前端優先」正確。）

2. **`jpeg_quality` 是死設定**：存進 DB、UI 也有「影像壓縮品質（50–95）」欄位，但
   全 codebase 無人消費。架構是純轉發——相機 publisher 送來 JPEG，`zmq_receiver`
   解碼一份給推論、另一份**原封不動**餵 HLS（`hls_manager.feed(label,"rgb",rgb_bytes)`），
   全程不重新編碼，故 `jpeg_quality` 無處作用。

附帶調整：retention 巡檢間隔 6h → 1h，縮短「改設定 → 真的刪除」的回饋延遲。

## 2. 範圍

- **僅後端**：`main.py`、`config.py`、`routers/settings.py`、`sql/init.sql`、
  `static/index.html`（移除 jpeg_quality UI 欄位與其 load/save 參照）。
- **不**動 retention 的核心刪除邏輯（`hls_retention.py` 已測、目錄命名對得上）。
- **不**新增 API 端點、**不**改 retention 的「先 sleep 再 purge」首輪延後設計。

## 3. 變更一：retention 讀 DB 即時生效

### 3.1 新增可測純函式（`main.py`）

```python
def _effective_retention_days(db_settings: dict[str, str] | None) -> float:
    """DB 有 hls_retention_days 且可解析 → 用 DB 值（單一權威）；
    否則回退 app_settings.hls_retention_days（.env/預設）。"""
    if db_settings is not None:
        raw = db_settings.get("hls_retention_days")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return float(app_settings.hls_retention_days)
```

### 3.2 retention loop 每輪讀 DB

`_retention_loop` 每輪開頭（sleep 之後、purge 之前）：

```python
pool = database.get_pool()
db_settings = None
if pool is not None:
    try:
        db_settings = await get_all_settings(pool)
    except Exception as e:
        logger.warning(f"HLS retention 讀取 DB 設定失敗，回退 app_settings：{e}")
days = _effective_retention_days(db_settings)
purge_expired_hls(app_settings.hls_base_dir, days)
```

- DB 不可用（pool 為 None）或讀取失敗 → `db_settings=None` → 回退 app_settings。
- 整段仍包在既有 `try/except Exception`（巡檢失敗不拖垮服務）。
- `hls_base_dir` 維持讀 `app_settings`（非使用者可調鍵，無 DB 來源）。

### 3.3 巡檢間隔

`_RETENTION_INTERVAL_SECONDS`：`6 * 3600` → `1 * 3600`。維持「先 `await asyncio.sleep`
再 purge」（首次延後一個間隔＝1h，沿用避免啟動 I/O + 避免 TestClient 短命 lifespan
啟動即刪掉 timeline 測試 fixture 的既有理由）。

## 4. 變更二：移除 jpeg_quality 死設定

逐處移除（保持「UI 看得到的設定都真的生效」）：

1. `config.py`：刪 `jpeg_quality: int = 70`。
2. `routers/settings.py`：`ALLOWED_KEYS` 移除 `"jpeg_quality"`；GET 的 pool-None
   回退 dict 移除 `"jpeg_quality"` 那行。
3. `sql/init.sql`：移除 seed INSERT 裡的 `('jpeg_quality', '70', NOW())` 列。
   （既有 DB 已有的 `jpeg_quality` 列無害留著——`get_all_settings` 仍會回傳，但前端
   無欄位讀取、`ALLOWED_KEYS` 拒絕寫入；不需 migration 刪列。spec 標註此既存列為
   無害殘留。）
4. `static/index.html`：移除 `#set-jpeg-quality` 的 `<label>`+`<input>`（約 819–820）、
   `loadSettings` 裡讀 `data.jpeg_quality` 的那行（約 1373/1379）、`saveSettings`
   PUT body 裡的 `jpeg_quality:` 欄位（約 1391）。

## 5. 錯誤處理

- DB 讀取失敗：log warning、回退 app_settings、迴圈續跑（沿用既有 except）。
- 值無法解析（非數字）：`_effective_retention_days` 回退 app_settings。
- 移除 jpeg_quality 後，舊 DB 殘留列不影響任何路徑（無人讀取、寫入被擋）。

## 6. 與子系統 B 的介面

B（回放儲存管理）之後會讓 `purge_expired_hls` 接受「受保護時段」排除集合，使「保留 /
書籤」標記的時段不被自動刪除。A 先不碰 `purge_expired_hls` 簽名，保持最小；B 再擴充。

## 7. 測試策略

- **`_effective_retention_days` 純函式單元測試**（新檔或加進 `tests/test_hls_retention.py`）：
  1. DB 有合法值 → 回 DB 值（勝過 app_settings）。
  2. DB 缺 `hls_retention_days` 鍵 → 回退 app_settings 值。
  3. DB 值為非數字字串 → 回退 app_settings 值。
  4. `db_settings=None`（pool 不可用）→ 回退 app_settings 值。
- **間隔常數**：斷言 `_RETENTION_INTERVAL_SECONDS == 3600`。
- `purge_expired_hls` / `find_expired_hour_dirs` 既有 4 測試不動。
- `routers/settings.py` 既有測試：更新對 `ALLOWED_KEYS` / GET 回退 dict 的斷言（移除
  jpeg_quality 期望）。
- 前端：`node --check`（抽出 `<script>`）確認移除 jpeg_quality 參照後 JS 語法正確。
- 全套件：以 `uv run pytest` 跑，維持既有綠燈（基線 4 既有 ZMQ_SOURCES 失敗無關）。

## 8. 非目標（YAGNI）

- 不實作 JPEG 重新編碼（純轉發架構下二次壓縮只降質耗 CPU，無值）。
- 不為移除 jpeg_quality 寫 DB migration 刪舊列（殘留無害）。
- 不改 retention 的目錄掃描/刪除邏輯與首輪延後設計。
- 不加「立即執行清理」手動端點（B 的刪除 UI 即為即時手段）。
- 不動 scheduler 既有的 4 鍵 reload 路徑（已正確）。
