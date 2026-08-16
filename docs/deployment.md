# 部署到一台新機器

從一台乾淨的機器把整套 pig-agri 跑起來。內容是 2026-08-17 實際把服務從
`lazoark` 搬到 `ed716-pig` 時走過一遍的流程，包含當時踩到的坑。

`service-readme.md` 講的是「服務跑起來之後怎麼維運」（開機自啟動、日常操作、
故障排除）。這份講的是「從零到服務跑起來」。兩份接在一起就是完整流程。

---

## 一、先確認機器夠用

| 項目 | 最低 | 實測參考 |
|---|---|---|
| GPU | NVIDIA，8GB VRAM | RTX 3070 8GB，4 台相機實際只用 **798 MB** |
| CPU | 8 核 | 12 核夠用；`MOT_WORKER_THREADS` 設核心數的一半到三分之二 |
| RAM | 16GB | 31GB 機器上實際用 4GB |
| 系統碟 | 100GB | 程式 + venv 約 8GB（光 torch 就 3GB）|
| 錄影碟 | **另外一顆** | 見下面的容量估算 |
| OS | Ubuntu 24.04 | 其他發行版沒試過 |

錄影容量的實測值：**4 台相機、6 條串流、每天錄 06:30–17:00，21 天用掉 208GB**，
大約 10 GB/天。這個數字會隨相機數、fps、解析度線性變化，自己按比例推。

**錄影一定要獨立一顆碟。** 不是為了效能，是為了 `storage_monitor.py` 的低空間
保護能真的保護到東西——它跟系統碟共用的話，系統碟被別的東西吃掉會直接影響錄影。

### GPU 不是瓶頸

從 RTX 4070 12GB 換到 RTX 3070 8GB 之前，我們擔心 VRAM 不夠。實測 YOLOX +
FastReID 兩個模型常駐只吃 798 MB。**CPU 核心數才是要注意的**：
`MOT_WORKER_THREADS` 如果設得跟核心數一樣多，會跟 ffmpeg 搶核心。

---

## 二、裝系統套件

```bash
sudo apt update
sudo apt install -y ffmpeg tmux docker.io docker-compose-v2 postgresql-client
sudo usermod -aG docker "$USER"      # 要重新登入才生效
sudo loginctl enable-linger "$USER"  # 沒登入時 systemd user service 也要活著
```

四個都不能少：

- `ffmpeg` — HLS writer 的核心，沒有它錄影跟直播整條不會動
- `tmux` — 開機自啟動的監控腳本靠它
- `docker.io` + **`docker-compose-v2`** — PostgreSQL 走 `docker-compose.yml`
- `postgresql-client` — `scripts/` 底下的維運腳本要 `psql`

> ⚠ Ubuntu 的 `docker.io` **不含** compose plugin，`docker-compose-v2` 要另外裝。
> 少裝的話 `docker compose up -d` 會回 `docker: unknown command`。

> ⚠ `enable-linger` 忘記做的話，服務只有在你 ssh 登入時才活著，登出就死。

裝 `uv`（user-level，不用 sudo）：

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

NVIDIA driver 要自己先裝好，`nvidia-smi` 跑得出來為準。**不需要**
nvidia-container-toolkit——推論跑在 host 上，只有 PostgreSQL 在容器裡。
前端是零 build 的 ES modules，**也不需要 node**。

---

## 三、拿到 git 給不了的四樣東西

`git clone` 之後還缺這些，全都被 `.gitignore` 排除：

```bash
git clone https://github.com/esp8266good/pig-agri ~/pig-agri
```

| 缺什麼 | 大小 | 怎麼拿 |
|---|---|---|
| `ref/HybridSORT/` | 1.1 GB | 從既有機器 `rsync -a ref/ 新機:~/pig-agri/ref/` |
| `uv.lock` | 500 KB | 同上。沒有它 `uv sync` 會自己解版本，可能跟現役環境不同 |
| `.env` | 2 KB | 從既有機器複製後改路徑（見下一節）|
| 模型權重 | 1.1 GB | 在 `ref/HybridSORT/pretrained/`，跟著 `ref/` 一起來 |

