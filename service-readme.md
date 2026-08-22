# pig-agri 開機自啟動與維運手冊

本手冊用 **systemd user service** 在開機後啟動一個叫 `pig-agri` 的 tmux session，
裡面跑 Uvicorn。Uvicorn 若非正常停機而結束，監控腳本會發 ntfy 通知，等幾秒後自動重啟。

第一次把系統裝到一台新機器上，先看 [`docs/deployment.md`](docs/deployment.md)——
那份講的是「從零到服務跑起來」，這份接手講「跑起來之後怎麼維運」。

## 開始之前：設定三個變數

底下所有指令都用這三個變數，先在你的 shell 裡設好，整份手冊就能直接複製貼上：

```bash
export PIG_DIR="$HOME/pig-agri"        # 專案目錄（換成你 clone 的位置）
export PIG_BIN="$HOME/bin"             # 監控腳本要放哪
export PIG_HOST="127.0.0.1"            # Uvicorn 綁哪個位址，見下面「綁定位址」
```

> 這三個變數只在你目前這個 shell 有效。重開終端機要重設，或者寫進 `~/.bashrc`。

### 綁定位址怎麼選

| `PIG_HOST` | 什麼時候用 | 代價 |
|---|---|---|
| `127.0.0.1` | 反向代理跟服務在**同一台**（最安全，預設）| 其他機器連不到 |
| `0.0.0.0` | 反向代理在**別台**機器上 | 整個網段都連得到，要嘛開登入、要嘛用防火牆只放行代理主機 |
| 某個固定 IP | 想只曝露單一介面 | **開機時該介面還沒起來會 bind 失敗、陷入重啟迴圈**（Tailscale IP 特別容易中）|

綁 `0.0.0.0` 又不開登入的話，等於把 dashboard 對整個網段無條件開放。
登入的啟用步驟在後面「啟用帳號密碼登入」。

## 完成後的行為

- 開機後自動建立 tmux session：`pig-agri`
- 在 `$PIG_DIR` 中執行：

```bash
uv run uvicorn main:app \
  --log-level info \
  --port 5005 \
  --host "$PIG_HOST" \
  --proxy-headers \
  --forwarded-allow-ips=127.0.0.1 \
  --reload-exclude "tools/*"
```

- Uvicorn 意外結束時發布通知到 ntfy，標題會帶主機名（例如
  `[ed716-pig] pig-agri service alert`），這樣多台機器共用同一個 topic 也分得出來
- 異常後等 10 秒再重啟，避免快速失敗造成重啟風暴
- `tmux attach -t pig-agri` 看即時輸出
- 正常執行 `systemctl --user stop pig-agri-tmux.service` 不會誤發異常通知，也不會自動重啟
- tmux session 若被非預期刪除，systemd 會在 10 秒後重建

> 沒有 `--reload`：這是正式服務，改 `.py` 要自己重啟才生效。改 `static/` 底下的
> 檔案會直接生效，重新整理瀏覽器即可。

---

## 一次性安裝

### 1. 確認必要工具

```bash
command -v tmux curl uv
ls -ld "$PIG_DIR"
```

`uv` 通常在 `~/.local/bin/uv`。如果不在，等一下建 service 的時候要把它的目錄
加進 `Environment=PATH=...`——systemd user service 不會讀你的 `~/.bashrc`，
`uv` 找不到就整個起不來。

### 2. 建立監控腳本

腳本本體用「不展開變數」的 heredoc 寫出來（裡面的 `$` 都要留給 bash 自己用），
再用 `sed` 把兩個佔位符換成你的實際值：

