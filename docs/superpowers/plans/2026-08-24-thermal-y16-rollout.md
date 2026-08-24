# 熱像改送 Y16 溫度場：落地狀態與交接

**日期**：2026-08-24（凌晨改版並部署完成，體溫於當天 10:30 在正式機驗證通過）
**狀態**：**落地完成，全數驗證通過**。影像鏈路與體溫鏈路都在正式機實測過。

---

## 0. 接手的第一件事

照順序做，不要跳：

1. **跑驗證，不要相信這份文件裡的數字**：
   ```
   ssh pig-agri 'cd ~/lobby/pig-agri && bash scripts/verify_thermal_rollout.sh'
   ```
2. 看 10:30 那次排程跑出什麼：
   ```
   ssh pig-agri 'journalctl --user -u pig-thermal-verify -n 40 --no-pager'
   ```
3. 實際狀態跟這份文件對不上時，**先改文件再做新的事**。

---

## 1. 這次改了什麼

起點是「一個鏡頭一天用 18GB 正常嗎」，量下去發現熱像那一路整條是錯的。

### 根本問題：體溫功能整條是壞的

`inference/pipeline.py` 舊的 `_compute_thermal_intensity` 有三層疊在一起的錯誤：

| # | 程式碼寫的 | 實際是 | 後果 |
|---|---|---|---|
| 1 | rgb 硬編 `640x480` | bbox 座標系是 rgb 的 `1280x720` | 換算係數錯一倍 |
| 2 | 熱像硬編 `160x120` | 傳進來的是擷取端上採樣後的 `1280x720` | 取樣被 clamp 在圖的左上角約 1/8 寬 × 1/6 高 |
| 3 | 對陣列取 `np.mean` | 那是 turbo colormap 後的 BGR | 取到的是顏色亮度；turbo 亮度不單調（綠比紅亮），跟溫度連單調關係都沒有 |

合起來：**每一隻豬的「體溫」都取自熱像左上角同一小塊，而且取的是顏色不是溫度**。
DB 裡 3257 萬筆 `thermal_intensity` 落在 8.00~250.70、平均 116.81 —— 那是 0~255
的像素值，前端「體溫」欄位顯示的就是它。

⚠ **舊測試把錯誤的假設固化了**：它餵 640×480 的 bbox 配 160×120 的熱像，剛好符合
那兩組硬編值，所以這個 bug 一直是綠的。新測試特地用正式機的真實組合。

### 修法：擷取端直接送溫度

不是修正那幾個常數，而是讓 rpi5 送溫度而不是圖。

**rpi5**（`~/rgbt_edge/`，獨立 git repo，commit `6b7efe9`）：
`sender_config_dual.yaml` 的 thermal `mode: y16_png` → 直送原生 160×120 的 Y16
（Kelvin×100）無損 PNG16。舊路徑（`y16_tlinear`，假色 preview JPEG）完整保留。

**server**（`efd8881`）：
- `zmq_receiver._handle_thermal` 用檔頭（`\x89PNG` vs `\xff\xd8`）分辨兩種 payload，
  封包格式沒動，舊 sender 照常運作。
- `thermal_render.py` 用**固定溫度範圍**（`thermal_preview_min_c/max_c`，預設 25~40°C）
  上色成 640×480 餵 HLS。
- `_compute_thermal_celsius` 兩邊尺寸都用實際值；拿到三維陣列（舊 JPEG 路徑）回 None。
- 熱像的 hls feed 補上 `capture_ts`（以前熱像沒有擷取時間，VOD 只能估算）。
- DB 新增 `thermal_celsius`；舊的 `thermal_intensity` 保留但不再寫入。

### 實測效益

| | 改版前 | 改版後 |
|---|---|---|
| thermal ZMQ | 86 KB/幀，6.42 Mbps | 22 KB/幀，1.66 Mbps |
| ZMQ 合計 | 7.77 Mbps | 3.12 Mbps |
| 熱像 HLS | 1280×720（4:3 被拉成 16:9），531 MB/小時 | 640×480，約 125 MB/小時 |
| 熱像每天 | 6.0 GB | 約 1.5 GB |
| 相鄰幀平均像素差 | 1.03（每幀 percentile） | 0.23（固定範圍） |