權重是 `best_ckpt.pth.tar`（793 MB，YOLOX 偵測器）與 `model_0054.pth`
（312 MB，FastReID）。`ref/` 裡有 `logs/` 之類的雜物，rsync 時可以排除：

```bash
rsync -a --exclude 'logs/' --exclude '__pycache__/' --exclude '.git/' \
      ref/ 新機:~/pig-agri/ref/
rsync -a uv.lock 新機:~/pig-agri/uv.lock
```

### 建 venv

```bash
cd ~/pig-agri
uv sync --extra dev          # torch 約 3GB，慢
uv run python -c "import torch; print(torch.__version__, torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

最後一行要印出 `True` 跟你的顯卡名字才算過。

---

## 四、`.env`：從別台複製過來一定要改的欄位

```bash
cp .env.example .env      # 或從既有機器複製
```

| 欄位 | 為什麼一定要改 |
|---|---|
| `HLS_BASE_DIR` | **別台的路徑在這台不存在**，錄影會直接壞。指到這台的錄影碟 |
| `MOT_WORKER_THREADS` | 按這台的核心數重設（核心數的一半到三分之二）|
| `DATABASE_URL` | 用 `docker-compose.yml` 的話維持 `localhost:15432` |
| `ZMQ_SOURCES` | 相機位址。格式 `name:host:port:src_topic:label`，分號分隔 |
| `AUTH_*` | 見「六、對外開放」 |

`MODEL_WEIGHTS` / `MODEL_CONFIG_PATH` 是相對路徑，`ref/` 就位之後不用改。

`.env.example` 有完整欄位清單與註解，照著對一遍。

---

## 五、網路：相機連得到嗎

相機透過 ZMQ 推 JPEG。我們的部署走 Tailscale（相機在別的網段）：

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up          # 會吐一條 URL 要瀏覽器授權
```

**裝完一定要實測連通性**，不要假設：

```bash
for h in <相機1> <相機2>; do nc -vz "$h" 5555; done
```

連不到的話後面全部白做——服務會起來、網頁會通、就是一幀資料都沒有。

---

## 六、起服務

```bash
cd ~/pig-agri
docker compose up -d                    # PostgreSQL，schema 由 sql/init.sql 自動建
uv run pytest -p no:cacheprovider -q    # 應為 380 passed, 0 failed
uv run uvicorn main:app --host 127.0.0.1 --port 5005   # 前景跑，看 log
```

前景跑的時候要在 log 裡看到這三種行：

```
[inference] active=True 60s | cam_xx: fresh=91(1.5/s) stale=0 ageout=0 ...
[rpi5_dual] 60s: recv=541 drop_stale=0 rate=9.0/s age(ms) min=5 med=20 max=278
Started HLS stream cam_xx/rgb → ... (mode=ephemeral)
```

- `active=False` 不一定是壞掉——先看是不是落在 `gpu_off` 排程時段內
- `mode=ephemeral` 也不一定是壞掉——夜間排程時段本來就不落地錄影

### 對外開放：一個要想清楚的決定

預設 `--host 127.0.0.1`，只有本機連得到，前面要有反向代理。這是最安全的擺法。

如果反向代理在**另一台機器**上，就得綁到對外介面。這時候有三種選擇，各有代價：

| 做法 | 代價 |
|---|---|
| `--host 0.0.0.0` + `AUTH_ENABLED=true` | 整個網段連得到，但要登入。開機時不依賴任何網路介面就位 |
| `--host <固定IP>` | 不對其他介面曝露，但**開機時該介面還沒起來會 bind 失敗、陷入重啟迴圈**（Tailscale IP 特別容易中）|
| `--host 0.0.0.0`，不開登入 | 最快，但 dashboard 對整個網段無登入開放。只有你完全信任該網段時才可以 |

