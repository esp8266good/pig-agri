#!/usr/bin/env bash
# 關注清單改版的驗收檢查。在開發機上跑，會 ssh 到跑 app 的那台。
#   ./scripts/check_progress.sh [camera_id]
set -uo pipefail
HOST="${PIG_HOST:-pig-agri}"
APP_DIR="${PIG_APP_DIR:-lobby/pig-agri}"
CAM="${1:-rpi5_dual}"

echo "=== 1. 服務還活著嗎 ==="
ssh "$HOST" "systemctl --user is-active pig-agri-tmux.service; cd $APP_DIR && git log --oneline -1 && git rev-parse --abbrev-ref HEAD"

echo
echo "=== 2. 命中率 ==="
ssh "$HOST" "cd $APP_DIR && python3 scripts/focus_hitrate.py --camera $CAM"

echo
echo "=== 3. scheduler 每輪的落差 log ==="
# 「快取 N 個編號，畫面上 K 個」。兩個數字貼近＝這次改版有效。
# app 的 stdout 只在 tmux pane 裡，沒有落地成檔案。
out=$(ssh "$HOST" "tmux capture-pane -p -t pig-agri -S -8000 2>/dev/null | grep -a '關注快取' | tail -10")
if [ -z "$out" ]; then
  echo "（還沒有這行。第一輪分析要等 analysis_interval_minutes＝30 分鐘，"
  echo "  重啟後也要重新等一輪；夜間沒有偵測資料時也不會有。）"
else
  printf '%s\n' "$out"
fi
