# 儲存韌性：故障防護 + 健康監控 + 夜間 Ephemeral Live + 編碼旋鈕

日期：2026-06-13
狀態：設計已確認，待寫 implementation plan

## 0. 背景與動機

兩次硬碟故障引發此設計：

1. **前一台主機（NVMe 系統碟）**：跑約 2 個月後系統碟毀損、無法開機，只能唯讀掛載救資料。
   `nvme smart-log`：`critical_warning=0x9`（spare 低於門檻 + 已鎖唯讀）、但 `media_errors=0`、
   `percentage_used=11%`。
2. **目前主機（USB 外接錄影碟 sdd, `/media/lazoark/pig_data`）**：跑 12hr 後 `usb 2-2: USB disconnect`
   → I/O error → ext4 自保 remount read-only → app `PermissionError: [Errno 13]`，**靜默失敗數小時**
   才被發現。

### 0.1 根因判定（有證據，非軟體寫壞硬碟）

**正常寫檔不會物理性損壞健康硬碟。** 兩次都是硬體／環境故障：

- **USB 碟**：先 `USB disconnect`（VLI / VIA Labs USB-SATA 橋接晶片，`idVendor=2109`）**才**有 I/O error
  → ext4 remount read-only。因果順序明確：實體斷線在前。常見於 24/7 持續寫入下供電不足／線材鬆動／
  橋接晶片過熱。軟體無法造成 USB 實體斷線。`PermissionError` 是唯讀掛載的**症狀非原因**。
- **NVMe**：`media_errors=0` + `percentage_used=11%` 與「寫太多磨壞」自相矛盾（真磨壞 media_errors 不會是 0、
  耐久也不會只用 11%）。比較像控制器/韌體缺陷或異常斷電弄壞 FTL → 控制器自鎖唯讀。fsck 的 orphan inode /
  journal aborted 是斷電/控制器失效的災後現場。

**結論：開發的軟體沒有把硬碟寫壞。** 真正的軟體缺口是「**硬碟出事時 pipeline 無任何防護 → 靜默失敗數小時**」。
本設計補的就是這個缺口，外加可選的寫入量旋鈕。

### 0.2 關鍵架構事實（決定設計乾淨度）

- **Postgres 在系統碟**（docker named volume `pg_data` → `/var/lib/docker/volumes/`），**不在**錄影碟。
  → 錄影碟掛掉時，**核心採血任務（推論 → `tracking_logs` → scheduler 活動量分析 → 告警）完全不受影響**，
  只有 HLS 即時串流 + VOD 回放儲存會死。
- **live 串流本身就是寫磁碟**：`zmq_receiver._on_frame` → `hls_manager.feed` → ffmpeg → `.ts` 寫進
  `hls_base_dir`；前端 hls.js 播這些落地片段。**沒有「不落地卻能看 live」的現成路徑**。這逼出夜間
  ephemeral live 的設計（見 §4）。
- **告警分流**：`/alerts/active`（live 紅框來源）讀 scheduler 的 `get_anomaly_cache()`，**不是**
  `health_alerts` 表。儲存告警走 `write_health_alert` → `health_alerts` → 通知中心，**不會在豬身上亂亮紅框**。

## 1. 範圍

做（使用者確認）：
1. 儲存故障的軟體防護（偵測 + 優雅降級 + 自動恢復）。
2. 儲存健康主動監控 + 告警（寫入探針 + 空間/inode 門檻 + ffmpeg 失敗信號；**無需 root**）。
3. 夜間排程 no-record + ephemeral live（預設 17:00–06:30 不錄影、但 live 照常；前端可調）。
4. 統一故障降級：錄影碟掛掉自動轉 ephemeral live（live 不斷 + 告警 + 自動續錄）。
5. 寫入磨耗優化：可選編碼旋鈕（crf/codec），預設零行為改變。

不做（YAGNI / 使用者排除）：
- dmesg / SMART 硬體層解析（需 root、脆、不可移植）。
- 動作觸發錄影（重大行為改變）。
- glob/flush 等微優化（對磨耗是雜訊，動了沒用）。
- 硬體 / 掛載參數 / 部署建議（使用者排除）。
- 多碟 failover / 把長期錄影改寫到備援碟。

## 2. 新模組 `storage_monitor.py`（架構 A：獨立模組）

對齊 `hls_retention.py`（純函式）+ scheduler（遲滯狀態機）風格。

