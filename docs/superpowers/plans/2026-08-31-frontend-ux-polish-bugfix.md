# 前端 UX 改版：修掉被撤回的 bug 再重新上線

## 這份文件怎麼用

你是接手的新 session。這份文件是**唯一的事實來源**，每一條事實都在 2026-08-31
實測過，不是抄舊筆記。照 Task 1 → 5 的順序走。

**目前進度：0/5，一步都還沒執行。** 產生這份文件的 session 只做了唯讀查證
（`ssh pig-agri` 上的 `grep .env`／`git log`／`ss -ltnp`、本機 `git log`／`grep`），
**沒有部署、沒有 push、沒有改任何 code**。唯一的副作用是正式機跑過一次
`git fetch`（只更新遠端 refs，working tree 沒動）。

**使用者只做最後一次驗收（Task 5）。** 中間的每一輪驗證都由你自己用
headless 瀏覽器跑，不要中途丟問題給他、不要請他幫忙點畫面確認。
他要的是「跑完、修好、部署好，然後叫我看」。

## 不要做的事

| ⛔ | 為什麼 |
|---|---|
| 中途請使用者幫忙點畫面／回報症狀 | 他只做 Task 5 的最終驗收。驗證是你的工作，工具都給你了 |
| 重跑「正式機是不是開著 auth」 | 已實測 `AUTH_ENABLED=false`，見下面的否證表 |
| 重新設計 header／設定面板 | 設計已核准（附錄有 before/after 對照圖），這次只修 bug |
| 讀 `RESUME_frontend_ux.md` | 已被這份文件取代，且有兩處事實是錯的 |
| 三項改動綁在一起上線 | A／B／C 互相獨立。哪一項修不好就退掉那一項，其他兩項照上 |
| 在驗證腳本裡寫入任何設定／遮罩／通知 | 正式機是真的在跑的服務。腳本**只准讀**，見 Task 1 的唯讀約束 |

---

## 開工前的事實（2026-08-31 實測）

| 項目 | 值 |
|---|---|
| 本機 master | `d6c1328`（favicon + logo）**未推上 origin** |
| `origin/master` | `9d1f4c8` |
| 本機分支 | `frontend-ux-polish` = `18405aa`，疊在 `d6c1328` 上 |
| `origin/frontend-ux-polish` | 已推，與本機一致 |
| Worktree | `.claude/worktrees/agent-ad9adf838d6dc69dc`（`18405aa`） |
| **正式機 HEAD** | **`206c970`**，working tree 乾淨 |
| **正式機 favicon** | **不存在**。`git reset --hard 206c970` 把 UX 改版和 favicon 兩個 cherry-pick 一起洗掉了 |
| 正式機落後 | `origin/master` 5 個、本機 master 6 個 |
| 正式機 `AUTH_ENABLED` | **`false`**（不會出現登入畫面，`#logout-btn` 全程 `hidden`） |
| 正式機 URL | `http://192.168.50.48:5005`（`ssh pig-agri` 是同一台，`~/lobby/pig-agri`） |
| 正式機重啟 | `ssh pig-agri 'systemctl --user restart pig-agri-tmux.service'` |
| 本機 headless 工具 | `.venv` 內 **playwright 可用**，chromium `149.0.7827.55`，實測 `p.chromium.launch()` 成功 |
| 本機起 app | `uv run uvicorn main:app --port 18321`（本專案 headless 驗證的慣用 port） |

驗證指令（要複查時跑）：

```bash
git log --oneline -1 master; git rev-parse --short origin/master
ssh pig-agri 'cd ~/lobby/pig-agri && git log --oneline -1 && ls static/favicon.ico'
```

### 已經否證的兩條，不要重查

| 假設 | 為什麼不成立 |
|---|---|
| ❌ 正式機開著 `AUTH_ENABLED=true`，登出鍵被收進 `hidden` 的選單裡出不來 | `.env` 實測 `AUTH_ENABLED=false` |
| ❌ cherry-pick 疊在落後 5 個 commit 的舊 base 上，前端跟後端對不起來 | `git log 206c970..origin/master -- static/` 是空的：那 5 個 commit 只動 `inference/pipeline.py`、`routers/*`、tests，一行前端都沒碰 |

### 改動內容（三項在同一個 commit `18405aa`，動 4 個檔案）

- **A. Header 收斂**：`#help-btn`／`#manual-link`／`#logout-btn` 移進新的 `#more-btn` → `#more-menu` 浮動選單，重用 `.slot-action-menu` 樣式。
- **B. 設定手風琴**：`#settings-drawer` 7 組設定 `<section class="settings-group">` → `<details class="settings-group">`，標題與說明放進 `<summary>`，預設只展開「異常分析」。
- **C. 色相**：`--thermal` `#ff8c42`→`#ff7043`、`--vod` `#e8a13c`→`#e6b23c`。

