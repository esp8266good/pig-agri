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
