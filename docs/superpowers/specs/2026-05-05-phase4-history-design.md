# Phase 4 — 歷史查詢 設計文件

## 範圍

Phase 4 在 Phase 3（即時 MOT 推論 + WebSocket）基礎上，增加：

1. 追蹤結果寫入 PostgreSQL `tracking_logs`
2. 前端時間軸（7 天視窗 + 週導航）
3. VOD 回放（動態生成含 `EXT-X-PROGRAM-DATE-TIME` 的 m3u8）
4. 歷史 MOT overlay（VOD 播放時以 `hls.playingDate` 查詢歷史 bbox）

---

## 關鍵設計決策

| 議題 | 決策 | 理由 |
|------|------|------|
| 時間軸資料來源 | 查詢 `tracking_logs` | 準確反映推論是否成功，不依賴 HLS 檔案存在 |
| VOD 時間同步 | `EXT-X-PROGRAM-DATE-TIME` + `hls.playingDate` | 無累積誤差，hls.js 原生支援 |
| DB 寫入策略 | 每幀即時寫入（`run_coroutine_threadsafe`） | 實作簡單，延遲低 |
| DB 邏輯位置 | 獨立 `db_writer.py` 模組 | 可獨立單元測試，pipeline 職責不膨脹 |

---

## 後端設計

### 新增：`db_writer.py`

```python
async def write_tracking_log(
    pool, *, camera_id, timestamp, frame_id, object_id,
    bb_left, bb_top, bb_width, bb_height, confidence, thermal_intensity
) -> None: ...

async def query_tracking_logs(
    pool, camera_id: str, start: float, end: float,
    object_id: int | None = None
) -> list[dict]: ...
# 每筆 dict 格式（與 WebSocket overlay 相容）：
# { "object_id": int, "bbox": [bb_left, bb_top, bb_width, bb_height],
#   "confidence": float, "timestamp": float, "frame_id": int }

async def query_timeline_hours(
    pool, camera_id: str, start_ts: float, end_ts: float
) -> list[int]: ...
# 回傳該時段內有 tracking_logs 記錄的整點 Unix timestamp list
# SQL: SELECT DISTINCT floor(timestamp/3600)*3600 AS hour ...
```

### 新增：`vod_generator.py`

函式簽名：
```python
def build_vod_m3u8(
    camera_id: str, stream_type: str, start_ts: float, end_ts: float
) -> str | None
```

演算法：

1. 根據 `start_ts`/`end_ts` 列舉所有涵蓋的小時目錄（`{HLS_BASE_DIR}/{camera_id}/{stream_type}/{YYYY-MM-DD-HH}/`）
2. 解析每個存在目錄內的 `index.m3u8`，提取 `(filename, duration_sec)` 序列
3. 計算各 segment 的真實開始時間：`seg_start = hour_unix + accumulated_duration`
4. 過濾：只保留 `seg_start >= start_ts` 且 `seg_start < end_ts` 的 segment
5. 在第一個 segment 前插入：`#EXT-X-PROGRAM-DATE-TIME: <ISO8601>`
6. Segment URL 使用絕對路徑：`/stream/hls/{camera_id}/{stream_type}/{YYYY-MM-DD-HH}/{filename}`
7. 尾端加 `#EXT-X-ENDLIST`
8. 過濾後無 segment → 回傳 `None`

輸出格式（`#EXT-X-TARGETDURATION` 從各小時 m3u8 的 `#EXT-X-TARGETDURATION` 取最大值，動態填入）：
```
#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:{max_segment_duration}
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-PROGRAM-DATE-TIME:2026-05-04T14:32:08Z
#EXTINF:4.0,
/stream/hls/rpi_sensors/rgb/2026-05-04-14/seg_002.ts
...
#EXT-X-ENDLIST
```

### 修改：`inference/pipeline.py`

在 `_process_batch` 的 tracker 結果處理迴圈中新增：

**Thermal intensity 計算**（在 bbox overlay 迴圈裡）：
- tracker 輸出為原始圖座標（640×480），thermal_np 為 160×120
- scale_x = 160/640 = 0.25，scale_y = 120/480 = 0.25
- 裁切 thermal_np 對應區域，取 `np.mean()`；若 `thermal_np is None` 則為 `None`

**DB 寫入**：
```python
asyncio.run_coroutine_threadsafe(
    write_tracking_log(pool, camera_id=cam, timestamp=frame_data.ts,
                       frame_id=frame_data.frame_id, object_id=obj_id,
                       bb_left=x1, bb_top=y1, bb_width=w, bb_height=h,
                       confidence=conf, thermal_intensity=ti),
    self._event_loop,
)
```

