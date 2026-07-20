# 交接：只有 rpi5_dual 有 tracking 資料，其餘三支掛零（2026-07-20）

## 任務

查明 inference/tracker_pool 為什麼只為 `rpi5_dual` 產出 tracking 資料，其餘攝影機
（`rpi_sensors`、`cam_02`、`cam_03`）在 GPU 排程開啟的白天時段也沒有資料。
這直接影響 `analysis/scheduler.py` 的活動量計算與採血判斷正確性。

## 已確認的事實（唯讀查詢，2026-07-20 22:30–22:45）

`GET /tracking/{cam}?start=…&end=…`（部署服務 port 5005）逐支查詢，各取兩分鐘視窗：

| 攝影機 | 今日 14:00（兩分鐘） | 備註 |
|---|---|---|
| rpi5_dual | 4517 logs | 唯一正常 |
| rpi_sensors | 0 | |
| cam_02 | 0 | |
| cam_03 | 0 | 三天前同時段有 2880，近兩日 0 |

其他時段抽查：`rpi5_dual` 今日 10:00→4567、16:00→5138（穩定）；`cam_03` 今日
10:00/14:00/16:00 皆 0。

`GET /cameras` 回報四支皆存在，且 `active_types` 顯示都有在送幀：
```json
{"cameras":["rpi5_dual","rpi_sensors","cam_02","cam_03"],
 "active_types":{"rpi5_dual":["rgb","thermal"],"rpi_sensors":["rgb","thermal"],
                 "cam_02":["rgb"],"cam_03":["rgb"]}}
```
→ **ZMQ 收幀正常，問題在收幀之後的推論/追蹤/寫入環節**，不是攝影機斷線。

## 已排除的解釋

- **夜間全部歸零是預期行為**，不是故障：DB 設定 `gpu_off_schedule_enabled=true`
  （18:00–06:00 停 GPU）、`recording_schedule_enabled=true`（`recording_off_start=17:00`、
  `recording_off_end=06:30`）。17:12 的 `recording_paused` 系統告警符合排程。
  查白天時段才有意義。
- 不是前端問題：使用者原始症狀是「時間軸進入的回放沒有 bbox，從通知中心進去才有」，
  已查明三條 VOD 路徑共用同一套 `loadVod()` 機制，差別只在該 camera×時段
  `/tracking` 有無資料（通知本身由 tracking 算出 → 必然有資料）。

## 建議的下一步（尚未做）

1. `inference/pipeline.py` 的 `_process_batch`：確認 `cameras`/`snapshot` 每輪實際涵蓋哪幾支
   （`pipeline.py:130` `frames = [snapshot[c] for c in cameras]`、`:149` 的 zip 迴圈）。
   加暫時 log 或用既有 log 等級觀察是否只有 rpi5_dual 進到 batch。
2. `inference/tracker_pool.py:89-99`：`Created tracker for {camera_id}` 這行 log 對哪幾支出現過？
   （journalctl 查 service log；本次查 09:00–17:00 沒撈到 error/traceback 關鍵字，
   可能是 log 等級或輸出位置問題，需確認 log 實際去向——服務是 tmux 型
   `pig-agri-tmux.service`，日誌可能在 tmux pane 而非 journal。）
3. 檢查 batch 組成條件：是否有「只取第一支/只取有 thermal 的/取樣間隔」之類的邏輯
   讓其餘攝影機被跳過；以及 `batch_detector.py` 對 batch size 的假設。
4. 對照 DB：tracking 寫入端是否對某些 camera_id 失敗（唯讀查一下各 camera 最後一筆
   tracking 的時間戳，能界定「何時開始停」→ 對照 git log／設定變更時間）。

## 環境與安全注意

- 部署服務 `pig-agri-tmux.service` 跑在 **port 5005**，目前 active。
  **不要**另開 uvicorn 對同一批錄影目錄跑第二份 ffmpeg（寫入衝突）。
- **不要讀 `.env`**（權限會拒絕）。需要設定值改用 `GET /settings` 或 `Settings(_env_file=None)`。
- 對真實 DB 唯讀優先；要動 systemd 或重啟推論前先跟使用者確認（24/7 錄影服務）。
- 測試基準：`uv run pytest -p no:cacheprovider` 應為 0 失敗（目前 279 passed）。

## 根因（已確認，2026-07-21）

**不是「只有 rpi5_dual 曾有資料」，而是三支遠端攝影機的 ZMQ 送幀停滯時，推論迴圈
把最後一張凍結畫面反覆重寫進 DB，把該時段汙染成一坨重複列、之後看似「掛零」。**

### DB 證據（唯讀）

