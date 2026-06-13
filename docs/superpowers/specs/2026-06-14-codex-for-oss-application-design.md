# Codex for Open Source 申請設計：pig-agri

**日期**：2026-06-14
**目標**：用 `esp8266good/pig-agri` 申請 OpenAI「Codex for Open Source」program，爭取 6 個月免費 ChatGPT Pro（含 Codex）。
**定位策略**：方案 3（混合）— 真實部署使命 ＋ 紀律維護硬證據 ＋ solo-maintainer 放大故事，並誠實面對弱點。

---

## 0. 現況與誠實前提

申請表（`openai.com/form/codex-for-oss/`）無法匿名抓取（HTTP 403，需登入 ChatGPT）。表單實際內容由使用者親自複製確認：

**前言**：program 定位在「維護工作流」— review PR、triage issue、發 release、維護安全與程式品質，目標是替廣泛使用的專案**減輕 coding 與 review 負擔**。

**三題，每題上限 500 字元**：
1. Why does this repository qualify?（提示：GitHub stars、月下載量、或為何對生態重要）
2. How will you use API credits for your project?
3. Anything else we should know?

**官方評估標準**（developers.openai.com/codex/codex-for-oss-terms，rolling review、OpenAI 全權裁量）：repository usage、ecosystem importance、evidence of active maintenance、role/permissions、program capacity。無寫死 star 門檻，但坊間常引用 ~1,000 stars。

**repo 現況查證**：

| 項目 | 現況 | 對申請 |
|---|---|---|
| 公開 | public | OK |
| Stars / Forks | **0 / 0** | 最大硬傷，無法造假 |
| License | 無 | 待補（A1） |
| Description / README | 無 | 待補（A2/A3） |
| 建立 | 2026-05-03（約 6 週） | 新、無社群史 |
| 第三方碼 | `ref/HybridSORT/` = **MIT**（內含 YOLOX/FastReID = Apache-2.0） | 可乾淨公開、無 GPL 傳染 |
| `.env` / 權重 | 未被 git 追蹤 | 無祕密外洩 |
| 活躍維護 | 100+ commits、~190 tests、spec-driven、詳盡除錯紀錄 | **唯一最強牌** |

**誠實底線（不可違反）**：
- ❌ 不捏造 stars / 下載量 / 「廣泛使用」/ 虛構社群或外部貢獻者。
- ✅ 誠實點出「新、stars 低」，火力集中於可驗證的：真實豬場部署 ＋ 紀律維護 ＋ solo-maintainer 放大。
- ⚠️ 即使做到最好，因 0 star / ecosystem importance 這關，**仍有很大機率被拒**。本設計是把命中率最大化，非保證。

---

## 1. Part A — Repo 補強

審查者必定會點進 repo；在沒有社群的情況下，repo 門面是「這是個認真開源專案」的唯一展示。

### A1. `LICENSE`（MIT）
- 頂層新增 **MIT** LICENSE（著作權人 = 使用者本名/帳號），與 vendored HybridSORT 的 MIT 一致。
- 保留 `ref/HybridSORT/LICENSE` 原檔不動。
- 在 README「Acknowledgements / Third-party」段標註：HybridSORT (MIT)、其內含 YOLOX (Apache-2.0)、FastReID (Apache-2.0)。

### A2. `README.md`（英文、專業、門面）
結構：
1. **一句話定位** + 一張架構圖（mermaid 或 ASCII）：
   `cameras → ZMQ → inference (YOLOX + HybridSORT-ReID) → Postgres tracking_logs → analysis/scheduler（活動量異常）→ 採血/健康告警`；另一條 `HLS live/VOD pipeline → Web UI`。
2. **Why it matters**：依活動量判斷豬隻是否需採血、改善動物福利、減少不必要採血、**真實豬場部署**、非商業研究（論文基礎）。
3. **Features**：即時 MOT 追蹤（ID 穩定性 → 採血判斷正確性）、HLS live + VOD 回放、活動量/體溫異常告警與通知中心、儲存韌性（故障防護/健康監控/夜間 ephemeral）、書籤/保留/timeline。
4. **Tech stack**：Python、FastAPI、PostgreSQL、ZMQ、ffmpeg/HLS、YOLOX、HybridSORT-ReID、uv、pytest。
5. **Status**：actively developed、pre-publication research、~190 passing tests、spec-driven development（指向 `docs/superpowers/specs`）。
6. **Roadmap**（直接對應使用者的 A/B/C/D，見下）。
7. **Acknowledgements / Third-party licenses**（A1）。
- CLAUDE.md **原樣保留**（使用者決策 D2(a)），不在 README 特別處理。

### A3. GitHub repo metadata（使用者於 GitHub 網頁設定，本 spec 提供文字）
- **Description**（一行，建議）：
  > Real-time computer-vision system for pig farms: tracks each pig's activity to flag animals needing veterinary blood tests. Deployed, non-commercial research.
- **Topics**：`computer-vision` `object-tracking` `multi-object-tracking` `animal-welfare` `precision-livestock-farming` `fastapi` `yolox` `agriculture` `hls`

### A4. `CONTRIBUTING.md` + `CODE_OF_CONDUCT.md`（輕量）
- 指南列為合格訊號。CONTRIBUTING：如何跑 `uv` 環境、跑 `pytest`、提 PR 流程、spec-driven 慣例。CODE_OF_CONDUCT：採用 Contributor Covenant 標準短版。