### 2.1 純函式（table-driven 好測，零 I/O mock）

```
check_free_space(path) -> (free_bytes, free_ratio, free_inodes_ratio)
    os.statvfs；路徑不存在 → 拋例外（呼叫端視為 down）。

classify_health(probe_ok, marker_ok, free_bytes, free_inodes_ratio, thresholds)
    -> "ok" | "degraded" | "down"
    - not probe_ok or not marker_ok           → "down"（不可寫 / 碟沒掛上）
    - free_bytes < min_free_bytes
        or free_inodes_ratio < min_inodes     → "degraded"（仍可寫，空間吃緊）
    - else                                    → "ok"

next_state(current, reading, count, debounce) -> (new_state, new_count)
    遲滯：需連續 debounce 次同一 reading 才翻轉，否則維持 current（去抖、避免狂洗告警）。

is_recording_time(now_local, off_start, off_end) -> bool
    now 是否落在「錄影時段」（off window 之外）。處理跨午夜：
    off 17:00→06:30 ⇒ recording ON 僅 06:30–17:00。off_start==off_end → 永遠錄。
```

### 2.2 含 I/O 的部分

```
write_probe(base_dir) -> bool
    在 base_dir 寫極小探針檔（含 ts）→ fsync → 刪除；抓 OSError/PermissionError → False。
    一次抓到「唯讀 remount」「掛載消失」「權限不足」。

掛載點誤判防護（重要 gotcha）：
    USB 碟 unmount 後 /media/lazoark/pig_data 可能變回 root fs 上的空目錄、仍可寫
    → 影片會被偷偷寫進系統碟。對策：可選 storage_volume_marker（真實磁碟上放標記檔），
    probe 能寫但 marker 不見 → marker_ok=False → "down"。預設空字串＝不檢查（不破壞既有部署）。
```

### 2.3 單一健康/模式狀態（模組級，被 hls/UI/告警共用）

```
StorageHealth: state("ok"|"degraded"|"down"), free_gb, free_ratio, free_inodes_ratio,
               writable(bool), last_transition_ts, dropped_frames, ephemeral_active(bool),
               revive_count(借 hls 觀測)

target_mode() -> "record" | "ephemeral" | "drop"   ← feed/writer 讀這個（cheap、cached enum）
    錄影碟 writable 且 is_recording_time            → "record"
    (not is_recording_time) or (錄影碟 not writable) → "ephemeral"（前提 ephemeral base 可寫）
    連 ephemeral base 也不可寫                       → "drop"

recording_disk_health() / ephemeral_disk_health()  ← 兩個 base 各自一份 health
```

## 3. 背景 loop（`main.py` lifespan，仿 `_retention_loop`）

```
_storage_monitor_loop()：每 storage_check_interval_seconds（預設 20s）一輪：
    1. pool = get_pool(); db_settings = get_all_settings(pool)（拿不到→用 app_settings 預設、不中斷）
    2. 對錄影碟 base + ephemeral base 各跑 write_probe + check_free_space + classify_health + next_state
    3. 算 is_recording_time(now, off_start, off_end)
    4. 更新 target_mode() 的 cached 結果
    5. 狀態轉換時 write_health_alert（見 §5）+ logger
```

設定每輪讀 DB → 前端改門檻/排程 ≤20s 生效、免重啟（對齊 retention loop 慣例）。

## 4. `hls_manager` 整合（侵入極小，PDT 內部零改動）

### 4.1 輸出目標選擇

每個 `HLSStream` 依 `storage_monitor.target_mode()` 決定 ffmpeg 輸出去哪：

| 模式 | 目錄 | ffmpeg flags |
|---|---|---|
| `record` | `hls_base_dir/<cam>/<type>/<YYYY-MM-DD-HH>/` | `-hls_list_size 0 -hls_flags append_list+program_date_time`（全留，現狀） |
| `ephemeral` | `hls_ephemeral_dir/<cam>/<type>/_live/`（固定名） | `-hls_list_size 8 -hls_flags delete_segments+append_list+program_date_time`（滾動、舊段自動刪） |
| `drop` | — | feed 直接 return 丟幀、writer 不 emit 不 revive |

