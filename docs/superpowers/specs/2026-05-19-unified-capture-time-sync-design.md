# 統一 live + VOD 擷取時間同步架構設計

> 狀態：設計確認、待寫實作計畫
> 日期：2026-05-19
> 取代：FID 幀身分對應（`2026-05-18-frameid-bbox-sync*`）+ `live_pdt_offset_seconds` 手動 offset + `pdt_offset` EMA

## 1. 問題

`hls_manager._writer_loop` 以 naive `sleep(1/TARGET_FPS)` 取幀餵 ffmpeg，
ffmpeg 再 `-vf fps={TARGET_FPS}` resample。結果：

```text
媒體時間 = 累積餵入幀數 ÷ TARGET_FPS
```

而 bbox 比對的是相機真實擷取牆鐘（`/tracking` log 的 `frame_data.ts`）。
writer 實際餵入速率對 TARGET_FPS 的任何持續性偏差（`sleep` 累積誤差 + pipe
背壓 + 攝影機速率 ≠ TARGET_FPS 的補/丟動態）使兩者以速率 r **線性發散**。

症狀（瀏覽器實測）：

- live：FPS10/20 輕微落後可接受，**FPS15 明顯落後**（FPS 依賴）。
- VOD：`vodStartTs + video.currentTime`（單一錨點、不重錨）配 `/tracking`
  真實 ts，跑久 bbox 明顯領先。
- 兩者同源於上式脫鉤。先前所有常數 offset / FID 幀身分對應都在這條斜線上
  修補，已連修 3 次達 systematic-debugging Phase 4.5「同一架構 3+ 修正」門檻。

## 2. 目標與約束

**成功標準**：bbox 不隨時間漂移、對齊殘差 ≤ ±1 秒、與追蹤狀態無關、
**與串流 FPS 無關**、live 與 VOD 皆然；能刪除 FID tag / 手動 offset /
PDT-EMA 整套修正債。

**約束**：

- 可變更 ffmpeg 擷入架構（根治，非再加修正層）。
- 只保證新架構上線後錄製的影片正確；舊 ffmpeg 設定錄下的歷史 m3u8
  **不追溯修正**（VOD 對舊錄影回退既有行為）。
- 不動 `analysis/scheduler.py`（採血判斷用 object_id + ts，與本同步無關）。
- 相機 `frame_id` 端到端資料流（`zmq_receiver` → `pipeline.update_frame` →
  WS payload / DB `tracking_logs` / VOD `pickClosestFrame` 同幀群聚）**保留**；
  僅刪除「HLS 時間同步用的 FID 機制」。

## 3. 架構原則

不再用「ffmpeg 媒體時鐘」做 bbox 時間對應。服務出去的時間軸由相機真實
`capture_ts` 構成，且**每段重新錨定**。兩根支柱：

1. **Writer = 真實牆鐘節拍器**：消除造成斜線的持續性餵入速率偏差。
2. **時間軸後端權威重寫、每段重錨並落磁碟**：誤差變成「每段獨立、有界
   （≤ 段長 × 段內速率誤差）、不跨段累積」的小鋸齒，而非無界斜線。

ffmpeg 降格為純 JPEG→H.264 編碼器/切片器；其 EXTINF/PDT 一律被覆寫，
媒體時鐘是否漂移**不再被任何時間對應使用**。live 用 `hls.playingDate`、
VOD 用同一套 PDT 機制 → 兩條路徑收斂到同一個真實時間來源。

**單一時鐘勝點**：授權 PDT 採用相機 `capture_ts`，與 bbox `/tracking` ts
同一時鐘 → NTP skew 完全抵消，根除 CLAUDE.md 記載「ffmpeg 寫伺服器牆鐘
vs 相機時鐘差 3–5s」最初根因。

## 4. 後端機制（`hls_manager.py`）

### 4.1 Writer 真實牆鐘節拍器

`_writer_loop` 由 naive `sleep(1/FPS)` 改 `time.monotonic()` 截止排程：