rgb 那一路沒動：18 KB/幀、1.35 Mbps @720p9，本來就正常。

---

## 2. 還沒驗證的事，與預先註冊的門檻

改版當晚豬舍沒開燈，rgb 全黑 → 偵測不到豬 → 沒有 `tracking_logs` 也就沒有體溫，
04:26 那次自動驗證因此 FAIL（`tracking_logs` 完全沒有新資料）。天亮後補驗通過：

| 時間 | 判定 | 體溫筆數 | 值域 | 同一秒跨豬標準差 |
|---|---|---|---|---|
| 08-24 04:26 | ❌ FAIL | 0（豬舍全黑，偵測不到豬） | — | 算不出來 |
| 08-24 10:30（排程） | ✅ PASS | 173576/173576 | 28.80 ~ 41.94°C | **2.453°C** |
| 08-24 稍後（手動重跑） | ✅ PASS | 51258/51258 | 29.58 ~ 41.56°C | **1.904°C** |

標準差 1.9~2.5°C 遠高於 0.3°C 門檻：取樣確實跟著各自的 bbox 走。舊 bug 的特徵
（每隻豬同一個數字）不存在。

本機端到端測試的結果（作為對照）：畫面四角＋中央的 bbox 各自取到
29.35 / 29.92 / 33.92 / 34.38 / 30.28 °C；舊版不管 bbox 在哪都回同一個 31.57。

門檻寫死在 `scripts/verify_thermal_rollout.sh` 裡，**不是看到數字之後才決定的**：

| 判定 | 條件 |
|---|---|
| ✅ PASS | 帶體溫的紀錄 ≥ 100 筆，值域落在 20~45°C，且**同一秒不同 object_id 的體溫標準差 ≥ 0.3°C** |
| ⚠ WARN | 有資料但算不出多隻豬之間的差異（只偵測到一隻等） |
| ❌ FAIL | 沒有資料／值域不合理／標準差過小／熱像 HLS 不是 640×480 |

**標準差那一條是關鍵**：舊 bug 的特徵就是每隻豬拿到同一個數字。光看「有資料、範圍
合理」會漏掉退化。

### 自動驗證已經排好

- `pig-thermal-verify.timer`（user timer，`OnCalendar=*-*-* 10:30:00`，`Persistent=true`）
- 結果推 ntfy（`https://ntfy.ed716.duckdns.org/experiments`）
- 已手動觸發驗證過管線：`ntfy 200`，推播會到
- **每天跑但不是每天吵**：第一次 PASS 會推播並留下 `.verify_thermal_passed`，
  之後 PASS 就安靜，退化或無法判定才再推
- 已於 08-24 10:30 進入「安靜」狀態（首次 PASS 完成）

---

## 3. 出事怎麼退回

兩邊各自獨立，可以只退一邊：

```
# 擷取端退回舊的假色 preview（體溫會變成 None，但影像照常）
ssh pig-rpi5 "cd ~/rgbt_edge && sed -i 's/mode: \"y16_png\"/mode: \"y16_tlinear\"/' sender_config_dual.yaml && systemctl --user restart rgbt-sender"

# server 退回
ssh pig-agri "cd ~/lobby/pig-agri && git revert -m 1 efd8881 && systemctl --user restart pig-agri-tmux.service"
```

rpi5 改動前的原檔也留在 `~/rgbt_edge/*.bak-20260824`。

---

## 4. 這次排除掉的路（不要重新推導）

- **不是「調小 preview 尺寸」就好**。降到 640×480 只省 53%，而且體溫仍然是假的：
  問題的核心是送圖而不是送溫度。
- **不是調 CRF**。thermal 跟 rgb 共用 `hls_crf=23`，調高能省容量，但跟體溫錯誤無關。
- **不能從 colormap 反推溫度**。turbo/jet 的亮度不單調，數學上不可逆。
- **不要改回每幀 percentile 正規化**。它同時毀掉兩件事：顏色的絕對溫度意義（同一隻
  豬不同幀不同色），以及 H.264 的 P-frame（畫面裡任何東西一動整張圖就跳色）。