```bash
mkdir -p "$PIG_BIN"
cat > "$PIG_BIN/pig-agri-tmux.sh" <<'BASH'
#!/usr/bin/env bash
set -uo pipefail

SESSION_NAME="pig-agri"
WORKING_DIRECTORY="__PIG_DIR__"
# ⚠ 換成你自己的 ntfy topic。topic 本身就是密碼（沒有第二層驗證），
# 知道它的人可以讀你全部的告警、也可以往你手機灌推播。
# 這個 repo 是公開的，真值不要寫回這裡，改寫進未追蹤的 .env 或前端設定。
NTFY_URL="https://ntfy.example.com/your-topic"
# 標題帶主機名：多台機器可能共用同一個 ntfy topic，不標機器就分不出是誰在叫。
# 放最前面是因為手機通知的標題從尾巴截斷。
NTFY_HOSTNAME="$(hostname)"
RESTART_DELAY_SECONDS=10
STOP_MARKER="${XDG_RUNTIME_DIR:-/tmp}/pig-agri-tmux.stop"
SCRIPT_PATH="$(readlink -f "$0")"

send_ntfy_notification() {
  local message="$1"

  curl \
    --fail \
    --silent \
    --show-error \
    --retry 3 \
    --max-time 15 \
    -H "Title: [${NTFY_HOSTNAME}] pig-agri service alert" \
    -H "Priority: high" \
    -H "Tags: warning,arrows_counterclockwise" \
    --data-binary "$message" \
    "$NTFY_URL" >/dev/null || true
}

run_service_loop() {
  local exit_code
  local hostname_value
  local stopped_at

  hostname_value="$(hostname)"

  if [[ ! -d "$WORKING_DIRECTORY" ]]; then
    send_ntfy_notification \
      "pig-agri could not start on ${hostname_value}: directory not found: ${WORKING_DIRECTORY}"
    printf '錯誤：工作目錄不存在：%s\n' "$WORKING_DIRECTORY" >&2
    return 1
  fi

  if ! command -v uv >/dev/null 2>&1; then
    send_ntfy_notification \
      "pig-agri could not start on ${hostname_value}: uv was not found in PATH"
    printf '錯誤：在 PATH 中找不到 uv。\n' >&2
    return 1
  fi

  cd "$WORKING_DIRECTORY" || return 1

  while [[ ! -e "$STOP_MARKER" ]]; do
    printf '\n[%s] 啟動 pig-agri Uvicorn 服務。\n' "$(date --iso-8601=seconds)"

    # --proxy-headers + --forwarded-allow-ips 讓反向代理送的
    # X-Forwarded-Proto/For 被正確採用；只信 127.0.0.1 這個來源。
    uv run uvicorn main:app \
      --log-level info \
      --port 5005 \
      --host "__PIG_HOST__" \
      --proxy-headers \
      --forwarded-allow-ips=127.0.0.1 \
      --reload-exclude "tools/*"

    exit_code=$?
    stopped_at="$(date --iso-8601=seconds)"

    if [[ -e "$STOP_MARKER" ]]; then
      printf '[%s] 收到正常停止要求，不再重啟。\n' "$stopped_at"
      break
    fi

    printf \
      '[%s] Uvicorn 已結束，exit code=%s；%s 秒後重啟。\n' \
      "$stopped_at" \
      "$exit_code" \
      "$RESTART_DELAY_SECONDS" >&2

    send_ntfy_notification \
      "pig-agri stopped unexpectedly on ${hostname_value} at ${stopped_at}; exit code=${exit_code}. Restarting in ${RESTART_DELAY_SECONDS} seconds."

    sleep "$RESTART_DELAY_SECONDS"
  done
}

start_supervisor() {
  rm -f "$STOP_MARKER"

  if ! tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    tmux new-session \
      -d \
      -s "$SESSION_NAME" \
      "$SCRIPT_PATH run-loop"
  fi

  printf 'tmux session 已啟動：%s\n' "$SESSION_NAME"

  while tmux has-session -t "=$SESSION_NAME" 2>/dev/null; do
    sleep 2
  done

  if [[ -e "$STOP_MARKER" ]]; then
    rm -f "$STOP_MARKER"
    return 0
  fi

  printf '錯誤：tmux session 非預期消失。\n' >&2
  return 1
}

stop_supervisor() {
  touch "$STOP_MARKER"

  if tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
    tmux send-keys -t "=$SESSION_NAME" C-c

    for _attempt_number in {1..10}; do
      if ! tmux has-session -t "=$SESSION_NAME" 2>/dev/null; then
        return 0
      fi

      sleep 1
    done

    tmux kill-session -t "=$SESSION_NAME" 2>/dev/null || true
  fi
}

case "${1:-}" in
  supervise)
    start_supervisor
    ;;
  run-loop)
    run_service_loop
    ;;
  stop)
    stop_supervisor
    ;;
  *)
    printf '用法：%s {supervise|run-loop|stop}\n' "$0" >&2
    exit 2
    ;;
esac
BASH

sed -i "s|__PIG_DIR__|$PIG_DIR|; s|__PIG_HOST__|$PIG_HOST|" "$PIG_BIN/pig-agri-tmux.sh"
chmod 0755 "$PIG_BIN/pig-agri-tmux.sh"
bash -n "$PIG_BIN/pig-agri-tmux.sh" && echo "語法正確"
grep -nE 'WORKING_DIRECTORY|--host' "$PIG_BIN/pig-agri-tmux.sh"
```

