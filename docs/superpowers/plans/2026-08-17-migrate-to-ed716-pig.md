# 遷移到 ed716-pig（`ssh pig-agri:~/lobby/pig-agri`）

日期：2026-08-17。目標：把 pig-agri 服務從目前這台（`lazoark`，
`/home/lazoark/OneDrive/Curriculum/pig-agri`）搬到 `ed716-pig`（使用者 `chen`，
`/home/chen/lobby/pig-agri`）。本輪只做準備，不切換。

## 兩台機器的差異

| | 現行（lazoark） | 目標（ed716-pig） |
|---|---|---|
| GPU | RTX 4070 12GB | RTX 3070 **8GB** ⚠ |
| CPU / RAM | 24 核 / 62GB | 12 核 / 31GB ⚠ |
| NVIDIA driver | 595.71.05（CUDA 13.2） | 595.71.05（CUDA 13.2）✅ 相同 |
| 系統碟 | 916G，已用 87% | 468G，剩 424G |
| 錄影碟 | 外掛 1TB HDD，HLS 已佔 **378G** | 沒有第二顆碟 ⚠ |
| 網路 | Tailscale `nycu716rgbt@`，100.93.143.37 | **沒裝 Tailscale** ⚠ |

`mot_worker_threads` 現在是 12，等於現行機器的一半核心數；搬到 12 核的機器上要
往下調（建議 6~8），不然 CPU 執行緒池會跟 ffmpeg 搶核心。

## 已經做好的（2026-08-17 這輪）

1. ✅ 遠端已有 repo clone，`master` 對齊 `c3bcb46`，remote 指向 GitHub。
2. ✅ 裝好 `uv` 0.12.5（`/home/chen/.local/bin/uv`，user-level，無 sudo）。
3. ✅ `ref/`（1.1G，含 `pretrained/best_ckpt.pth.tar` 792MB 與
   `model_0054.pth` 312MB）已 rsync 過去。這包被 `.gitignore` 排除，clone 拿不到。
4. ✅ `uv.lock`（也被 gitignore）已複製過去。
5. ✅ `uv sync --extra dev` 建好 venv。`torch 2.11.0+cu130`，
   `torch.cuda.is_available() == True`，抓到 `NVIDIA GeForce RTX 3070`。
6. ✅ 用 `.env.example` 當暫時設定跑了一次全套測試：**374 passed, 4 failed**。
   4 個失敗全是 `tests/test_database.py` 連不到 `127.0.0.1:15432`（遠端還沒有
   postgres），不是程式回歸。測完那份暫時的 `.env` 已刪掉，免得切換當天被誤用。

7. ✅ `ffmpeg` / `tmux` / `docker.io` / `postgresql-client` 已裝，`chen` 進了 docker
   群組，`Linger=yes`，`.env` 也已複製過去。
8. ✅ 用 throwaway postgres container 跑完整套測試：**378 passed, 0 failed**。
   container 與 volume 跑完就刪掉了。
9. ✅ 已把改好路徑的啟動腳本放到 `~/bin/pig-agri-tmux.sh`（`bash -n` 通過）；
   systemd unit 放在 `~/lobby/pig-agri-deploy/pig-agri-tmux.service`，**故意不裝進
   `~/.config/systemd/user/`**，避免現在就被啟動。切換當天再 `cp` + `enable --now`。

10. ✅ Tailscale 已加入 tailnet，`ed716-pig` = `100.104.167.102`；兩台相機
    （`100.77.97.67`、`100.67.51.73`）的 5555 都通。
11. ✅ postgres 走 `docker compose` 起來，`init.sql` 建好 5 張表。
12. ✅ **服務已經在遠端跑起來，開機自啟動也裝好了**（`~/.config/systemd/user/
    pig-agri-tmux.service`，`enabled` + `active`，`Linger=yes`）。四路 ZMQ 都在
    收，`fresh=91(1.5/s)`、`drop_stale=0`；HLS 走 ephemeral（凌晨 1 點在
    17:00–06:30 的休息時段內，正確行為）。
13. ✅ 登入已開啟（`AUTH_ENABLED=true`，帳號 `pig`）。`POST /auth/login` 回 200，
    帶 cookie 打 `/cameras` 拿到四台相機；不帶 cookie 一律 401。

### 這台跟舊機不一樣的地方（別照抄舊設定）

| 設定 | 值 | 為什麼 |
|---|---|---|
| `--host` | `0.0.0.0`（舊機是 `127.0.0.1`） | 這台前面還沒有 Traefik，要讓 LAN 與 tailnet 都連得到。只綁 tailscale IP 的話開機時 `tailscale0` 還沒起來會 bind 失敗 |
| `AUTH_ENABLED` | `true` | 直接對 LAN 開就一定要有登入。舊機靠 Traefik 擋在前面 |
| `AUTH_COOKIE_SECURE` | `false` | 還沒有 TLS。**接上 Traefik 後要改回 `true`** |
| `AUTH_TRUST_FORWARDED_FOR` | `false` | 前面沒有 proxy，信 `X-Forwarded-For` 等於讓人偽造來源 IP 繞過登入節流 |
| `HLS_BASE_DIR` | `/home/chen/lobby/pig-agri-data/hls` | 這台沒有 1TB HDD，錄影從零開始。之後加硬碟再換路徑或掛 symlink |
| `MOT_WORKER_THREADS` | `8`（舊機 12） | 這台只有 12 核，留 headroom 給 ffmpeg |