- **`node --check foo.js` 不是有效的前端檢查**（同一天踩到）。它對 `.js` 走 CommonJS
  解析器，物件字面量少一個逗號都放行回 exit 0。用 `./scripts/check_js.sh`。
- **08-22 17:00 ~ 08-23 全天沒有錄影與 tracking 不是 bug**：停電維修，已確認。

---

## 5. 已知還沒處理的

### 容量（使用者明確說先不管，但總有一天要面對）

`HLS_RETENTION_DAYS=90` × 每天用量 ≈ 1.5TB，而錄影碟就是系統碟 `/dev/nvme0n1p2`
468G、當時已用 89%（剩 52G）。**數學上不可能滿足，保留天數永遠不會先觸發、磁碟
一定先滿**。這次熱像改版把每天 17GB 降到約 12.5GB，緩解但沒解決。

碟滿的後果不只是停止錄影：`storage_monitor` 在剩餘空間低於 `storage_min_free_gb=10`
時會切 ephemeral（live 續、不落地），但系統碟本身滿了會連 postgres 跟 app 一起拖垮。

三個施力點：把 1TB 錄影碟從舊機器搬過來、調降保留天數、rgb 提高 CRF
（推論吃的是 ZMQ 原始幀，不經過 HLS，所以降 HLS 畫質對追蹤與活動量零影響）。

### 其他

- `cam_02` / `cam_03` / `rpi_sensors` 三台最後資料停在 08-20，共佔 41G。是否已永久
  移除、可否清掉，沒有問過。
- **擷取端改變 thermal 影像尺寸後，server 的 ffmpeg 不會自己跟上**：mjpeg pipe 的
  尺寸在第一幀就定了，之後送不同尺寸只會被硬縮放。這次靠重啟 app 解決，但 config
  再改一次又會發生。值得讓 `hls_manager` 偵測輸入尺寸變化自動重啟 ffmpeg。
- `thermal_preview_min_c/max_c` 目前只能用 `.env` 調、要重啟。現場季節溫差大的話，
  做成 DB-backed（前端可調）會比較好用 —— CLAUDE.md 有寫「新增 DB-backed 設定＝改 5 處」。
- rpi5 的 rgb 沒有動過。18 KB/幀很正常，但如果之後要再省，`send_fps: 9` 跟
  `jpeg_quality: 80` 都還有空間。

---

## 6. 接手用的 prompt

```
接手 pig-agri 的熱像/體溫改版驗證。

Where: /home/lazoark/OneDrive/Curriculum/pig-agri，branch master。
  正式機 `ssh pig-agri`（~/lobby/pig-agri），擷取端 `ssh pig-rpi5`（~/rgbt_edge）。

First action:
  ssh pig-agri 'cd ~/lobby/pig-agri && bash scripts/verify_thermal_rollout.sh'
  相信它的輸出，不要相信文件裡寫的數字。

What is running: pig-thermal-verify.timer 每天 10:30 跑上面那個驗證並推 ntfy。
  第一次 PASS 之後就安靜，只有退化才會再推。

Background: docs/superpowers/plans/2026-08-24-thermal-y16-rollout.md
  ——尤其第 4 節（這次排除掉的路，不要重新推導）與第 5 節（還沒處理的）。

Pending decision: 體溫在正式機還沒驗證過（改版當晚豬舍沒開燈，偵測不到豬）。
  門檻已預先寫死在 verify script 裡：帶體溫紀錄 ≥100 筆、值域 20~45°C、
  且同一秒不同 object_id 的體溫標準差 ≥0.3°C。最後那條是關鍵——舊 bug 的特徵
  就是每隻豬拿到同一個數字，只看「有資料、範圍合理」會漏掉退化。
  FAIL 的話退回方式在文件第 3 節。

Do not touch:
  · 不要把 thermal 的上色改回「每幀算 min/max 或 percentile」（理由在文件第 4 節）
  · 不要用 `node --check static/js/foo.js` 驗前端，用 ./scripts/check_js.sh
  · 不要重跑 scripts/migrate_merge.sh finish（migrate_own_boundary 表還要用）
  · rpi5 上不要 kill sender 而不重啟 service，它現在是 rgbt-sender.service
```