- `deadline += 1/TARGET_FPS`，睡到 `deadline`。
- 落後過多（`now - deadline > slip_resync_seconds`，預設 `2/TARGET_FPS`）→
  `deadline = now` 重同步（不爆衝補償，避免時間軸扭曲）。
- 每 tick 取 buffer **最新**幀餵出；無新幀 → 複製上一幀；超前 → 丟舊幀。
  取捨依真實排程而非 buffer 滿空。

`_frame_buffer` 元素由 `(jpeg, fid)` 改 `(jpeg, capture_ts)`。複製幀沿用
上一幀 `capture_ts`（無新內容 → 不推進時間，正確）。

### 4.2 每段首幀真實 capture_ts 精準錨定

`_make_ffmpeg_cmd`：**移除 `-vf fps={TARGET_FPS}` 與輸入 `-framerate`**
（輸入已被 writer 鎖成真實 CFR，resample 多餘且正是脫鉤源）。`-hls_time`、
`-g`、`-hls_flags`（保留 `program_date_time` 作為 fallback PDT 來源）不變。

`_emit_frame`：維護單調 `_emit_idx`（每成功寫入 +1）與環形
`_emit_log: deque[tuple[int, float]]`（`(emit_idx, capture_ts)`，
`maxlen = TARGET_FPS * 1800`）。

`_scan_new_segments`：偵測新 `seg_K.ts`（`K = int(re.match(r"seg_(\d+)\.ts$"))`）
時，該段首幀輸出索引 `expected = round(K * TARGET_FPS * _HLS_TIME)`；於
`_emit_log` 取 `emit_idx` 最近 `expected` 者的 `capture_ts`，存
`_seg_pdt[seg_name]`。**取代現行用「掃描當下最新 `_last_capture_ts`」
（被管線延遲 L 汙染、正是殘留領先源）。**

非單調保護：若解析出的段首幀 ts ≤ 前一段 `_seg_pdt` → clamp 為前段 + ε
（`1e-3`），並 `logger.warning`。

### 4.3 真實時間軸落磁碟（VOD 能讀）

每小時輸出目錄新增 append-only sidecar `pdt.jsonl`。`_scan_new_segments`
每解析出一段首幀真實時間就追加一行：

```json
{"seg": "seg_007.ts", "pdt": 1779165126.49}
```

- 不改寫 ffmpeg 自管的 `index.m3u8`（`append_list` 下改寫有 race / 格式
  脆弱風險，CLAUDE.md 已有教訓）。
- current hour 的 `serve_hls` 仍即時吐 `corrected_m3u8`（PDT 來源改為精準
  `_seg_pdt`）。
- 跨小時後 VOD 靠 sidecar 拿真實段時間。
- `_restart` 只清記憶體 `_seg_pdt` / `_emit_log` / `_emit_idx`，
  **不動已落磁碟的 sidecar**。
- 寫入容錯：僅 writer thread 追加；不強制 fsync（best-effort，遺失 = 回退）。

### 4.4 EXTINF 真實化與不連續

段 K 的 `#EXTINF = _seg_pdt[K+1] − _seg_pdt[K]`。

- 尾段（無 K+1）→ 暫用 nominal `_HLS_TIME`，下段解析即自我修正（有界暫態）。
- 若 `pdt[K+1] − pdt[K] > discontinuity_seconds`（預設 `2 * _HLS_TIME`）→
  視為不連續：該邊界寫 `#EXT-X-DISCONTINUITY`，該段用 nominal `_HLS_TIME`。
  hls.js 過 DISCONTINUITY 後 `playingDate` 重錨。涵蓋 hourly restart 空檔、
  相機停擺後恢復。

### 4.5 thermal / 無 capture_ts

thermal feed 無 `capture_ts`（`zmq_receiver` 不傳）→ 不寫 sidecar →
vod_generator 自動回退 `hour_unix + ΣEXTINF`、live 用 ffmpeg 原生 PDT
fallback。與現況一致，無回歸（thermal 不用於採血活動量，可接受降級）。