⚠️ 為什麼 590 個測試全過還是有 bug：`uv run pytest` 與 `./scripts/check_js.sh`
**都不載入瀏覽器**。所有症狀必然在這兩層之外——這就是 Task 1 要補的那一層。

---

## Task 1：寫自動驗證腳本（這是這次改版真正缺的東西）

**Files**: Create `scripts/check_frontend_ux.py`

沒有這支腳本，前端改動就只能靠人點，於是就會發生「推上去才發現壞了」。
腳本寫完之後這個專案永久多一道防線，不只這次用。

### 唯讀約束（違反會弄壞正在跑的服務）

腳本**只准讀**：不 POST 設定、不存遮罩、不刪錄影、不發通知。
需要驗「設定值讀得回來」時，只斷言 input 的 `value` 非空，**不要改值再存**。
點 `#mask-edit-btn` 只驗編輯器開得起來，**不要按儲存**。

### 介面

```bash
uv run python scripts/check_frontend_ux.py --url http://127.0.0.1:18321 --mode polished
uv run python scripts/check_frontend_ux.py --url http://192.168.50.48:5005 --mode baseline
```

- `--mode baseline`：只跑通用檢查（頁面載得起來、`console.error` 為 0、`/favicon.ico` 回 200）。
- `--mode polished`：通用檢查 ＋ 下面 10 項。
- `--viewport 1920x1080`（預設）／`390x844`（手機）。
- 退出碼 0 = 全過；非 0 = 有 ❌，且 stdout 每項一行 `✅/❌ #n 名稱：實際看到什麼`。

### 10 項斷言（selector 與預期值都已對照過原始碼）

| # | 斷言 | 若 ❌ 的根因與修法 |
|---|---|---|
| 1 | 點 `#more-btn` 後 `#more-menu` 的 `hidden` 為 false，`getBoundingClientRect()` 完全落在 viewport 內，且 `menu.top >= btn.bottom`、`abs(menu.right - btn.right) < 40` | `.slot-action-menu` 是 `position: absolute`，靠 `.more-wrap { position: relative }` 當錨點。位置跑掉就是這條被別的規則蓋掉了 |
| 2 | 開說明模式後重開選單，`#help-btn` 的 computed `color` 或 `background-color` 與未開啟時**不同** | `app.css:1008` 的 `#help-btn[aria-pressed="true"] { border-color: var(--accent) }` 失效：新元素吃 `.slot-action-menu button { border: none }`，沒有 border 可以上色。改 `background: var(--accent-dim); color: var(--accent)` |
| 3 | 說明模式開著 → 開選單 → 點畫面別處 → `#more-menu.hidden === true` | `help.js` 在**捕獲階段** `stopPropagation`，`main.js` 的 `onOutsideClick` 掛冒泡階段收不到。改成 `document.addEventListener('click', onOutsideClick, true)` |
| 4 | 說明模式開著 → 點 `#more-btn` → 選單仍打得開 | `help.js` 的 `PASSTHROUGH` 白名單要含 `#more-btn`。沒有的話說明模式下這個選單完全打不開，裡面三個項目全部點不到 |
| 5 | 點 `#video-max-btn` 進放大 → 開選單 → 按 `Escape` → 選單關了**且** `body` 仍有 `video-max` class | 新 keydown listener 註冊在 `main.js:301` 的 video-max listener **之前**，兩個都會跑。關完選單要 `e.stopImmediatePropagation()` |
| 6 | 開設定抽屜，7 個 `details.settings-group` 逐一點 `summary`：`open` 屬性要跟著切換，且 `open` 時對應 `.settings-form` 的 `offsetHeight > 0` | `open` 不切換 → `summary` 裡的 `<svg class="settings-chev">` 吃掉了點擊（給它 `pointer-events: none`）；展開沒高度 → `.settings-group .settings-form` 的 padding 或 `[hidden]` 全域規則衝突 |
| 7 | 把某個**收合著**的組（例如「推播通知」）的 `details.open` 設為 true 之前，先讀它裡面 input 的 `value`，斷言非空 | 證明 `loadSettings` 在 `display:none` 底下照樣寫得進值。空的話逐欄位比對 `static/js/api.js` 的 `_smap` |
| 8 | 展開「遮罩」→ 點 `#mask-edit-btn` → `#mask-editor` 不是 hidden（**不要按儲存**） | handler 假設抽屜可見，或假設按鈕在 `<section>` 而非 `<details>` 裡 |
| 9 | `getComputedStyle(document.documentElement).getPropertyValue('--thermal').trim() === '#ff7043'`、`--vod === '#e6b23c'` | 已 grep：`static/` 底下沒有硬編 `ff8c42`／`e8a13c`（只有 `player.js:452` 的註解提到）。低風險，但這條免費 |
| 10 | viewport `390x844` 重跑第 1 項：`rect.right <= innerWidth && rect.left >= 0` | `.more-menu { right: 0 }` 在窄螢幕的表現 |