- `feed()` 開頭計算 `desired = (mode, dir)`：
  - `drop` → 直接 return（丟幀，更新 dropped_frames，**不**更新 out_dir、**不**碰 ffmpeg）。
  - `(mode, dir)` 與當前不同 → `_restart(dir, mode)`（沿用既有 hour rollover 重啟機制，多帶 mode→flags）。
  - record 模式內小時變更仍照常 `_restart`；ephemeral 模式用固定 `_live` 目錄 → 夜間**不做小時 rollover**
    （滾動視窗 + monotonic writer + revive 已足夠穩定，減少重啟）。
- `_writer_tick` 開頭：`drop` 模式跳過 emit 與 revive（根除磁碟死掉時 `_restart_in_place` 不斷 spawn 失敗
  ffmpeg 的風暴）。`record`/`ephemeral` 照常。
- 自動恢復：磁碟回來 → `target_mode()` 變 → 下個 feed 觸發 `_restart` 切回 → **不需重建 stream 物件、不需重啟服務**。
- ephemeral 模式**不寫 `pdt.jsonl` sidecar**（夜間不需 VOD，省寫入）。

### 4.2 `_make_ffmpeg_cmd` / `_restart` 帶 mode

- `_make_ffmpeg_cmd(out_dir, start_number, *, rolling: bool, crf, codec)`：rolling=True → 用
  `-hls_list_size 8 -hls_flags delete_segments+...`；False → 現狀全留。crf/codec 見 §7。
- `_restart(new_dir, *, rolling)` / `_restart_in_place` 透傳 rolling。

### 4.3 live serving（`routers/stream.py` `serve_hls`）

改成：**若有 active stream 且其 `out_dir.name == date_hour`** → 從 stream 當前 `out_dir` 撈 `.ts`
（不管在 hls_base 還是 ephemeral base）；否則 fallback `hls_base_dir/...`（歷史回放）。順手修掉
「segment 一定在 hls_base」的隱含假設。

- `/live` 回傳 `out_dir.name`：record 時是小時字串、ephemeral 時是 `_live`。
- `corrected_m3u8` 的 `date_hour == out_dir.name` 判斷天然成立（含 `_live`）→ **PDT 修正照常、bbox 對齊不變**。
- 前端 `checkLiveHandoff()`（12s 輪詢 `/live`）在 17:00/06:30 偵測 URL 變（小時 ↔ `_live`）→ 自動續播。
  **使用者全程看得到 live**。
- VOD / timeline 只列 hls_base 的小時目錄 → ephemeral `_live` 不被列入 → 夜間無 VOD、無錄影留存（正確）。

## 5. 告警（走現成通知中心，不亂亮紅框）

狀態轉換時 `write_health_alert(camera_id='_system', object_id=0, metric=<below>, current_value=free_gb,
mean_value=門檻, std_value=0)`（sentinel；camera_id≤16、metric≤32）：

| 轉換 | metric | log |
|---|---|---|
| ok→degraded（空間低，仍寫） | `storage_low_space` | error |
| ok/degraded→down（不可寫） | `storage_unwritable` | error |
| 因 down 轉 ephemeral（錄影碟掛、live 續） | `recording_paused_disk` | error |
| 進排程 no-record（正常夜間） | （不發告警，屬正常）— 僅 logger.info | info |
| down→ok（恢復、自動續錄） | `storage_recovered` | info |

`health_alerts` 那筆是持久化/稽核紀錄。

## 6. 健康狀態呈現（前端）

- 新 `GET /storage/health`（加進現有 `routers/storage.py`）回傳 `StorageHealth` 全欄位 +
  `target_mode` + recording/ephemeral 兩 base 的 health。
- 前端 header 全域狀態小燈：綠 `ok`(record) / 琥珀 `degraded` 或 `ephemeral(no-record 排程)` /
  紅 `down` 或 `ephemeral(錄影碟掛)`。每 ~20s 輪詢 `/storage/health`。與 per-camera bell 區隔
  （儲存是系統級）。點擊顯示細節。

## 7. 編碼旋鈕（磨耗 bucket，預設零行為改變）

- `config.py` 加 `hls_crf: int = 23`、`hls_video_codec: str = "libx264"`（= 現值）。
- `_make_ffmpeg_cmd` 讀這兩值取代寫死的 `"-crf","23"`/`"libx264"`。
- **env-only**（非 `/settings`，避免誤觸畫質）。改值於下次 ffmpeg 重啟生效（最多一小時內 / 模式切換時）。
- 想降寫入量/SSD 磨耗 → 調高 crf（如 28）或換 codec → 檔案變小。誠實註記：實際磁碟寫入量幾乎全來自影像編碼，
  這是唯一真正的軟體槓桿；真正防「寫爆磁碟而死」的是 §2 的空間門檻監控。