## 5. `vod_generator.py` + 前端統一

### 5.1 `vod_generator.py`

`_parse_hour_m3u8`：讀同目錄 `pdt.jsonl` 建 `{seg_name: real_pdt}`。

- 每段 `seg_start = pdt[seg]`（非 `hour_unix + ΣEXTINF`）。
- `#EXTINF = pdt[next] − pdt[this]`（尾段 fallback nominal；超過
  `discontinuity_seconds` 同 4.4 處理）。
- **該段在 sidecar 缺（舊錄影 / thermal / 尚未解析）→ 整段回退現行
  `hour_unix + ΣEXTINF` 邏輯**（forward-only：舊錄影行為不變、無回歸）。

`build_vod_m3u8`：改**逐段輸出 `#EXT-X-PROGRAM-DATE-TIME`**（目前只在
playlist 頭輸出一個），讓前端對 VOD 也做每段重錨。

### 5.2 前端（`static/index.html`）

- `scheduleTrackingFetch`：`ts = vodStartTs + video.currentTime` 改為
  **由 VOD 播放器 PDT 推得的牆鐘**（hls.js 對含 PDT 的 VOD 同樣提供
  `playingDate` / frag PDT），與 live 完全同一套機制 → 每段重錨、有界誤差、
  不累積。`vodStartTs` 僅保留給 UI 時間碼顯示。
- Fallback：PDT 不可用（舊錄影 / thermal / 冷啟動首段前）→ 回退舊
  `vodStartTs + video.currentTime`，不可 crash / NaN。
- live：`targetTs = hls.playingDate`（不再減任何 offset）。
- HUD：移除 `fid` / `pdtOffset` 行；保留 `src`（PDT / fallback）、
  `playingDate`、`chosen-target`、`now`。

## 6. 技術債刪除清單（「根治 vs 補丁」分界）

- `hls_manager.py`：刪 `_update_pdt_offset` / `get_pdt_offset` /
  `pdt_offset` EMA；`corrected_m3u8` 不再寫 `#EXT-X-PIG-FRAMEID`；
  刪 `_seg_first_fid` / `_fed_log` / `_fed_count`（改 `_emit_log` /
  `_emit_idx`，語意 = 真實 capture_ts 而非 frame_id）。`HLSStream.feed` /
  `HLSManager.feed` 移除 `frame_id` 參數。
- `routers/stream.py` `/live`：移除回傳 `pdt_offset` 欄位。
- `config.py` / `routers/settings.py`：刪 `live_pdt_offset_seconds`
  （含 `ALLOWED_KEYS`、`_RELOAD_KEYS`、`reload()` 相關）。
- `zmq_receiver.py`：`hls_manager.feed` 呼叫不再傳 `frame_id`
  （仍傳 `capture_ts`；`pipeline.update_frame` 的 `frame_id` 不動）。
- `static/index.html`：刪 `parseFragFid` / `fidBySn` / `liveFragFid` /
  `liveFragNextFid` 等 FID 配對、`#EXT-X-PIG-FRAMEID` 處理、
  `bboxHistory` 的 `fid` 欄、`drawBoxes` 的 FID-first 分支、
  `livePdtOffset` 抓取與相減；前端設定面板移除手動 offset 欄位。

**刪除範圍界線**：相機 `frame_id` 仍照常流向 `FrameData` / WS payload /
DB `tracking_logs` / VOD `pickClosestFrame`，**不動**；只刪 HLS 時間同步
那條使用。

## 7. 設定參數（`config.py`）

- `hls_slip_resync_seconds: float = 2.0 / TARGET_FPS`（writer 落後重同步門檻）
- `hls_discontinuity_seconds: float`（預設 `2 * _HLS_TIME = 8.0`）

