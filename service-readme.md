# pig-agri 開機自啟動與維運手冊

本手冊使用 **systemd user service** 在開機後啟動一個名為 `pig-agri` 的 tmux session。Uvicorn 若非因正常停機流程而結束，監控腳本會透過 ntfy 發送通知，等待數秒後自動重啟服務。

## 完成後的行為

- 開機後自動建立 tmux session：`pig-agri`
- 在 `/home/lazoark/OneDrive/Curriculum/pig-agri` 中執行：

```bash
uv run uvicorn main:app \
  --reload \
  --log-level info \
  --port 5005 \
  --host 0.0.0.0 \
  --reload-exclude "tools/*"
```

- Uvicorn 意外結束時，發布通知到：<https://ntfy.ed716.duckdns.org/pig>
- 異常後等待 10 秒再重啟，避免快速失敗造成重啟風暴
- 可使用 `tmux attach -t pig-agri` 查看即時運作狀況
- 正常執行 `systemctl --user stop pig-agri-tmux.service` 時，不會誤發異常通知，也不會自動重啟 Uvicorn
- tmux session 若被非預期刪除，systemd 會在 10 秒後重建

> 注意：服務使用 `--host 0.0.0.0`，會監聽所有網路介面。請確認主機防火牆、路由器轉發及存取控制符合預期。

---

## 一次性安裝

### 1. 確認必要工具

```bash
command -v tmux
command -v curl
command -v uv
```

若 `uv` 的路徑不是下列任一路徑，也沒有出現在 systemd 的 PATH 中，請稍後修改 service 的 `Environment=PATH=...`：

- `/home/lazoark/.local/bin/uv`
- `/home/lazoark/.cargo/bin/uv`
- `/usr/local/bin/uv`
- `/usr/bin/uv`

同時確認專案目錄存在：

```bash
ls -ld /home/lazoark/OneDrive/Curriculum/pig-agri
```

### 2. 建立監控腳本

```bash
mkdir -p /home/lazoark/bin
cat > /home/lazoark/bin/pig-agri-tmux.sh <<'BASH'
#!/usr/bin/env bash
set -uo pipefail

SESSION_NAME="pig-agri"
WORKING_DIRECTORY="/home/lazoark/OneDrive/Curriculum/pig-agri"
NTFY_URL="https://ntfy.ed716.duckdns.org/pig"
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
    -H "Title: pig-agri service alert" \
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

    uv run uvicorn main:app \
      --reload \
      --log-level info \
      --port 5005 \
      --host 0.0.0.0 \
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

chmod 0755 /home/lazoark/bin/pig-agri-tmux.sh
```

### 3. 建立 systemd user service

```bash
mkdir -p /home/lazoark/.config/systemd/user
cat > /home/lazoark/.config/systemd/user/pig-agri-tmux.service <<'SYSTEMD'
[Unit]
Description=pig-agri Uvicorn service inside tmux
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=/home/lazoark/.local/bin:/home/lazoark/.cargo/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=/home/lazoark/bin/pig-agri-tmux.sh supervise
ExecStop=/home/lazoark/bin/pig-agri-tmux.sh stop
Restart=on-failure
RestartSec=10
TimeoutStopSec=20

[Install]
WantedBy=default.target
SYSTEMD
```

### 4. 載入、啟用並立即啟動

```bash
systemctl --user daemon-reload
systemctl --user enable --now pig-agri-tmux.service
```

### 5. 允許未登入時也啟動 user service

這一步通常只需執行一次，需要 sudo 權限：

```bash
sudo loginctl enable-linger lazoark
```

沒有 linger 時，user service 通常要等使用者登入後才會啟動，登出後也可能被停止。啟用 linger 後，系統可在開機時啟動該使用者的 systemd user manager。

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
  'https://ntfy.ed716.duckdns.org/pig'
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

## `--reload` 模式注意事項

此服務按照指定指令保留 `--reload`，適合開發或教學環境。檔案內容改變時，Uvicorn 的 reloader 會重新載入應用程式。

- `--reload-exclude "tools/*"` 會排除 `tools` 目錄下符合該模式的檔案變化。
- reloader 可能產生父程序與子程序，因此程序清單中可能看到不只一個相關程序。
- 正常停止時，監控腳本會先傳送一次 `Ctrl+C`，最多等待 10 秒；若 tmux session 仍存在才會強制刪除 session。
- 若是正式生產環境，通常應評估移除 `--reload`，避免不必要的檔案監控與自動載入。

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
/home/lazoark/.config/systemd/user/pig-agri-tmux.service
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
ls -ld /home/lazoark/OneDrive/Curriculum/pig-agri
```

若實際目錄不同，修改 `/home/lazoark/bin/pig-agri-tmux.sh` 中的 `WORKING_DIRECTORY`，再執行：

```bash
systemctl --user restart pig-agri-tmux.service
```

### ntfy 沒收到通知

```bash
curl -v \
  --data-binary 'manual connectivity test' \
  'https://ntfy.ed716.duckdns.org/pig'
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
rm -f /home/lazoark/.config/systemd/user/pig-agri-tmux.service
rm -f /home/lazoark/bin/pig-agri-tmux.sh
systemctl --user daemon-reload
systemctl --user reset-failed
```

若這台主機不再需要任何未登入即啟動的 user service，也可以停用 linger：

```bash
sudo loginctl disable-linger lazoark
```

不要在仍有其他 user service 依賴 linger 時執行最後一步。
