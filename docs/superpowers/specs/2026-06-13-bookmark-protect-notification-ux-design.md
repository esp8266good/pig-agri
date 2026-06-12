# 書籤/保留/通知 UX 補完 設計

> 狀態:設計已確認,待寫實作計畫。子系統 D。

## 1. 動機

子系統 B 與 Phase 5 的功能架構皆完整,但 UI 缺口讓使用者無法閉環:

- **書籤備註不可見**:`POST /storage/segments` 已存 `note`,但 `loadBookmarks()` 只顯示 `label`(`★ ${b.label}`),`note` 從未渲染。
- **書籤不可編輯**:`PUT /storage/segments/{id}`(`update_saved_segment`)後端早就有,前端無入口。建好後想改名或補備註只能砍掉重建。
- **保留無法取消**:🔒 標記只是視覺,沒有任何 UI 觸發 `DELETE /storage/segments/{id}`(該 endpoint 後端存在)。要解保留只能走「刪除錄影」流程(連影片一起刪)——錯誤的工具。
- **通知已讀 = 沒有變化**:`PUT /alerts/{id}/read` 設 `is_read=TRUE`,但前端只把按鈕 disable + 移除 `.unread` class,該筆**仍在清單**。使用者直覺「我處理掉了」,看到還在會困惑。
- **通知無法刪除**:`health_alerts` 表只增不減,長期積累。前端沒有任何刪除入口。

## 2. 範圍與決策(brainstorm 已確認)

- **#2-A 書籤編輯**:Modal 對話框(統一風格、可同時改 label+note)。
- **#2-B 書籤備註顯示**:`loadBookmarks()` 列出每筆時把 `note` 顯示在 label 下方(若有)。
- **#2-C 取消保留入口**:timeline-bar 上的 🔒 / ★ 標記本身成為**獨立可點擊熱區**(≥28×28px hit area,行動裝置友善),點擊 stopPropagation 開出 action sheet/popover。slot 本體點擊仍是「播放 VOD」,語意不變。
  - ★(書籤):popover →「編輯 / 取消書籤」
  - 🔒(保留):popover →「取消保留」
- **#3-A 已讀後行為**:已讀立刻**從預設清單移除**(視覺即刻反映「處理掉了」)。預設 fetch `unread_only=true`。
- **#3-B「顯示已讀」toggle**:通知中心面板頂端 checkbox,切換 fetch 是否帶 `unread_only`。打開時顯示全部(已讀+未讀)。
- **#3-C 永久刪除**:單筆「刪除」按鈕 + 頂端「清空已讀」批量。
- **後端新 endpoint(最小集)**:
  - `DELETE /alerts/{alert_id}` — 單筆永久刪除
  - `DELETE /alerts?read_only=true[&camera_id=...]` — 批量清除(只刪 `is_read=TRUE` 的,可選依攝影機篩)
- **`health_alerts` retention 自動清理**:**非本次目標**(技術債,YAGNI)。使用者批量清除已足夠。

## 3. 後端改動(最小)

### 3.1 `db_writer.py` 新增
```python
async def delete_alert(pool: asyncpg.Pool, alert_id: int) -> bool:
    """單筆永久刪除。回傳是否有列被刪。"""
    result = await pool.execute("DELETE FROM health_alerts WHERE id = $1", alert_id)
    return result != "DELETE 0"

async def delete_alerts_bulk(
    pool: asyncpg.Pool,
    read_only: bool = True,
    camera_id: Optional[str] = None,
) -> int:
    """批量刪除。預設只刪已讀;可選依攝影機 narrow。回傳刪除筆數。"""
    conditions: list[str] = []
    params: list = []
    if read_only:
        conditions.append("is_read = TRUE")
    if camera_id is not None:
        conditions.append(f"camera_id = ${len(params) + 1}")
        params.append(camera_id)
    where = "WHERE " + " AND ".join(conditions) if conditions else ""
    result = await pool.execute(f"DELETE FROM health_alerts {where}", *params)
    return int(result.split()[-1]) if result else 0
```

### 3.2 `routers/alerts.py` 新增
```python
@router.delete("/{alert_id}")
async def delete_one(alert_id: int):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(503, "Database not available")
    found = await delete_alert(pool, alert_id)
    if not found:
        raise HTTPException(404, "Alert not found")
    return {"ok": True}

@router.delete("")
async def delete_bulk(read_only: bool = True, camera_id: Optional[str] = None):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(503, "Database not available")
    n = await delete_alerts_bulk(pool, read_only=read_only, camera_id=camera_id)
    return {"deleted": n}
```

**安全**:`read_only` 預設 True 是「保險」——拒絕意外刪除未讀;若前端真要全清(極少),仍可顯式 `read_only=false`,但 UI 不暴露這個能力。

### 3.3 既有 endpoint 維持不變
`PUT /storage/segments/{id}`(`update_saved_segment`)、`DELETE /storage/segments/{id}`(`delete_saved_segment`)、`GET /storage/bookmarks`、`GET /alerts` 全部沿用。

## 4. 前端改動(`static/index.html`)

### 4.1 書籤編輯 Modal
- 新增 `#bookmark-edit-modal`(沿用既有 `#delete-modal` 結構/CSS)。
- 內含:標題、`<input>` 名稱、`<textarea>` 備註、取消/儲存 按鈕。
- `openBookmarkEditModal(seg)` 帶入 `{id, label, note}` 預填 → 儲存呼叫 `PUT /storage/segments/{id}` body `{label, note}` → `loadBookmarks()` + `loadTimeline()`。
- 取消保留也走此 modal?**否**——保留無 label/note,直接 confirm 後 `DELETE /storage/segments/{id}`,免兩層彈窗。