### A5. CI + badge（使用者決策 D3 = 要）
- 新增最小 GitHub Actions workflow：`uv sync` → `uv run pytest`。
- README 頂部加 CI 狀態 badge（命中前言「code quality」訊號）。
- **注意**：目前有 4 個測試因待辦 #12（`ZMQ_SOURCES` OS-env gap）失敗。CI 必須綠燈，否則 badge 反效果。處理方式（plan 階段二選一）：
  - (i) CI 於 workflow 設定 `ZMQ_SOURCES` 等必要 env，使該 4 測試可通過；或
  - (ii) 先修 #12（config 預設值被真實 .env 覆蓋的問題）再開 CI。
  - 傾向 (i)（範圍小、不動既有除錯議題）。

### A6. 門面整理（使用者決策：D2 保留 CLAUDE.md 原樣）
- `_phist.txt`（76KB dump）：建議 `.gitignore` 並從追蹤移除（純雜訊，傷觀感）。
- `old/HybridSORT_old/`：保留（CLAUDE.md 註明「舊版不要改」），README 不主打；可於 .gitignore 評估，但因已追蹤且體積大，移除與否列為 plan 階段小決策。
- `_docs/`：plan 階段檢視內容後決定保留或整併進 `docs/`。

---

## 2. Part B — 三題申請文案（每題 ≤500 字元，已驗證字數）

### Q1. Why does this repository qualify?（490 字元）
> pig-agri is a real-time computer-vision system deployed on a working pig farm: it tracks each pig's activity to flag animals needing veterinary blood tests, improving welfare and cutting unneeded draws. It's non-commercial research (my thesis basis). As sole maintainer I keep ~190 passing tests, practice spec-driven development, and document systematic debugging across 100+ commits. The repo is new so stars are low, but this is actively maintained, deployed production code, not a demo.

**中文對照**：pig-agri 是部署在真實運作豬場的即時電腦視覺系統，追蹤每頭豬的活動量以標記需獸醫採血的個體，改善福利並減少不必要採血。非商業研究（論文基礎）。身為唯一維護者，我維持 ~190 個通過測試、spec-driven 開發、100+ commits 的系統化除錯紀錄。Repo 很新所以 stars 低，但這是持續維護、已部署的 production 程式碼，不是 demo。

### Q2. How will you use API credits for your project?（467 字元）
> As a solo maintainer, Codex would let one person responsibly sustain a full-stack real-time CV system: expanding the test suite and CI, refactoring for clarity, and reviewing my own diffs for code quality and security. It would accelerate features (multi-camera, stronger ReID, automated blood-draw reports) and help me systematically verify and fix the many long-running stability issues in my backlog, freeing scarce time for the underlying animal-welfare research.

**中文對照**：身為唯一維護者，Codex 能讓一個人負責任地撐起全端即時 CV 系統：擴充測試與 CI、為清晰度重構、為程式品質與安全自審 diff。它能加速新功能（多攝影機、更強 ReID、自動採血報表），並協助我系統化驗證與修復 backlog 中眾多長期穩定性問題，把稀缺時間留給底層的動物福利研究。

**設計說明**：刻意對齊前言的「維護工作流／code quality／review」，A/B/C/D 全涵蓋（A=test/CI/refactor、B=features、C=stability、D=research time）。

### Q3. Anything else we should know?（419 字元）
> I'm one researcher, not a funded team, building this for animal welfare in agriculture, an underserved domain for AI tooling. The repo is young but the system is real and running on an actual farm. Codex wouldn't polish a popular library; it would let a single person maintain and document production code while completing the research it supports. Happy to share deployment details, demo footage, or a maintainer call.

**中文對照**：我是一名研究者、不是有資金的團隊，為農業領域的動物福利打造這套系統——這是 AI 工具普遍忽視的領域。Repo 很年輕，但系統是真的、跑在實際豬場上。Codex 不會是替熱門函式庫拋光；而是讓一個人能維護並文件化 production 程式碼，同時完成它所支撐的研究。樂意提供部署細節、demo 影片或維護者通話。

**設計說明**：把弱點（新、無資金、無社群）轉成「最早期支持有真實軌跡的專案」，並主動提出可驗證證據（部署細節、demo、通話）降低審查者疑慮。

### 替代語氣（plan 階段可選，本 spec 先記方向）
- Q1 替代：更強調「real deployment + welfare impact」開頭，弱化 tests。
- Q2 替代：更聚焦單一旗艦工作流（如「自審 PR/diff 的 review load」）以呼應前言。

---

## 3. 交付順序（給 writing-plans 的輸入）

1. A1 LICENSE（MIT）
2. A6 門面整理（移除 `_phist.txt` 追蹤；`_docs`/`old` 決策）
3. A2 README（含架構圖、roadmap、third-party 致謝）
4. A4 CONTRIBUTING + CODE_OF_CONDUCT
5. A5 CI workflow + badge（含 #12 env 處理）
6. A3 metadata 文字交付（使用者於 GitHub 手動設定）
7. Part B 三題定稿（字數已驗證；如需替代版一併產出）
8. 使用者送出申請

---

## 4. 風險與限制
- **無法克服的**：0 stars / ecosystem importance 是 program 核心評分項，文案與 README 無法製造社群與使用量。
- **可控的**：授權、README、CI、metadata、文案品質 — 本設計全面補齊。
- **本人需手動執行**：GitHub description/topics 設定、CI secrets（若需）、最終送出申請、維護者身分驗證。
- 結論：本設計把「可控變數」做到滿，最大化逐案審查時被正向注意的機率，但**不保證錄取**。
