# Live bbox 與畫面同步 — frame_id 幀身分對應 設計

> 日期：2026-05-18 ／ 狀態：已與使用者確認設計，待審 spec

## 問題陳述

Live 模式 bbox 與串流畫面對不上，且**隨時間越來越偏**。過去 3 次修補
（自動 EMA `pdt_offset` → 後端自管 PDT → 手動 `live_pdt_offset_seconds`）
都建立在「用時鐘相減推算對應幀」的前提上，已被證明為錯誤架構
（systematic-debugging Phase 4.5）。

**根因（結構性，非調參）**：`hls_manager.py` 的 `_writer_loop` 以
`1/TARGET_FPS` 取幀、`-vf fps={TARGET_FPS}` 強制定 FPS，使 ffmpeg 媒體時間軸 =
輸出幀數 ÷ TARGET_FPS，與真實擷取牆鐘脫鉤。攝影機實際發布速率不等於
`TARGET_FPS`（永遠不會剛好）→ 媒體時鐘相對真實時間以速率 `r` 持續漂移。
`hls.playingDate` 跑在這條媒體時鐘上。誤差 = `L`（固定管線延遲）+ `r·Δt`
（隨時間線性增長）。**任何常數（手動或 EMA 自動）只能抵銷 L，不可能抵銷
`r·Δt` 這條斜線**；ffmpeg 每小時 restart 重錨 → 觀察到逐小時鋸齒累積。

**關鍵線索**：`frame_id` 其實全程存在——`zmq_receiver.py` 封包頭 `"dQII"`
帶 `ts, frame_id`，inference WS payload（`inference/pipeline.py:182`）
**已包含 `frame_id`**，VOD 的 `/tracking` log 也有 `frame_id`（VOD 因此可用）。
唯一缺口：前端 live 路徑在 `index.html:1379` 只存 `ts`、丟棄 `frame_id`，
且 HLS segment 沒有對應的 frame_id 標註。

## 目標

讓 live bbox 與畫面**自動**同步，**消除手動/常數 offset**，**天生 per-stream**，
且**誤差不隨時間累積**。

## 核心原理

不再用時鐘相減。每個 HLS segment 標註其首幀對應的擷取 `frame_id`；前端依
播放位置算出當前 `frame_id`，用它在 `bboxHistory` 精準配對。`frame_id` 單調
遞增、與任何時鐘無關 → 無漂移、無 offset、每段重錨故誤差不累積、per-stream。

## 架構與改動單元

### 單元 1：`hls_manager.py` — 後端記錄 segment ↔ frame_id

`HLSStream` 職責：在既有「偵測新 segment 檔」機制旁，平行維護
「segment 首幀 frame_id」對應，並在 corrected m3u8 寫出自訂標籤。

- **`feed()` 新增 `frame_id: Optional[int]` 參數**（比照現有 `capture_ts`）。
  維護單調遞增的餵入幀記錄：`self._fed_log: collections.deque[tuple[int, int]]`
  存 `(fed_index, frame_id)`，`self._fed_count: int` 為已餵入幀數。
  每次 `feed()`：`self._fed_count += 1`；若 `frame_id is not None` 則
  `self._fed_log.append((self._fed_count - 1, frame_id))`。deque `maxlen` 取
  `TARGET_FPS * 60 * 30`（約 30 分鐘餘量，遠超單一小時 segment 數需求即可，
  實作時以常數 `_FED_LOG_MAX` 表示，預設 `TARGET_FPS * 1800`）。

- **`self._seg_first_fid: dict[str, int]`**：segment 檔名 → 該段首幀 frame_id。
  在 `_scan_new_segments()` 內，對每個**新**出現的 `seg_NNN.ts`：
  - 解析序號：`m = re.match(r"seg_(\d+)\.ts$", name)`，`ordinal = int(m.group(1))`。
  - 期望首幀餵入索引：`expected = round(ordinal * TARGET_FPS * _HLS_TIME)`
    （`_HLS_TIME = 4`，與 `-hls_time` 一致，提為模組常數供雙方引用）。
  - 從 `self._fed_log` 取 `fed_index` 最接近 `expected` 的項，取其 `frame_id`
    存入 `self._seg_first_fid[name]`。若 `_fed_log` 為空或無對應 → 不寫入
    （該段於 m3u8 即不帶標籤，前端對該段降級）。
  - 上限保險同 `_seg_pdt`：`len > 2000` 時刪最舊。
  - **理由**：用「幀計數」推算（與餵入當下牆鐘無關）才能避開管線延遲 L；
    「掃描當下最新幀」會被 L 汙染（舊 `_seg_pdt` 即此弱點）。

