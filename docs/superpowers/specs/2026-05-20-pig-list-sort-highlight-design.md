# 下方 ID 列排序 + 點選強調/只顯示 設計

> 狀態：設計已確認，待寫實作計畫。純前端（`static/index.html`），後端零改動。

## 1. 動機

採血當天往往**沒有任何豬被標記異常**，操作者只能直接找「活動力最低」的豬
採血。但網頁底部 pig 列表固定按偵測順序排列、影片上的 bbox 又密集（尤其手機
小畫面），很難定位目標豬。本功能讓操作者：

1. 把底部 ID 列**依活動量 / 體溫 / ID 排序**，活動量最低的豬直接置頂。
2. **點該列即在影片上強調該豬的 bbox**（其餘變淡），並可一鍵**只顯示該豬**。

服務的核心任務：快速、可靠地在擁擠畫面中鎖定要採血的低活動豬。

## 2. 範圍

- **僅改 `static/index.html`**（狀態、`renderPigStatus`、`drawBoxes`、表頭與面板
  標記、切換時的重置）。
- live 與 VOD 模式皆生效（pig 列表與 `drawBoxes` 兩模式共用）。
- **不**新增後端端點、**不**動 `anomalyMap`/WS/`/alerts/active` 資料流、**不**加
  「點影片 bbox 選取」（明確排除，手機上難點中）。

## 3. 新增前端狀態

| 變數 | 型別 | 預設 | 用途 |
|---|---|---|---|
| `selectedObjectId` | `int \| null` | `null` | 目前選取要強調的豬 object_id |
| `soloMode` | `bool` | `false` | 「只顯示選取」開關狀態 |
| `sortKey` | `'activity' \| 'temp' \| 'id'` | `'activity'` | 排序依據 |
| `sortDir` | `1 \| -1` | `1`（升序） | 排序方向（1=低→高 / 小→大） |

`sortKey`/`sortDir` 為使用者偏好，切攝影機/模式時**保留**；`selectedObjectId`/
`soloMode` 在切換時**重置**（見 §7）。

## 4. 功能一：排序

改 `renderPigStatus()`（現約 `static/index.html:1204`，現行直接 iterate
`currentObjectIds` Set）：

1. 把 `currentObjectIds` 組成陣列，每筆 `{oid, act, temp, anomalous}`，其中
   `act = anomalyMap[oid]?.activity_current ?? null`、
   `temp = anomalyMap[oid]?.temp_current ?? null`、
   `anomalous = activity_anomaly || temp_anomaly`。
2. 依 `sortKey` 取對應值排序：
   - `'id'` → 比 `oid`（數值）。
   - `'activity'` → 比 `act`；`'temp'` → 比 `temp`。
   - **null 值永遠排在最後**（無論 `sortDir`）——未分析到的豬不得佔據「活動量
     最低」頂端而誤導採血。非 null 之間依 `sortDir` 升/降。
3. 依排序後陣列渲染列（沿用現有列 HTML：`#oid` / 活動 / 體溫 + 既有
   `.anomaly-row`/`.anomaly-cell` 樣式）。

表頭 `<tr><th>豬隻</th><th>活動量</th><th>體溫</th></tr>` 三個 `<th>` 皆可點：

- 點目前作用中的欄 → 切換 `sortDir`（1↔-1）。
- 點其他欄 → `sortKey` 換成該欄、`sortDir` 重設為該欄的預設方向（activity/temp
  預設升序=低→高/低溫先；id 預設升序）。
- 作用中欄的 `<th>` 顯示方向指示（`▲` 升 / `▼` 降）；非作用欄不顯示。
- `<th>` 加 `cursor:pointer` 與 hover 視覺、`role`/`aria-sort` 以利無障礙與手機點擊。

任何排序變更後重新呼叫 `renderPigStatus()` 重繪列表（不影響影片/選取）。

## 5. 功能二：點列強調

`renderPigStatus()` 渲染每列時：

- 列加 `data-oid` 與點擊 handler：點擊 → 若 `selectedObjectId === oid` 則設
  `null`（取消），否則設為 `oid`；接著重繪列表並（強調由 `drawBoxes` 每幀自然
  反映，無需手動重畫 canvas）。