全程 `page.on("console")` 收集，`console.error` 筆數必須為 **0**；有的話把訊息原文印出來。

- [ ] **Step 1**：寫腳本。
- [ ] **Step 2**：對**未改版**的本機 master 跑 `--mode baseline`，確認退出碼 0。
      這步是在驗腳本本身沒寫壞，不是在驗改版。
- [ ] **Step 3**：`./scripts/check_js.sh` 照跑（腳本是 Python，不影響，但保持習慣）。

---

## Task 2：對改版跑腳本，收集症狀

- [ ] **Step 1**：在 worktree 起 app

```bash
cd /home/lazoark/OneDrive/Curriculum/pig-agri/.claude/worktrees/agent-ad9adf838d6dc69dc
git log --oneline -1      # 應為 18405aa
uv run uvicorn main:app --port 18321
```

- [ ] **Step 2**：`uv run python scripts/check_frontend_ux.py --url http://127.0.0.1:18321 --mode polished`
      桌機與手機兩種 viewport 各跑一次。
- [ ] **Step 3**：把 ❌ 的項目抄下來，一項一行。這是 Task 3 的驗收清單。

若出現的症狀**不在那 10 項裡**，代表有我沒讀出來的東西：走
`superpowers:systematic-debugging`，先穩定重現再改，並**把新的斷言補進腳本**。

---

## Task 3：修到腳本全綠

**Files**: `static/js/main.js`、`static/js/help.js`、`static/css/app.css`、`static/index.html`

- [ ] **Step 1**：在 worktree 的 `frontend-ux-polish` 分支上修，一個症狀一個 commit，
      message 寫「症狀 → 根因 → 修法」。
- [ ] **Step 2**：每修一項就重跑 Task 2 Step 2，確認該項由 ❌ 變 ✅ 且沒有新的 ❌。
- [ ] **Step 3**：全綠之後跑完整驗證：
  - `uv run pytest -p no:cacheprovider` → 0 失敗（基準 590 過，少於這個數先當回歸查）
  - `./scripts/check_js.sh` → 12 檔全過
    ⚠ 不要用 `node --check static/js/<file>.js`：它對 `.js` 走 CommonJS 解析器，ES module 的錯一律放行 exit 0
    ⚠ 語法過關 ≠ 模組載得起來：改過 export 名稱要 grep 全部呼叫端
  - `check_frontend_ux.py --mode polished` 兩種 viewport 都退出碼 0
- [ ] **Step 4**：若某一項的修法會把改版的價值抵銷（例如「登出鍵根本不該收進選單」），
      **直接退掉那一項**，其他兩項照上。

⛔ **停損規則**：連續三輪改 code 都沒修好就**停止改 code**。改成：講出一個到目前為止
一直被當成真的、但可能是錯的假設；設計一個能分辨真假的**實驗**（不是問使用者）；
跑完實驗再動 code。真的走到「只有使用者答得出來」的地步才可以打斷他。

---

## Task 4：部署正式機，並在正式機上自測

- [ ] **Step 1**：推分支與 master

```bash
cd /home/lazoark/OneDrive/Curriculum/pig-agri
git push origin master                                  # 9d1f4c8 → d6c1328（favicon 從沒推過）
git push origin frontend-ux-polish --force-with-lease   # 帶著 Task 3 的修正
```

- [ ] **Step 2**：正式機先補到 `d6c1328`（把被誤刪的 favicon 帶回來），再疊改版

```bash
ssh pig-agri 'cd ~/lobby/pig-agri && git fetch && git merge --ff-only origin/master && git cherry-pick origin/frontend-ux-polish && git log --oneline -2'
ssh pig-agri 'systemctl --user restart pig-agri-tmux.service'
```

⚠ `--ff-only`：正式機 working tree 乾淨、HEAD 是 master 的祖先，能 ff。
若被拒代表正式機被人動過，**先查清楚為什麼，不要 `--hard`**。

回退指令（出事直接用，目標是 `d6c1328` **不是** `206c970`：上次就是回退回頭太多，
把 favicon 一起洗掉）：

```bash
ssh pig-agri 'cd ~/lobby/pig-agri && git reset --hard d6c1328 && systemctl --user restart pig-agri-tmux.service'
```

- [ ] **Step 3**：對正式機跑同一支腳本，兩種 viewport