`/static/index.html` 是刻意公開的（登入頁本身就是前端 app 的一部分，
`auth_middleware.py` 的註解有寫）；所有資料端點未登入都是 401。

14. ✅ 重開機驗過了：01:22 重開，服務 01:22:06 自己回來，postgres 靠
    `restart: unless-stopped` 也回來了。
15. ✅ ntfy 通知標題掛上主機名（`[ed716-pig] …`）。掛在 `ntfy_notifier.notify`
    這個唯一出口，`~/bin/pig-agri-tmux.sh` 的 Title header 兩台機器也都改了。
16. ✅ 舊機的 `tracking_logs_old`（126M 列、22GB 備份表）已 DROP，空間回收 22GB。
    **注意**：不能直接 `DROP ... CASCADE`——`tracking_logs_id_seq` 的擁有者還掛在
    舊表上，但它同時是現役 `tracking_logs.id` 的 default 來源，CASCADE 會把
    sequence 一起刪掉、現役表從此插不進資料。要先
    `ALTER SEQUENCE tracking_logs_id_seq OWNED BY tracking_logs.id;` 再 DROP。
17. ✅ 融合腳本 `scripts/migrate_merge.sh` 寫好並實測過（見下）。

## 資料融合：`scripts/migrate_merge.sh`

在舊機上跑，把舊機的資料融合進新機。每個子命令都能重複執行，中斷了重跑會接續。

| 子命令 | 做什麼 |
|---|---|
| `status` | 只看不動：兩邊範圍、有沒有時間重疊、設定差異、影像空間夠不夠 |
| `prep` | 新機建暫存表、去重索引，並**拍下「新機自己的資料從哪裡開始」的快照** |
| `db-day <日期>` / `db-all` | 搬 `tracking_logs`，逐日、可中斷 |
| `small` | `health_alerts` / `pig_notes` / `saved_segments` |
| `hls` | 影像，一個小時目錄一個單位 |
| `verify` | 逐日比對兩邊筆數 |
| `finish` | 收尾：丟暫存表、對齊 sequence |

實測結果（2026-08-17）：搬 2026-05-05 與 05-06 兩天共 438 萬列，各約 27 秒
（照這個速度全部 1.09 億列約 25 分鐘）。小表 47279 筆 `health_alerts` 與 9 筆
`saved_segments` 已搬完，重跑不會重複。影像用 4 個小時目錄實測 staging→落地
都正常，測完已清掉（影像照你說的先不搬）。

### 設計上踩到、也修掉的三個坑

**1. 重疊檢查會被自己剛搬進去的資料擋住。**
一開始拿「新機目前最早的一筆」當基準，搬完第一天之後，第二天就被自己寫的資料
判定為重疊。改成 `prep` 時把邊界拍成快照存在 `migrate_own_boundary` 表，之後
一律跟快照比。**所以 `prep` 一定要在第一次 `db-day` 之前跑**；如果新機的
`tracking_logs` 還是空的（或你確定裡面全是搬進來的），用 `BOUNDARY=now prep`。

**2. rsync 中斷會留下「看起來存在」的半個小時目錄。**
那個小時之後會被永久跳過、缺片而且沒人會發現。改成先落到 `.incoming`，整包
傳完才 `mv` 進正式位置。正式位置永遠只有完整的小時目錄。

**3. 一個 `hls` 指令就能把新機的碟塞爆。**
378G 進 414G 只剩 36G，低於 `storage_min_free_gb=100`，錄影會當場切到 ephemeral
並開始發告警。加了空間煞車：預估搬完會低於門檻就擋下來，要嘛 `HOURS_LIMIT=N`
分批、要嘛加硬碟改 `REMOTE_HLS`、要嘛 `FORCE=1`。

### 為什麼 `object_id` 是這整件事最危險的地方

兩台機器的 `object_id` 是各自獨立的號碼空間——舊機的 3 號豬跟新機的 3 號豬沒有
任何關係。同一台相機、同一段時間如果兩邊都有資料，融合之後
`analysis/scheduler.py` 會把兩隻不同的豬的 bbox 中心點串成同一條軌跡，活動量
直接算成垃圾，而且看起來完全正常。所以預設只搬「早於新機邊界」的範圍，越界
就擋，要硬跑得自己加 `ALLOW_OVERLAP=1`。

`tracking_logs.id` 不搬（舊機已經跑到一億八千多萬），一律由新機重新配號，
去重靠自然鍵 `(camera_id, frame_id, object_id, timestamp)`——跟
`dedup_tracking_logs.sql` 同一把鍵。

`user_settings` 預設不搬：兩台的 ntfy topic（pig / swine）、保留天數、錄影排程
本來就該不一樣。`status` 會把差異列出來讓你自己判斷。