綁 `0.0.0.0` 卻不開登入的話，可以用防火牆只放行代理主機：

```bash
sudo ufw allow 22/tcp                                   # 這行不能省，否則 ssh 一起被擋
sudo ufw allow from <代理主機IP> to any port 5005 proto tcp
sudo ufw enable
```

開登入的話：

```bash
uv run python scripts/make_password_hash.py    # 互動式，印出可貼進 .env 的區塊
```

⚠ 沒有 TLS 之前 `AUTH_COOKIE_SECURE` 要設 `false`，不然 cookie 根本不會送出。
⚠ 前面沒有反向代理時 `AUTH_TRUST_FORWARDED_FOR` 要設 `false`，不然任何人都能
偽造 `X-Forwarded-For` 繞過登入失敗的節流。

---

## 七、開機自啟動

完整步驟在 [`service-readme.md`](../service-readme.md)。摘要：

1. 把監控腳本放到 `~/bin/pig-agri-tmux.sh`，改掉裡面的 `WORKING_DIRECTORY`
2. 把 unit 檔放到 `~/.config/systemd/user/pig-agri-tmux.service`，改掉 `PATH` 與 `ExecStart`
3. `systemctl --user daemon-reload && systemctl --user enable --now pig-agri-tmux.service`

**一定要真的重開機驗一次。** `enabled` 加 `linger=yes` 看起來對，不代表開機後
真的會起來。我們實測是重開機後 6 秒回來。

重開機時會看到一次啟動失敗：uvicorn 比 PostgreSQL 早起來，`ConnectionRefusedError`
之後退出，監控腳本 10 秒後重試就成功了。這是預期行為（自癒機制在運作），不是故障。
不想看到那則 ntfy 通知的話，unit 加 `After=docker.service`。

---

## 八、驗收清單

一項一項跑過，不要跳：

```bash
# 服務
systemctl --user is-enabled pig-agri-tmux.service   # enabled
systemctl --user is-active  pig-agri-tmux.service   # active
loginctl show-user "$USER" -p Linger --value        # yes

# 測試
uv run pytest -p no:cacheprovider -q                # 380 passed

# 端點（不帶任何 cookie）
for p in /health /cameras /auth/status /storage/health /alerts/active /settings; do
  printf '%-16s ' "$p"; curl -s -o /dev/null -w '%{http_code}\n' "http://<本機IP>:5005$p"
done

# 有沒有真的在收資料
curl -s http://<本機IP>:5005/cameras

# 回放（挑一個有錄到東西的整點）
curl -s "http://<本機IP>:5005/stream/<相機>/timeline?start_ts=<起>&end_ts=<迄>"
curl -s "http://<本機IP>:5005/stream/<相機>/vod?start=<整點epoch>&end=<整點+3600>"
```

VOD 那條要吐出 `#EXTM3U` 開頭、帶 `#EXT-X-PROGRAM-DATE-TIME` 的 playlist，
而且裡面指到的 `.ts` 抓得下來，才算回放真的能用。

> API 的查詢參數名不一致：`/stream/*/timeline` 用 `start_ts` / `end_ts`，
> `/stream/*/vod` 用 `start` / `end`。不是筆誤。

最後看一眼 `GET /storage/health`：`recording_state` 要是 `ok`，
`recording_free_gb` 要在 `storage_min_free_gb` 之上。

---

## 九、從既有機器搬資料過來

`scripts/migrate_merge.sh` 專門做這件事，設計成可以分很多次跑、中斷了重跑會接續。
完整說明在腳本開頭的註解，這裡只講順序：

```bash
./scripts/migrate_merge.sh status        # 只看，不動任何東西
BOUNDARY=now ./scripts/migrate_merge.sh prep
DAYS=21 ./scripts/migrate_merge.sh db-all
./scripts/migrate_merge.sh small
./scripts/migrate_merge.sh settings      # 要不要對齊設定，自己決定
DAYS=21 ./scripts/migrate_merge.sh hls
./scripts/migrate_merge.sh verify
```