- **`corrected_m3u8()`**：在既有逐行改寫迴圈中，對 segment URI 行，若
  `self._seg_first_fid` 有該檔名，於 URI 行**之前**插入一行
  `#EXT-X-PIG-FRAMEID:<frame_id>`。未知 → 不插入（前端對該段降級）。
  與既有 `#EXT-X-PROGRAM-DATE-TIME` 改寫互不影響、順序：PDT 行（沿用）
  → `#EXT-X-PIG-FRAMEID` 行 → segment URI 行。

- **`_restart()`**：清空 `_seg_first_fid`、`_fed_log`、`_fed_count`
  （比照既有 `_seg_pdt.clear()`）。

- **`HLSManager.feed(...)`**：新增 `frame_id` 參數透傳給 `stream.feed`
  （比照現有 `capture_ts`，僅 `stream_type == "rgb"` 時有意義；thermal
  傳 None 即全程降級，可接受）。

### 單元 2：`zmq_receiver.py` — 透傳 frame_id

呼叫 `hls_mod.hls_manager.feed(label, "rgb", rgb_bytes, capture_ts=ts)`
（約 152 行）改為additionally 帶 `frame_id=frame_id`（該處本就有 `frame_id`
區域變數，見 83 行）。不改其他行為。

### 單元 3：`static/index.html` — 前端依 frame_id 配對

- **`bboxHistory` 存 frame_id**：`index.html:1379` 由
  `bboxHistory.push({ ts: data.timestamp, boxes: latestBoxes })` 改為
  `bboxHistory.push({ ts: data.timestamp, fid: data.frame_id, boxes: latestBoxes })`
  （`data.frame_id` WS 已提供；無則 `fid: undefined`）。

- **記錄當前 segment 的 frame_id 錨點**：新增模組變數
  `let liveFragFid = null, liveFragNextFid = null, liveFragStart = 0, liveFragDur = 0;`
  在 `loadStream`/HLS 初始化處綁定 `hls.on(Hls.Events.FRAG_CHANGED, (e, data) => {...})`：
  - 從 `data.frag.tagList` 找 `#EXT-X-PIG-FRAMEID`（hls.js 將未知標籤放入
    `frag.tagList`，形如 `["EXT-X-PIG-FRAMEID", "<value>"]` 或自訂；實作時
    以 `frag.tagList.find(t => String(t[0]).includes('PIG-FRAMEID'))` 取值並
    `parseInt`）。
  - `liveFragFid = 解析值 或 null`；`liveFragStart = data.frag.start`；
    `liveFragDur = data.frag.duration`。
  - 下一段基準：嘗試從 `hls` 已載入 fragments 找下一段的 PIG-FRAMEID 作
    `liveFragNextFid`；取不到則 `null`（段內改用每秒幀數估：
    `TARGET_FPS_HINT` 常數，前端定 `const FPS_HINT = 25;` 僅供 fallback 插值，
    註明非精確）。

- **live `drawBoxes` 改為 frame_id 配對**（`index.html` 約 1431 起的
  `if (isLive && bboxHistory.length)` 區塊）：
  - 若 `liveFragFid != null` 且 `bboxHistory` 多數項有 `fid`：
    - `const frac = liveFragDur > 0 ? Math.min(1, Math.max(0, (video.currentTime - liveFragStart) / liveFragDur)) : 0;`
    - `const span = (liveFragNextFid != null) ? (liveFragNextFid - liveFragFid) : (liveFragDur * FPS_HINT);`
    - `const targetFid = liveFragFid + frac * span;`
    - 在 `bboxHistory` 取 `Math.abs(entry.fid - targetFid)` 最小者，
      `displayBoxes = best.boxes`；HUD `src='FID'`。
  - 否則（無標籤/thermal/舊錄影/`fid` 不可用）→ **完全沿用現有**
    `playingDate` − `livePdtOffset` 最近-ts 邏輯（不刪、不改），HUD `src` 維持
    `PDT`/`latency`。
  - HUD（`__bboxDebug`）`_dbg` 增欄位：`fid`（targetFid）、`chosenFid`、
    `fragFid`/`fragNextFid`，供瀏覽器實測判讀。

- **`livePdtOffset` 與 `/live` `pdt_offset`**：保留現狀（fallback 用），
  本 spec 不改 `routers/stream.py` / `config.py` / `routers/settings.py`。
  待長時間實測確認 FID 路徑穩定後，另案評估是否移除常數機制（不在本 scope）。

## 資料流