- 各 camera 每小時分桶：cam_02/cam_03/rpi_sensors 每天早上都有資料，但約 10–11 時
  三支同時「停」（只剩 rpi5_dual 穩定 ~150k/hr），下午某時又回來。停止前一小時出現
  4–10 倍暴量（07-20 10 時 rpi_sensors 1,193,081 vs rpi5_dual 142,294）。
- 拆到單幀：07-20 10 時 rpi_sensors 的暴量幾乎全來自**單一 frame_id**：
  `frame_id=1111387`（擷取於 10:56:45）在 DB 有 **1,127,666 列**、只有 34 個 object_id，
  即同一幀每個 object 被寫約 33,000 次。`distinct_ts=1`、`distinct_frame=1`。
- 16:00 後第一個 frame_id 是 **34938**（計數器歸零）→ 遠端 Pi 送幀端 10:56 後**重啟**，
  期間停送約 5.5 小時。

### 機制

`inference/pipeline.py:116-123` 的 `_loop` 每 100ms 抓 `snapshot = dict(self._latest)`
無條件重跑 detect→track→write，**沒有以 frame_id/ts 去重**。`_latest[cam]` 只被
`update_frame` 覆寫、從不清除。當某支 ZMQ 輸入停滯（`rpi_tailscale*` 是遠端 Pi 走
Tailscale，網路延遲讓幀變 stale 被 `zmq_receiver.py:76` 丟棄 → `update_frame` 停止呼叫），
最後一幀就永遠卡在 `_latest`，10Hz loop 對它狂灌 DB（全部帶凍結的 capture_ts）。

- rpi5_dual 不受影響，因為它的送幀端（本機/穩定）不會停滯。
- 「三支同時停」是因為三支都是遠端 Pi，網路/送幀端狀況相近。
- HLS `active_types` 仍顯示 rgb「在送幀」，因 HLS writer 有自己的 freshness 處理、
  且串流事後有恢復（夜間 00:52 四支 recv 皆在增加）；這條路徑與推論輸入是**兩套**。

### 兩個後果 + 待辦

1. **既有 DB 汙染（比想像嚴重，且 rpi5_dual 也中招）**：`tracking_logs` 共 121,425,508 列，
   去重後只剩 **45,839,999 列 → 75.6M 列（全表 62%）是凍結重灌 dupes**。
   **rpi5_dual 並非「唯一正常」**——它歷史上也停滯過且灌得最兇（單一 frame_id 22697634
   有 15,157,190 列、全同一 timestamp）；只是 07-18~20 白天剛好送幀穩定才顯得正常。
   `analysis/scheduler.py` 讀這張表，凍結幀位移為 0 → 活動量算成「不動」→ 可能誤標採血。
   - **去重鍵必須含 timestamp**：`(camera_id, frame_id, object_id, timestamp)`。
     不可只用 `(camera_id, frame_id, object_id)`——frame_id 是 per-camera 計數器、會隨送幀端
     重啟而回繞重用（rpi_sensors 曾 1111387→34938），不同真實幀會共用 frame_id；真正的
     dupe 特徵是**連 timestamp 都完全相同**。
   - **規模太大不宜純 DELETE**（刪 75M 列 → WAL 爆量、鎖表、dead-tuple bloat、需 VACUUM FULL）。
     建議 **CTAS + 換表**：`CREATE TABLE tracking_logs_dedup AS SELECT DISTINCT ON (key) *`
     → 建索引（`idx_tracking (camera_id, timestamp DESC)` + PK）→ 交易內 RENAME 換表。
     **在夜間 GPU-off 窗（18:00–06:00，無推論寫入）執行**，避免換表期間漏寫。
   - 仍是**對正式資料的破壞性操作，需明確同意**再執行。
2. **修正需重啟推論**（tmux 服務非 `--reload`，改 `pipeline.py` 不會熱生效）→ 依環境限制，
   先備好 diff、給使用者看、經同意再重啟。

### 修正實作（已完成，未部署，2026-07-21）

`inference/pipeline.py`：`_process_batch` 加 per-camera `_last_processed_fid` 閘門。frame_id
未前進的 camera 視為停滯，改餵**空偵測**給 tracker（讓殘留 track 正常 age out），不重跑
detector／不寫 DB／不重播 WS。設計取捨：若整支排除出 batch，tracker 不會 age → 復原時
殘留 stale track；改餵空 dets 可正常 age out（採此案）。frame_id **跨 camera 會重複**，
故一律以 camera_id 分別比對，**不可全域去重**。新增測試
`test_process_batch_skips_frozen_frame_reprocess`、`test_process_batch_ages_out_stale_camera_tracker`。
全套件 281 passed（原 279）。**尚未重啟推論服務**（等使用者同意）。

## 相關背景

`CLAUDE.md` 的「核心任務」與「MOT 追蹤 / ReID」章節；
`analysis/scheduler.py` 的活動量計算依賴 tracking 的 `object_id` 連續性。
