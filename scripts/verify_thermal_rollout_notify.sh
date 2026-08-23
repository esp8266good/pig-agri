#!/usr/bin/env bash
# 跑 verify_thermal_rollout.sh 並把結果推到 ntfy。給 systemd timer 用。
#
# ⚠ topic 是公開 URL：推播內容不放密碼、不放內部主機名。
set -uo pipefail
cd "$(dirname "$0")/.."

NTFY=${NTFY:-https://ntfy.ed716.duckdns.org/experiments}
result=$(bash scripts/verify_thermal_rollout.sh 2>&1)
verdict=$(printf '%s' "$result" | head -1)
body=$(printf '%s' "$result" | tail -n +2)

case "$verdict" in
  PASS) title="熱像體溫改版：驗證通過"; prio="default"; tags="white_check_mark" ;;
  WARN) title="熱像體溫改版：部分無法判定"; prio="high";    tags="warning" ;;
  *)    title="熱像體溫改版：驗證失敗";     prio="urgent";  tags="rotating_light" ;;
esac

msg="$body"
if [ "$verdict" != "PASS" ]; then
  msg+=$'\n''退回方式（兩邊各自獨立，可只退一邊）：'
  msg+=$'\n''· 擷取端：sender_config_dual.yaml 的 mode 改回 y16_tlinear，'
  msg+=$'\n''  然後 systemctl --user restart rgbt-sender'
  msg+=$'\n''· server：git revert 那個 merge，重啟 app service'
fi
# 推播會在手機上讀到，手邊不會有 repo：把接手要用的最短入口直接寫進訊息，
# 而不是只給一個檔案路徑（那等於還要先開一個 session 才知道怎麼開對的 session）。
msg+=$'\n\n''── 接手 ──'
msg+=$'\n''cd pig-agri && bash scripts/verify_thermal_rollout.sh'
msg+=$'\n''完整背景與退回方式：'
msg+=$'\n''docs/superpowers/plans/2026-08-24-thermal-y16-rollout.md'
msg+=$'\n''（第 4 節是這次排除掉的路，不要重新推導）'

# 每天跑，但不是每天都吵人：第一次通過一定推（那是交接要的答案），之後只有
# 退化或無法判定才推。哪天又壞掉會自己叫，通過的日子安靜。
FLAG=.verify_thermal_passed
if [ "$verdict" = "PASS" ] && [ -f "$FLAG" ]; then
  echo "PASS 且先前已通知過，不重複推播"
else
  curl -s -m 20 -o /dev/null -w "ntfy %{http_code}\n" \
    -H "Title: $title" -H "Priority: $prio" -H "Tags: $tags" \
    -d "$msg" "$NTFY"
  [ "$verdict" = "PASS" ] && date -Is > "$FLAG"
fi

printf '%s\n' "$result"