`pool` 透過 `database.get_pool()` 取得。

### 修改：`routers/tracking.py`

```python
@router.get("/tracking/{camera_id}")
async def get_tracking(camera_id, start, end, object_id=None):
    pool = database.get_pool()
    logs = await query_tracking_logs(pool, camera_id, start, end, object_id)
    return {"logs": logs}

@router.get("/tracking/{camera_id}/timeline")
async def get_timeline(camera_id, start_ts: float, end_ts: float):
    pool = database.get_pool()
    hours = await query_timeline_hours(pool, camera_id, start_ts, end_ts)
    return {"hours": hours}
```

### 修改：`routers/stream.py`

```python
@router.get("/{camera_id}/vod")
async def get_vod_stream(camera_id, start: float, end: float,
                         stream_type: str = Query("rgb", alias="type")):
    m3u8 = build_vod_m3u8(camera_id, stream_type, start, end)
    if m3u8 is None:
        raise HTTPException(status_code=404, detail="No segments found")
    return PlainTextResponse(m3u8, media_type="application/vnd.apple.mpegurl")
```

---

## 前端設計

### 時間軸元件

位置：`<video>` 下方，週導航列 + 時間軸 bar 共兩列。

**週導航列：**
```
[< 上一週]  2026年5月 第1週（04/27 – 05/03）  [下一週 >]
```
- 以週一為每週起點
- 下一週按鈕：當週時 disabled；上一週按鈕：超過 `hls_retention_days` 時 disabled
- 切換週時重新呼叫 `GET /tracking/{camera_id}/timeline?start_ts=&end_ts=`

**時間軸 bar：**
- 寬度代表 7 天 × 24 小時 = 168 格
- 有資料的小時：`--accent`（綠色）色塊；無資料：暗色
- 點擊任意時間點 → 進入 VOD 模式，載入該小時片段（`start=clicked_ts`, `end=clicked_ts+3600`）
- 右側固定「**Live**」按鈕，點擊切回即時串流

### 模式切換

```
isLive = true  → WebSocket bbox + live HLS（現有邏輯）
isLive = false → 歷史 bbox query + VOD HLS
```

切換時清空 `latestBoxes`，防止殘影。

### VOD 模式下的 HLS 播放

- VOD endpoint（`/stream/{camera_id}/vod?start=&end=&type=rgb`）直接回傳 m3u8 純文字（`PlainTextResponse`），與 live 的 JSON wrapper 不同
- 前端直接呼叫 `hls.loadSource('/stream/{camera_id}/vod?start=&end=&type=rgb')`，不需中間 fetch 步驟
- 狀態列顯示「回放中 YYYY-MM-DD HH:mm」

### 歷史 MOT Overlay

- 監聽 `video.timeupdate` 事件（約每 250ms 觸發）
- 讀取 `hls.playingDate`（Date 物件），換算為 Unix 秒：`const ts = hls.playingDate.getTime() / 1000`
- 加 300ms debounce，避免過頻 request
- 呼叫 `GET /tracking/{camera_id}?start={ts-0.5}&end={ts+0.5}`
- 將回傳 objects 更新至 `latestBoxes`，由現有 `drawBoxes()` rAF 迴圈渲染

---

## 資料流

```
ZMQ frame → InferencePipeline._process_batch
    ├─→ WebSocket broadcast（live bbox，現有）
    └─→ db_writer.write_tracking_log（新增）
            ↓
        tracking_logs（PostgreSQL）

前端 VOD 模式：
    video.timeupdate
        → hls.playingDate
        → GET /tracking/{camera_id}?start=&end=
        → drawBoxes()
```

---

## 測試策略

| 模組 | 測試方式 |
|------|---------|
| `db_writer.py` | 單元測試，mock asyncpg pool |
| `vod_generator.py` | 單元測試，臨時目錄建假 m3u8 |
| `routers/tracking.py` | TestClient + mock db_writer |
| `routers/stream.py` VOD | TestClient + mock vod_generator |
| `pipeline.py` thermal | 單元測試，mock detector/reid/tracker |
| 前端時間軸 | 手動驗證（無框架，不寫 JS 單元測試）|

---

## 不在 Phase 4 範圍內

- Phase 5 分析排程（3σ 異常偵測）
- Phase 6 設定頁面
- Thermal VOD（架構相同，留給後續）
- 通知中心（Phase 5）