## 還沒做的：需要 sudo（`chen` 有 sudo 但要密碼，我沒有）

遠端這四樣都不在：

```bash
sudo apt update
sudo apt install -y ffmpeg tmux docker.io postgresql-client
sudo usermod -aG docker chen        # 要重新登入才生效
```

- `ffmpeg` — HLS writer 的核心，沒有它整條錄影/直播都不會動。
- `tmux` — `pig-agri-tmux.sh` 監控腳本靠它。
- `docker.io` — postgres 16 走 `docker-compose.yml`。
- `postgresql-client` — `scripts/dedup_tracking_logs.sh` 要 `psql`。

另外要讓 systemd user service 在沒登入時也活著：

```bash
sudo loginctl enable-linger chen    # 目前 Linger=no
```

## 還沒做的：只有你能做的（帶密的東西）

1. **`.env`** — 被權限設定擋住，我讀不到也複製不了。請自己 scp 過去，
   並在遠端改掉這幾個路徑：
   - `HLS_BASE_DIR` → 遠端沒有 1TB HDD，要重新決定放哪（見下面「未決事項」）
   - `MODEL_WEIGHTS` / `MODEL_CONFIG_PATH` → 相對路徑不用改，`ref/` 已就位
   - `MOT_WORKER_THREADS` → 12 改成 6~8
2. **Tailscale** — 相機是 `100.77.97.67`（rpi-rgbt-edge-01）與 `100.67.51.73`（nycu），
   都在 tailnet `nycu716rgbt@` 裡。遠端沒裝 Tailscale，裝完要加入同一個 tailnet
   才連得到 ZMQ source：
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   sudo tailscale up            # 需要瀏覽器授權
   ```
   驗證：`nc -vz 100.77.97.67 5555`
3. **DB 資料** — 要不要把歷史 tracking / 體溫資料一起搬？搬的話：
   ```bash
   # 舊機
   docker exec pig-agri-postgres-1 pg_dump -U pig pig_monitoring | gzip > /tmp/pig.sql.gz
   scp /tmp/pig.sql.gz pig-agri:/tmp/
   # 新機（compose 起來、init.sql 跑完之後）
   gunzip -c /tmp/pig.sql.gz | docker exec -i pig-agri-postgres-1 psql -U pig pig_monitoring
   ```
   不搬的話 `sql/init.sql` 會自動建空 schema，但活動量基準要重新累積。
4. **反向代理** — 現行前面有 Traefik（本機 :80/:443 → `127.0.0.1:5005`）。
   遠端服務起來後，Traefik 的 swine service 要改指向新機器，這步等切換當天再做。

## 未決事項（要你決定，會改變後面怎麼做）

### 1. 378G 的 HLS 錄影要不要搬？

遠端只有一顆 468G 的系統碟，剩 424G。整包搬過去等於把系統碟塞到 90% 以上，
而且 `storage_monitor.py` 的低空間告警會立刻叫。三個選項：

| 做法 | 代價 |
|---|---|
| 加一顆碟給遠端當錄影碟 | 要買/搬硬碟，但架構跟現行一致，最省事 |
| 只搬最近 N 天，舊的留在舊機 | 回放看不到舊資料；`HLS_RETENTION_DAYS` 要重設 |
| 不搬，新機從零開始錄 | 最乾淨，但歷史回放全斷 |

### 2. RTX 3070 8GB 夠不夠？

現行 4070 上整台 GPU 用到 5.4GB（含其他程式）。3070 少 4GB，而且 YOLOX + FastReID
兩個模型都要常駐。這個沒有紙上答案，**要在遠端實跑一次量 `nvidia-smi`**。
如果爆，先降 `hls_target_fps` 或把 ReID 的 batch 縮小。

## 切換當天的順序（先寫下來，別當天現想）

1. 舊機 `systemctl --user stop pig-agri-tmux.service`（正常停機，不會誤發 ntfy）
2. 舊機 `pg_dump` → 新機 restore
3. 新機 `docker compose up -d` 起 postgres
4. 新機手動 `uv run uvicorn main:app --port 5005 --host 127.0.0.1` 前景跑，看 log
5. 確認 bbox / 錄影 / 活動量都正常後，才裝 systemd unit 並 `enable --now`
6. Traefik 改指向新機
7. 舊機 `systemctl --user disable pig-agri-tmux.service`

新機的 `~/bin/pig-agri-tmux.sh` 與 systemd unit 要改的地方只有兩處：
`WORKING_DIRECTORY=/home/chen/lobby/pig-agri`，以及 unit 裡的
`Environment=PATH=/home/chen/.local/bin:...` 與 `ExecStart=/home/chen/bin/...`。
`service-readme.md` 那份手冊照抄即可，路徑換掉。

## 遠端會缺、但不影響服務的東西

`CLAUDE.md`、`.claude/`、`tools/`、`old/`、`_docs/`、`_phist.txt` 都被 gitignore，
遠端沒有。`old/` 是舊版 HybridSORT，不用搬；`tools/`（88K）跟 `CLAUDE.md` 如果要在
新機上繼續用 Claude Code 開發，順手 rsync 過去就好。