```bash
uv run python scripts/check_frontend_ux.py --url http://192.168.50.48:5005 --mode polished
uv run python scripts/check_frontend_ux.py --url http://192.168.50.48:5005 --mode polished --viewport 390x844
```

- [ ] **Step 4**：`curl -sI http://192.168.50.48:5005/favicon.ico | head -1` → `200`
- [ ] **Step 5**：確認服務真的活著：`ssh pig-agri 'systemctl --user is-active pig-agri-tmux.service'`
      ＋ 看 `tmux` 輸出前 30 行沒有 traceback。

**這五步全過才可以進 Task 5。任何一步 ❌ 就回 Task 3，不要帶著已知的 ❌ 去找使用者。**

---

## Task 5：交給使用者驗收（唯一一次打斷他）

- [ ] 給他一段話，內容只有四件事：
  1. 網址 `http://192.168.50.48:5005`，**請先 Ctrl+Shift+R 強制重新整理**
     （改的是 `static/` 的 css/js，瀏覽器快取會讓他看到舊版，「改了沒反應」常常只是快取）。
  2. 這次修了哪幾個症狀，一項一行。
  3. 自動驗證跑了什麼、結果是什麼（10 項 × 2 種 viewport、pytest 590、check_js 12 檔）。
  4. 回退指令，貼在最後讓他知道隨時可以撤。

- [ ] 他確認 ✅ 之後收尾：
  - 本機 `frontend-ux-polish` merge 進 master、`git push origin master`
  - 正式機 `git reset --hard origin/master`（脫離 cherry-pick 狀態）
  - `git worktree remove .claude/worktrees/agent-ad9adf838d6dc69dc`
  - 刪掉 `RESUME_frontend_ux.md`
  - `scripts/check_frontend_ux.py` **留著**，它是這次真正的產出

---

## 附錄：當初核准的設計

- icon 候選比較：`https://claude.ai/code/artifact/744b082c-a037-4c73-82ae-f1441c6e2da0`
- header／設定面板／色彩提案（before/after 對照）：`https://claude.ai/code/artifact/2e4231e4-4a20-4668-827c-c353b5da779e`

---

## 執行結果（2026-08-31 完成，5/5）

正式機 `cf946ff`，`origin/master` 同步。使用者驗收 ✅。

實際修掉的三個症狀（前兩個是計畫預測的 #3／#5，第三個不在計畫裡）：

| 症狀 | 根因 | 修法 | commit |
|---|---|---|---|
| 說明模式下點畫面別處，「更多」選單關不掉 | `help.js` 在捕獲階段 `stopPropagation`，outside-click 掛冒泡階段收不到 | 改掛捕獲階段；`removeEventListener` 的 capture 旗標要一起改，否則拆不掉；「點的是不是按鈕自己」改用 `closest('#more-btn')`，否則捕獲階段先關掉、按鈕 handler 又開回來 | `405316e` |
| 放大影片時按 Escape，選單關了也一併退出放大 | 兩條 keydown 都掛 `window`，前一條沒擋掉後一條 | `stopImmediatePropagation()`（`stopPropagation` 不夠：同一個節點） | `5967ef7` |
| `/favicon.ico` 回 404 | 只有 `/static/favicon.ico`，根目錄沒路由。**未改版的 master 也有，不是這次造成的** | 補路由並註冊 GET＋HEAD（`@app.get` 不含 HEAD，`curl -I` 與瀏覽器會拿到 405） | `e8d38b0` |

計畫預測會壞、實際上本來就好的：#2（`app.css` 早就有 `color: var(--accent)`，不只 `border-color`）、#6（`<svg>` 沒有吃掉 summary 的點擊）。

計畫本身錯的一條：Task 4 Step 4 寫 `curl -sI /favicon.ico → 200`，但那條路由當時根本不存在，是補了 `e8d38b0` 之後才成立。

新增 `scripts/check_frontend_ux.py`（12 項斷言，`cf946ff`）。這是這次真正的產出：
`pytest` 與 `check_js.sh` 都不載入瀏覽器，上面三個症狀全部只有這一層抓得到。
⚠ 唯讀（不 PUT 設定、不存遮罩、不按儲存）；前置條件不成立回 ⚠ SKIP 不回 ❌
（例如窄螢幕整條 `.stats-row` 是 `display:none`，`#video-max-btn` 根本不存在）。

驗證：正式機 12 項 × 1920x1080 與 390x844 皆退出碼 0、console 錯誤 0 筆；
`pytest` 590 passed（與基準相同）；`check_js.sh` 12 檔全過；服務 `active` 無 traceback。

部署方式與計畫不同：用 `git checkout -B frontend-ux-polish origin/frontend-ux-polish`
取代 cherry-pick，working tree 一樣但少一層新雜湊；驗收後 `reset --hard origin/master`。