`DAYS=21` 是先搬近期讓服務轉移，完整歷史之後再補。要一次搬完就不帶 `DAYS`。
全部搬完、確定不再補歷史了，才跑 `finish` 清暫存表。

**`object_id` 是這件事最危險的地方。** 兩台機器的 `object_id` 是各自獨立的號碼
空間——A 機的 3 號豬跟 B 機的 3 號豬沒有任何關係。同一台相機、同一段時間如果
兩邊都有資料，融合之後 `analysis/scheduler.py` 會把兩隻不同的豬的 bbox 中心點
串成同一條軌跡，活動量算成垃圾，而且**看起來完全正常**。腳本預設會擋，
`ALLOW_OVERLAP=1` 才能硬跑。

搬歷史影像之前，**先確認 `hls_retention_days` 夠大**。retention 掃描是按檔案
時間算的，把三個月前的影像搬進一台設 40 天的機器，它會在一小時內被刪光。

---

## 十、實際踩過的坑

| 症狀 | 根因 | 怎麼避開 |
|---|---|---|
| `docker compose` 說 unknown command | `docker.io` 不含 compose plugin | 另裝 `docker-compose-v2` |
| 服務跑得起來但一幀都沒有 | 相機網路不通 | 裝完 Tailscale 一定要 `nc -vz` 實測 |
| 錄影目錄不存在、寫入失敗 | `.env` 沿用了別台的 `HLS_BASE_DIR` | 複製 `.env` 後逐欄檢查路徑 |
| 測試從 0 失敗變成 49 失敗 | 部署的 `AUTH_ENABLED=true` 汙染端點測試 | 已由 `tests/conftest.py` 修掉；自己加測試時注意別再讀真實 `.env` |
| 開機後服務沒起來 | 忘記 `enable-linger` | `loginctl show-user $USER -p Linger` 要是 `yes` |
| 綁 Tailscale IP 後開機陷入重啟迴圈 | 開機時 `tailscale0` 還沒起來 | 綁 `0.0.0.0`，或 unit 加 `After=tailscaled.service` |
| `ephemeral_state` 永遠 `degraded` | `storage_min_free_gb` 同時套用在錄影碟與 `/dev/shm` tmpfs，tmpfs 永遠達不到 | 目前無解，行為上無影響（`degraded` 仍可寫、不丟幀、不發告警），知道就好 |
| `DROP TABLE tracking_logs_old` 被擋，照 hint 加 `CASCADE` 會炸 | sequence 的擁有者掛在舊表，但它是現役表 `id` 的 default 來源 | 先 `ALTER SEQUENCE tracking_logs_id_seq OWNED BY tracking_logs.id;` 再 DROP |

---

## 十一、要包裝成產品的話還缺什麼

這份流程目前還是「有人照著做」的等級。要變成可交付的產品，缺的是：

- **權重的取得方式**。現在靠 rsync 別台機器，等於部署依賴一台既有機器。要有
  可下載的 artifact（或把 `ref/HybridSORT` 收進 submodule / 打成 wheel）。
- **`.env` 的產生**。現在靠人工複製改路徑，容易漏。應該做成互動式產生器，
  或至少寫一支檢查腳本，開機前驗證每個路徑都存在、每個 ZMQ 來源都連得到。
- **一鍵安裝**。第二～七節其實可以是一支 `install.sh`，把 apt、uv、venv、
  systemd unit、路徑改寫全部包起來。
- **錄影碟與 ephemeral 碟分開的空間門檻**。同一個 `storage_min_free_gb` 套在
  468GB 的碟和 16GB 的 tmpfs 上本來就不合理，指示燈永遠紅著就等於沒有指示燈。
- **升級路徑**。目前沒有定義「既有部署怎麼升到新版」，schema 變更也沒有 migration
  機制（`sql/init.sql` 只有 `CREATE TABLE IF NOT EXISTS`）。