## 8. 設定（DB-backed via `/settings`，前端可即時調）

`routers/settings.py` `ALLOWED_KEYS` 加：

| key | 預設 | 說明 |
|---|---|---|
| `storage_check_interval_seconds` | 20 | 監控 loop 間隔 |
| `storage_min_free_gb` | 10 | 低於 → degraded |
| `storage_min_free_inodes_ratio` | 0.02 | 低於 → degraded |
| `storage_debounce_count` | 2 | 連續幾次同向才翻轉狀態 |
| `storage_volume_marker` | `""` | 掛載防誤判標記檔名（空＝不檢查） |
| `recording_schedule_enabled` | `true` | 夜間 no-record 排程總開關 |
| `recording_off_start` | `"17:00"` | no-record 起（本地時間 HH:MM） |
| `recording_off_end` | `"06:30"` | no-record 迄（隔天，HH:MM） |

env-only（建構時）：`hls_ephemeral_dir`（預設 `/dev/shm/pig_live`；不可用 fallback
`data/pig_monitoring/hls_live`）、`hls_crf`、`hls_video_codec`。

前端設定面板加上述 DB-backed 欄位（含排程開關 + 兩個時間欄）。

**注意**：`recording_schedule_enabled` 預設 **true** + 17:00–06:30，是使用者明確要求的預設行為——
既有部署升級後夜間將自動停止錄影（live 仍在）。可在設定面板關閉回 24/7 錄影。

## 9. 測試

- 純函式 table-driven：`classify_health` 全狀態組合、`next_state` 去抖、`is_recording_time`
  跨午夜/邊界/停用。
- `check_free_space`/`write_probe` 對 tmpdir + 模擬失敗（指唯讀/不存在目錄、marker 缺失）。
- `target_mode()` 真值表（record/ephemeral/drop × 排程 × 兩碟健康）。
- `hls_manager` 守衛：`drop` 時 feed 丟幀、writer 不 revive；mode 變觸發 `_restart` 帶正確 flags；
  ephemeral 用固定 `_live` 目錄、record 用小時目錄。復用既有 `test_hls_manager` harness。
- `serve_hls` 從 active stream out_dir（含 ephemeral base）撈 segment；歷史 fallback hls_base。
- `_make_ffmpeg_cmd` 含 config crf/codec + rolling flags。
- `/storage/health` endpoint；`/settings` 新鍵接受。

## 10. 誠實的邊界 / 已知限制

- **救不了第一台 NVMe 系統碟死亡的情境**：系統碟死 → DB（在系統碟）和 app 本身都死，監控無從運作/告警。
  本設計能做的是：**及早偵測空間耗盡** + **錄影碟（USB 這種）失敗自動降級 ephemeral**——也就是這次真正遇到的場景。
- 系統碟死時 DB 不可用 → `write_health_alert` 失敗，只剩 `logger.error`。
- **ephemeral base 最好在另一顆碟/RAM**：若 ephemeral 與錄影碟同碟、或系統碟（含 `/dev/shm` 的 RAM 耗盡）
  同時出事 → `target_mode()` 落到 `drop`、live 也斷。`/dev/shm` 預設約半 RAM，滾動 8 段 × N stream 極小，
  正常不會吃爆。
- 排程邊界判斷有 ≤ `storage_check_interval_seconds`（20s）延遲，無妨。
- ephemeral 固定 `_live` 目錄夜間長跑 ffmpeg（不小時 rollover）：靠 monotonic writer 節拍 + `_restart_in_place`
  自癒維持穩定；若觀測到夜間 drift/不穩，再評估 ephemeral 也週期重啟（YAGNI 暫不做）。
- 時區往返僅在固定 offset（UTC+8）正確；DST 時區排程會偏（部署無此問題）。

## 11. 不變量（回歸防線）

- record 模式行為與現狀逐位元相容（crf/codec/flags 預設不變、小時 rollover 不變、PDT/VOD 不動）。
- 採血主任務（推論→DB→scheduler）全程不受儲存模式影響。
- 儲存告警永遠走 `health_alerts`，永不進 `get_anomaly_cache`（不亂亮紅框）。