最後兩行是驗證：語法要過，而且兩個佔位符要真的被換掉（不能還看到 `__PIG_`）。

### 3. 建立 systemd user service

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/pig-agri-tmux.service <<SYSTEMD
[Unit]
Description=pig-agri Uvicorn service inside tmux
After=network-online.target docker.service
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=$HOME/.local/bin:$HOME/.cargo/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$PIG_BIN/pig-agri-tmux.sh supervise
ExecStop=$PIG_BIN/pig-agri-tmux.sh stop
Restart=on-failure
RestartSec=10
TimeoutStopSec=20

[Install]
WantedBy=default.target
SYSTEMD
```

> 這個 heredoc **沒有**加引號（`<<SYSTEMD` 而不是 `<<'SYSTEMD'`），因為
> `$HOME` / `$PIG_BIN` 要在這裡就被展開成實際路徑——systemd unit 檔不認識 shell 變數。
>
> `After=...docker.service`：PostgreSQL 跑在 docker 裡，沒有這個的話開機時
> Uvicorn 會比資料庫早起來、`ConnectionRefusedError` 之後退出。監控腳本 10 秒後
> 會重試成功，但你會收到一則假的異常通知。

### 4. 載入、啟用並立即啟動

```bash
systemctl --user daemon-reload
systemctl --user enable --now pig-agri-tmux.service
```

### 5. 允許未登入時也啟動 user service

只要做一次，需要 sudo：

```bash
sudo loginctl enable-linger "$USER"
loginctl show-user "$USER" -p Linger --value    # 要印出 yes
```

沒有 linger 的話，user service 要等你登入才啟動，登出可能就被停掉。

> **裝完一定要真的重開機驗一次。** `enabled` 加 `Linger=yes` 看起來對，不代表
> 開機後真的會起來。實測參考值：重開機後 6 秒服務就回來了。
---

## 日常管理

### 查看 tmux 中的即時輸出

```bash
tmux attach -t pig-agri
```

如果目前只有一個 tmux session，也可以使用：

```bash
tmux a
```

離開 tmux 但保持服務運作：按 `Ctrl+B`，放開後再按 `D`。

不要在 tmux 中直接按 `Ctrl+C`，除非要測試異常重啟與 ntfy 通知。

### 查看服務狀態

```bash
systemctl --user status pig-agri-tmux.service
```

### 查看 systemd 日誌

systemd 日誌主要記錄監督層狀態，Uvicorn 的即時輸出保留在 tmux 中。

```bash
journalctl --user -u pig-agri-tmux.service -n 100 --no-pager
journalctl --user -u pig-agri-tmux.service -f
```

### 正常停止

```bash
systemctl --user stop pig-agri-tmux.service
```

正常停止會建立停止標記、傳送 `Ctrl+C` 給 tmux 裡的 Uvicorn，且不會發送異常通知。

### 啟動

```bash
systemctl --user start pig-agri-tmux.service
```

### 重新啟動

```bash
systemctl --user restart pig-agri-tmux.service
```

若修改了 `.service` 檔，需先重新載入：

```bash
systemctl --user daemon-reload
systemctl --user restart pig-agri-tmux.service
```

### 停用開機自啟動

```bash
systemctl --user disable --now pig-agri-tmux.service
```

重新啟用：

```bash
systemctl --user enable --now pig-agri-tmux.service
```

---

## 驗證安裝

### 1. 確認 systemd 與 tmux

```bash
systemctl --user is-enabled pig-agri-tmux.service
systemctl --user is-active pig-agri-tmux.service
tmux list-sessions
```

預期前兩行分別顯示 `enabled`、`active`，tmux 清單中應出現 `pig-agri`。

### 2. 確認服務監聽連接埠

```bash
ss -lntp | grep ':5005'
```

因為服務使用 `--host 0.0.0.0`，通常會看到 `0.0.0.0:5005`。這表示同一網路中可路由到此主機的裝置，可能可以連線到該服務，實際結果仍取決於防火牆與網路設定。

### 3. 在主機本機測試 HTTP 回應

```bash
curl -v http://127.0.0.1:5005/
```

即使應用程式的 `/` 路由回傳 `404 Not Found`，只要成功連上 Uvicorn，仍代表服務程序和連接埠正在運作。

### 4. 測試 ntfy

```bash
curl \
  --fail \
  --silent \
  --show-error \
  -H 'Title: pig-agri manual test' \
  -H 'Tags: white_check_mark' \
  --data-binary 'pig-agri ntfy notification test succeeded.' \
  'https://ntfy.example.com/your-topic'