```
camera → zmq (ts, frame_id) ─┬─→ inference → WS payload {frame_id, timestamp, objects}
                              │                         → 前端 bboxHistory[{ts,fid,boxes}]
                              └─→ hls_manager.feed(rgb, capture_ts, frame_id)
                                     ├─ _fed_log[(fed_index, frame_id)]
                                     └─ _scan_new_segments: seg_NNN → _seg_first_fid
                                            ↓
                                  corrected_m3u8: #EXT-X-PIG-FRAMEID:<id> 寫在 segment 前
                                            ↓
                          前端 FRAG_CHANGED 讀 frag.tagList → liveFragFid
                                            ↓
              drawBoxes: targetFid = liveFragFid + frac×span → 取 bboxHistory 最近 fid
```

## 錯誤處理與降級

| 情境 | 行為 |
|---|---|
| segment 剛出現、`_fed_log` 尚無對應 | 不寫 `_seg_first_fid` → m3u8 無標籤 → 前端該段降級 PDT 路徑 |
| thermal 串流（feed 無 frame_id） | 全程無標籤 → 前端降級 PDT/latency 路徑 |
| 舊錄影 / 非當前小時（`corrected_m3u8` 回 None） | router 落回磁碟檔（無標籤）→ 前端降級；VOD 路徑本就不受影響 |
| `bboxHistory` 項缺 `fid`（WS 無 frame_id） | 該情境 fallback 至 PDT 路徑（與上同分支）|
| `frag.tagList` 無 PIG-FRAMEID | `liveFragFid=null` → 降級 |

降級一律回退到**現有**行為，保證不比現況更差。

## 已知限制（誠實揭露）

幀計數錨點 `expected = round(ordinal × TARGET_FPS × _HLS_TIME)` 假設餵入↔
ffmpeg 輸出近 1:1。攝影機速率長期顯著偏離 `TARGET_FPS` 致 ffmpeg 大量補/丟幀
時，錨點會有**數幀級、有界、且每段自我修正**的殘差——但這把舊方案那條
「無界累積的斜線」變成「有界、不累積的小常數」，即為使用者回報問題的根除。
若實測殘差仍可感，後續可另案以 ffmpeg progress/stats 取真實輸出幀數精修錨點
（不在本 scope，YAGNI）。

## 測試策略

- `tests/test_hls_manager.py`（既有，目前 20 passed）新增：
  1. `feed(..., frame_id=N)` 後 `_fed_log` 末項為 `(fed_count-1, N)`、
     `_fed_count` 遞增。
  2. 模擬 `_fed_log` 後呼叫 `_scan_new_segments`（mock `out_dir.glob`
     回 `seg_2.ts`），斷言 `_seg_first_fid["seg_2.ts"]` == 餵入索引最接近
     `round(2*TARGET_FPS*4)` 的 frame_id。
  3. `corrected_m3u8` 對有 `_seg_first_fid` 的段，輸出含
     `#EXT-X-PIG-FRAMEID:<id>` 且位在該段 URI 行之前；未知段不含該標籤。
  4. `_restart()` 後 `_seg_first_fid`/`_fed_log`/`_fed_count` 皆清空。
- `tests/test_zmq_receiver.py`：屬既有 baseline collection-error（待辦 #12
  ZMQ_SOURCES OS-env gap），不在此修；以隔離方式驗證 `feed` 呼叫帶
  `frame_id`（或於 `test_hls_manager` 層級驗 `HLSManager.feed` 透傳）。
- 全套件以 `--ignore=tests/test_main.py --ignore=tests/test_zmq_receiver.py`
  跑，對照 HEAD baseline（既有 5 失敗：`test_default_mot_worker_threads` +
  4 `test_stream_router` 404）確認零回歸。
- 前端無自動化測試（既有狀況）→ 列瀏覽器待測：HUD `src=FID`、拖動/直播時
  框貼齊豬隻、長時間（過 ffmpeg 整點 restart）不再漸進落後、thermal/舊錄影
  降級不崩。

## 範圍邊界（明確不做）

- 不動 ffmpeg 指令、不新增 endpoint、不加 sidecar 持久化。
- 不改 VOD 任何路徑（已查證 `vodStartTs + currentTime` 配 log，本就正確）。
- 不刪 `live_pdt_offset_seconds` / `pdt_offset` 機制（保留為 fallback，
  降風險；移除與否另案）。
- 不碰 `ref/HybridSORT/`。
- per-camera offset 化不需要（FID 對應天生 per-stream，常數機制僅 fallback）。

## 提交與既有約束

- commit 授權、**不 push**（留本機 master，使用者自行 push）。
- `CLAUDE.md`、`ref/HybridSORT/` 為 gitignore，永不 commit/force-add；
  HybridSORT 變更（本案無）記 `docs/hybridsort-local-patches.md`。
- 回應一律繁體中文。