兩者皆 `getattr` 有預設，缺也不會壞。`live_pdt_offset_seconds` 移除。

## 8. 測試策略

### 8.1 單元（CI，注入假時鐘 / 假 segment 出現，不需真 ffmpeg / 瀏覽器）

`tests/test_hls_manager.py`：

- writer 節拍器：快 / 慢輸入下每真實秒 ≈ TARGET_FPS；空 buffer 複製幀
  帶上一幀 `capture_ts`。
- `_emit_frame` 記 `_emit_log = (emit_idx, capture_ts)`、`_emit_idx` 單調。
- `_scan_new_segments` 由 `_emit_log` 取段首幀 ts（最近 `K*FPS*HLS_TIME`），
  **非** `_last_capture_ts`。
- sidecar `pdt.jsonl` 內容 `{seg,pdt}` 正確；`_restart` 清記憶體但
  **不刪 sidecar 檔**。
- 大 pdt gap → `#EXT-X-DISCONTINUITY` + nominal EXTINF。
- 非單調 capture_ts → clamp 前段 + ε。
- `corrected_m3u8` PDT 由新 `_seg_pdt`、**無** `#EXT-X-PIG-FRAMEID`。
- `_make_ffmpeg_cmd` **無** `-vf fps` / 輸入 `-framerate`。

`tests/test_vod_generator.py`（或既有 VOD 測試）：

- 讀 sidecar → `seg_start` = 真實 pdt、`#EXTINF` = pdt 差。
- 缺 sidecar → 回退 `hour_unix + ΣEXTINF`（舊錄影路徑不變）。
- 逐段 `#EXT-X-PROGRAM-DATE-TIME` 輸出；discontinuity 透傳。

`tests/test_stream_router.py`：`/live` 回傳**無** `pdt_offset`。
（既有 4 個 404 屬待辦 #12 ZMQ_SOURCES OS-env gap，非本次回歸。）

`tests/test_settings_router.py`：`ALLOWED_KEYS` / `reload()` **無**
`live_pdt_offset_seconds`。

`tests/test_inference_pipeline.py`：`frame_id` 仍流向 `FrameData` / WS /
DB（**不**移除）；僅確認 `hls_manager.feed` 不再收 `frame_id`。

### 8.2 整合 smoke（手動，如先前 FID 做法）

真 ffmpeg + 合成幀以例如 13fps 餵、`TARGET_FPS=20`，量授權 PDT 對已知
輸入時間軸的誤差，驗證 FPS 無關、不累積。

### 8.3 瀏覽器驗收（使用者執行）

1. live 跨數次 hourly `_restart`、數小時不漂（≤ ±1s）。
2. VOD 拖曳進度軌 bbox 貼齊豬隻、長 VOD 不累積偏移。
3. **FPS 10 / 15 / 20 全部不漂**（FPS 無關是硬指標，必含先前失敗的 FPS15）。
4. thermal / 舊錄影 / RGB↔Thermal↔VOD↔Live 來回切自動降級、無殘留 / crash。
5. HUD 簡化（無 fid / pdtOffset）。

## 9. Phase 4.5 誠實註記

這是**架構替換**，非 FID 家族內第 4 次修補：整套移除 FID / offset / EMA，
改成「單一時鐘真實 `capture_ts` 授權時間軸 + 每段重錨」全新機制（時間對應
不再經過會漂移的媒體時鐘）。驗收**必須**含先前失敗的 FPS15 sweep 以證實
架構主張；若新架構下 FPS15 仍漂，表示本設計的根因模型錯誤，須再退一步
（而非於本架構內補第 4 次）。

## 10. 已知不處理（YAGNI / 範圍外）

- 舊 ffmpeg 設定錄下的歷史 m3u8 不追溯修正（VOD 回退既有行為）。
- 冷啟動前 20–30s「全 bbox 不動」是前端 `bboxHistory` 尚空，非同步問題，
  本設計不處理。
- thermal 串流全程降級（無 capture_ts 來源）。