```

### 5. 測試異常重啟

先連入 tmux：

```bash
tmux attach -t pig-agri
```

在 tmux 中按 `Ctrl+C`。監控腳本應該：

1. 顯示 Uvicorn 的 exit code。
2. 發送 ntfy 通知。
3. 等待 10 秒。
4. 重新啟動 Uvicorn。

測試完用 `Ctrl+B`、`D` 離開 tmux。

---

## 啟用帳號密碼登入

服務預設**不需要登入**（`AUTH_ENABLED` 預設 `false`），行為與沒有這個功能時一模一樣。
以下步驟啟用；啟用後除了 `/health`、`/auth/*` 和 `/static/*` 之外，所有 API 與
WebSocket 都要求有效的 session cookie。

### 1. 產生帳號密碼設定

```bash
cd "$PIG_DIR"
uv run python scripts/make_password_hash.py
```

會互動式詢問帳號與密碼（密碼不顯示、不進 shell history），印出四行可直接貼進
`.env` 的設定。密碼至少 12 個字元。

### 2. 貼進 `.env` 並重啟

```
AUTH_ENABLED=true
AUTH_USERNAME=...
AUTH_PASSWORD_HASH=scrypt$16384$8$1$...
AUTH_SESSION_SECRET=...
```

```bash
systemctl --user restart pig-agri-tmux.service
```

### 3. 先確認 TLS

`AUTH_COOKIE_SECURE` 預設 `true`，代表 session cookie 只在 HTTPS 下送出。
**服務目前是 `--host 0.0.0.0` 對公網開放，走明文 HTTP 登入等於把帳密攤在網路上。**
啟用登入前請先在前面架好 TLS（反向代理或 Caddy/nginx + Let's Encrypt）。
純內網測試時才暫時設 `AUTH_COOKIE_SECURE=false`——那條路徑啟動時會印 WARNING。

若使用反向代理，額外把 `AUTH_TRUST_FORWARDED_FOR=true` 打開，登入節流才會看到
真實來源 IP。**直接對外時務必保持 `false`**：任何人都能自己偽造 `X-Forwarded-For`，
讓每次嘗試都算在不同「IP」上，把節流整個繞過去。

### 相關設定

| 設定 | 預設 | 說明 |
|---|---|---|
| `AUTH_ENABLED` | `false` | 總開關 |
| `AUTH_SESSION_HOURS` | `12` | 登入後多久要重新登入 |
| `AUTH_COOKIE_SECURE` | `true` | cookie 只走 HTTPS |
| `AUTH_MAX_ATTEMPTS` | `10` | 同一 IP 連續失敗幾次就鎖 |
| `AUTH_LOCKOUT_MINUTES` | `15` | 鎖多久 |
| `AUTH_TRUST_FORWARDED_FOR` | `false` | 只有在反向代理後面才開 |

### 常見操作

- **改密碼**：重跑步驟 1、換掉 `.env` 的 `AUTH_PASSWORD_HASH`、重啟。
- **強制所有人重新登入**：換掉 `AUTH_SESSION_SECRET`、重啟。
- **關掉登入**：`AUTH_ENABLED=false`、重啟。

> 這幾個設定刻意**只**從 `.env` 讀，不出現在網頁的設定抽屜裡。`PUT /settings` 正是
> 要被這道驗證保護的端點，如果開關是資料庫存的，還沒登入的人就能先打一發
> `PUT /settings` 把鎖拆掉。代價是改設定要重啟。

---

## 改了程式碼什麼時候生效

**這個服務沒有開 `--reload`。** 正式環境不該讓 Uvicorn 監看整個專案目錄自動重載——
推論 pipeline 與 ffmpeg 子程序在重載時的狀態很難保證乾淨。

| 改了什麼 | 什麼時候生效 |
|---|---|
| `*.py` | 要 `systemctl --user restart pig-agri-tmux.service` |
| `static/` 底下的 HTML / CSS / JS | 直接生效，重新整理瀏覽器即可（前端是零 build 的 ES modules）|
| 前端設定頁改的值 | 即時。消費端每輪重讀 DB，不用重啟 |
| `.env` | 要重啟 |

指令裡還留著 `--reload-exclude "tools/*"`：沒有 `--reload` 時它不生效，
留著是為了萬一有人臨時加上 `--reload` 除錯，`tools/` 的變動不會觸發重載。

正常停止時，監控腳本會先送一次 `Ctrl+C`，最多等 10 秒；tmux session 還在才會強制刪除。

---

## 常見問題

### tmux session 不存在

```bash
systemctl --user status pig-agri-tmux.service
journalctl --user -u pig-agri-tmux.service -n 100 --no-pager
systemctl --user restart pig-agri-tmux.service
```

若 tmux session 被手動刪除，systemd 會將監督腳本視為失敗，並在 10 秒後重建。

### 顯示找不到 `uv`

先取得實際路徑：

```bash
command -v uv
```

把該路徑所在目錄加入：

```text
~/.config/systemd/user/pig-agri-tmux.service
```

中的 `Environment=PATH=...`，然後執行：

```bash
systemctl --user daemon-reload
systemctl --user restart pig-agri-tmux.service
```

### 5005 連接埠已被占用

```bash
ss -lntp | grep ':5005'
```

先確認占用程序，不要直接終止不明程序。若是先前手動啟動的 Uvicorn，確認後正常停止舊程序，再重新啟動本服務。

### 專案目錄不存在

```bash
ls -ld "$PIG_DIR"
```

若實際目錄不同，修改 `~/bin/pig-agri-tmux.sh` 中的 `WORKING_DIRECTORY`，再執行：

```bash
systemctl --user restart pig-agri-tmux.service
```

### ntfy 沒收到通知

```bash
curl -v \
  --data-binary 'manual connectivity test' \
  'https://ntfy.example.com/your-topic'
```

同時檢查 DNS、TLS 憑證、防火牆與 ntfy 伺服器是否可達。即使通知發送失敗，腳本仍會繼續嘗試重啟 Uvicorn，避免通知系統故障拖垮主要服務。

### 外部裝置無法連線

先在伺服器主機上確認：

```bash
curl -v http://127.0.0.1:5005/
ss -lntp | grep ':5005'
```

再檢查主機防火牆是否允許 TCP 5005。若使用 UFW，可先查看狀態：

```bash
sudo ufw status verbose
```

不要在不清楚網路暴露範圍時直接開放公網存取。若只需要本機反向代理存取，可將啟動參數改為 `--host 127.0.0.1`。

---

## 完整移除

```bash
systemctl --user disable --now pig-agri-tmux.service
rm -f ~/.config/systemd/user/pig-agri-tmux.service
rm -f ~/bin/pig-agri-tmux.sh
systemctl --user daemon-reload
systemctl --user reset-failed
```

若這台主機不再需要任何未登入即啟動的 user service，也可以停用 linger：

```bash
sudo loginctl disable-linger "$USER"
```

不要在仍有其他 user service 依賴 linger 時執行最後一步。