- `selectedObjectId === oid` 的列加 `.selected` 樣式（高亮底色），便於對照。

`drawBoxes()`（現約 `static/index.html:1497` 的 `for (const o of displayBoxes)`
迴圈）：

- `selectedObjectId === null` 時：行為與現狀**完全一致**（安全網）。
- `selectedObjectId !== null` 時：
  - 該 `o.object_id === selectedObjectId` 的框 → **加粗線寬 + 全不透明**（強調）。
  - 其餘框 → **降低透明度（淡化、不轉色）**，仍畫出（含 ID 標籤與異常圖示，
    一併淡化）。
  - 透過 `ctx.globalAlpha` 控制淡化、`ctx.lineWidth` 控制加粗，畫完每框後還原，
    不影響既有上色（異常紅框 `#ff4444` 邏輯不變，只疊加強調/淡化）。

## 6. 功能三：「只顯示選取」開關

- 在 pig 分頁（`#tab-pig-status`）表格上方加一個 checkbox（label：「只顯示選取的
  豬」），綁 `soloMode`。
- `drawBoxes()` 取要畫的框時：若 `soloMode && selectedObjectId !== null` →
  **只保留 `object_id === selectedObjectId` 的框**，其餘完全不畫；否則畫全部
  （即 `soloMode` 開但未選取時，等同未開、畫全部，不致整片空白）。
- 此過濾在 §5 的強調/淡化之前（只剩選取框時，該框正常以強調樣式畫）。

## 7. 重置時機

下列既有切換點，除現有清理外，加 `selectedObjectId = null; soloMode = false;`
（並同步 UI：清 `.selected`、checkbox 取消勾選）：

- 切攝影機（`camSelect` change handler）。
- 切 RGB↔Thermal（type 切換路徑）。
- Live↔VOD 來回（`loadVod` / `switchToLive`）。

`sortKey`/`sortDir` 不重置。

## 8. 邊界與互動

- 選取的豬暫時消失（遮擋/掉追蹤）：該列消失，但 `selectedObjectId` 保留 →
  該豬重現時自動恢復高亮（避免每次遮擋都要重點）。`drawBoxes` 找不到該 id 的框
  時，強調無對象、其餘框照 §5/§6 規則（solo 開且選取者不在場 → 該幀無框，屬預期）。
- `act`/`temp` 為 `null` 的列在排序中沉底；列內顯示維持現有 `—`。
- 排序與選取彼此獨立：排序改變不清除選取（選取以 object_id 記，與列位置無關）。

## 9. 測試策略

- `static/index.html` 無單元測試框架（與本專案既有前端改動一致）。
- `node --check`（抽出 `<script>`）確認 JS 語法。
- 後端零改動 → Python 測試套件維持 `143 passed`（4 既有 ZMQ_SOURCES 失敗無關）。
- **瀏覽器驗收清單**（使用者執行）：
  1. 點「活動量」欄頭，列表依活動量升序、最低置頂；再點切降序；`—` 永遠沉底。
  2. 點「體溫」「豬隻」欄頭排序正確、方向指示 ▲▼ 正確。
  3. 點一列 → 影片上該豬框加粗變亮、其餘變淡；再點同列取消、恢復全亮。
  4. 勾「只顯示選取」→ 只剩該豬框；取消勾恢復全部；未選取時勾選不致空白。
  5. 切攝影機 / RGB↔Thermal / Live↔VOD → 選取與 solo 自動重置、無殘留高亮，
     排序偏好保留。
  6. 選取的豬被遮擋消失再出現 → 高亮自動恢復。
  7. VOD 拖曳時間軸下，排序/選取/只顯示行為與 live 一致。

## 10. 非目標（YAGNI）

- 點影片 bbox 選取（手機難點中，已排除）。
- 多選 / 同時強調多隻。
- 排序偏好持久化到 localStorage / 後端（單一 session 內保留即可）。
- 後端排序或新端點（資料已在前端 `anomalyMap`）。