### 4.2 書籤列表渲染 note
`loadBookmarks()` 已有 `bookmarks.forEach(b => { ... link.textContent = '★ ' + b.label; ... })`,擴充:
- 在 `<a>` 下面加 `<div class="bm-note">${b.note}</div>`(僅當 `b.note` 為非空字串)。
- 移除按鈕旁加「編輯」按鈕,onclick `openBookmarkEditModal(b)`。

### 4.3 timeline 標記成為可點擊熱區
**關鍵 UX 細節**:現在 `.timeline-slot.bookmarked::after { content: "★"; position: absolute; top: 0; right: 1px; font-size: 8px }` 是 pseudo-element,**無法獨立點擊**。改成真正 DOM 元素。

- `renderDayBar()` 對每個 slot:若 `savedSegmentsMap.has(slotTs)`,在 slot 內 append `<button class="slot-marker">` 顯示 ★(label) 或 🔒(no label)。
- CSS:`.slot-marker` 絕對定位右上;`padding` 與 `min-width/height` 使視覺仍小但 hit area ≥28×28px;`background: transparent; border: none`;`touch-action: manipulation`;`pointer-events: auto`(slot 父其餘區仍可點)。
- onclick:`event.stopPropagation()` + 開 popover/action sheet。
  - ★ → 「編輯書籤」「取消書籤」「取消」
  - 🔒 → 「取消保留」「取消」
- 移除舊 `::after` CSS pseudo-element。
- Action sheet:輕量(absolute positioned below the slot 或 center modal,行動裝置友善);沿用 modal 風格,**不引入新框架**。

### 4.4 通知中心 UI
- 通知 tab 內容頂端加 toolbar:
  - 「顯示已讀」`<input type="checkbox" id="show-read-toggle">`
  - 「清空已讀」`<button id="clear-read-btn">` (僅當顯示已讀 = false 時 enabled? 不,任何時候皆可用——批量清除是固定動作)
- `refreshNotifications()` 改:
  - 預設 `unread_only=true`(`showRead=false` 時)
  - toggle 開 → `unread_only=false`
- `markAlertRead(id, btn)` 改:
  - 呼叫 `PUT /alerts/{id}/read` 成功後,**從 DOM 移除該筆 `<li>`**(若 `showRead=false`)
  - 若 `showRead=true`,該筆改 `.unread` → 已讀樣式(原本行為)
- 每筆右側加「刪除」按鈕:`onclick` confirm → `DELETE /alerts/{id}` → 從 DOM 移除。
- 「清空已讀」`onclick` confirm → `DELETE /alerts?read_only=true&camera_id={currentCamera}` → `refreshNotifications()`。
- bell badge 邏輯不變(永遠算 unread count)。

## 5. 邊界 / 錯誤處理

- 書籤編輯 PUT 失敗(404 / 503)→ alert + modal 不關。
- `DELETE /alerts` 批量無對應筆 → `{deleted: 0}` 仍 200;前端顯示「無已讀可清除」(僅當為 0)。
- timeline 標記 popover:點外部關閉(`document.click` listener,記得 cleanup)。
- 同一個小時切換 label/note 後 `savedSegmentsMap` 必須更新——`loadBookmarks` 與 `loadDaySegments` 後重新渲染。
- 切攝影機/切月/切日 → 關閉開啟中的 modal/popover(避免殘留)。

## 6. 測試策略

### 後端(pytest)
- `test_db_writer.py`:`test_delete_alert_ok`、`test_delete_alert_missing_returns_false`、`test_delete_alerts_bulk_read_only`、`test_delete_alerts_bulk_camera_filter`。
- `test_alerts_router.py`:`test_delete_one_ok`、`test_delete_one_404`、`test_delete_one_503`、`test_delete_bulk_ok`、`test_delete_bulk_default_read_only_true`。

### 前端
- `node --check` JS 語法檢查(沿用既有方式)。
- 瀏覽器驗收(使用者):
  1. 書籤列表顯示 note;「編輯」開 modal、改名/改備註後立刻反映。
  2. 點 timeline 🔒:popover → 取消保留 → 🔒 消失、檔案還在。
  3. 點 timeline ★:popover → 編輯/取消書籤,各自正確。
  4. 標記區 hit area 在手機上夠大(可試 width 50% 的小 slot)。
  5. 通知按已讀 → 該筆立刻從清單消失;bell 數字 -1。
  6. 「顯示已讀」打開 → 已讀筆數重現;每筆有「刪除」。
  7. 「清空已讀」→ 確認後一次清光,DB 也少了對應列。
  8. 切換攝影機/分頁:無殘留 popover/modal/錯誤狀態。

## 7. 非目標(YAGNI)

- `health_alerts` 自動 retention 機制(批量清除已涵蓋實際需求)。
- alert 撤回(取消已讀):一旦讀就讀。需要可走 SQL。
- 書籤匯出/匯入。
- popover 動畫過場。
- 多選書籤批量編輯。

## 8. 與既有功能的相容

- `savedSegmentsMap` 結構不變(`Map<hour_ts, {id, label, note}>`)。
- `hour_ts` 值域不變(3600 倍數)。
- 子系統 B 的刪除錄影流程不變;`保留/書籤` 交集警示邏輯沿用。
- 子系統 C 的月曆/日格不變;只動格內 markers 渲染方式(pseudo-element → 真 button)。
- 後端 `purge_expired_hls` 受 `get_protected_hours` 保護的邏輯不變(取消保留後該小時可進入回收清單;符合預期)。
