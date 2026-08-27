#!/usr/bin/env bash
# 等到農場開燈之後量關注清單的命中率，把結果推到 ntfy。
# 在跑 app 的那台機器上 detach 執行：
#   setsid nohup bash scripts/focus_hitrate_watch.sh > /tmp/focus_watch.log 2>&1 < /dev/null &
#
# ⚠ topic 是公開 URL：不放密碼、不放內部主機名。
set -uo pipefail
cd "$(dirname "$0")/.."

NTFY=${NTFY:-https://ntfy.ed716.duckdns.org/experiments}
WAKE=${WAKE:-tomorrow 10:00}
CAM=${CAM:-rpi5_dual}
# 分析間隔是 30 分鐘。取三次、間隔 35 分鐘，跨得過一輪，
# 免得剛好卡在「這一輪還沒跑完」而量到一片零。
SAMPLES=${SAMPLES:-3}
GAP=${GAP:-2100}

push() {  # push <title> <priority> <tags> <body>
  curl -s -m 20 -o /dev/null -w "ntfy %{http_code}\n" \
    -H "Title: $1" -H "Priority: $2" -H "Tags: $3" -d "$4" "$NTFY"
}

target=$(date -d "$WAKE" +%s)
now=$(date +%s)
wait_s=$(( target - now ))
[ "$wait_s" -lt 0 ] && wait_s=0

push "關注清單驗收：已排程" "default" "hourglass" \
"農場關燈中，量不到東西。
排到 $(date -d "@$target" '+%m-%d %H:%M') 開燈後再量，取 $SAMPLES 次。
量完會再推一次，帶接手用的指令。"

sleep "$wait_s"

report=""
for i in $(seq 1 "$SAMPLES"); do
  report+="── 第 $i 次 $(date '+%H:%M') ──"$'\n'
  report+="$(python3 scripts/focus_hitrate.py --camera "$CAM" 2>&1)"$'\n\n'
  [ "$i" -lt "$SAMPLES" ] && sleep "$GAP"
done

# 命中率那行長成「命中率  8/8 = 100%」。三次裡只要有一次量得到就算數：
# 另外兩次可能卡在 herd_low（豬群休息）或首次分析未完成。
worst=$(printf '%s' "$report" | grep -a '命中率' | grep -oE '[0-9]+%' | tr -d '%' | sort -n | head -1)
if [ -z "$worst" ]; then
  title="關注清單驗收：還是量不到"; prio="high"; tags="warning"
elif [ "$worst" -ge 90 ]; then
  title="關注清單驗收：通過 ${worst}%"; prio="default"; tags="white_check_mark"
else
  title="關注清單驗收：命中率只有 ${worst}%"; prio="urgent"; tags="rotating_light"
fi

msg="$report"
msg+=$'── 接手 ──\n'
msg+=$'cd pig-agri && ./scripts/check_progress.sh\n'
msg+=$'背景：docs/superpowers/specs/2026-08-27-focus-list-onscreen-contract-design.md\n'
msg+=$'（「否掉的做法」那節是已經排除的路，不要重新推導）\n'
msg+=$'分支 fix/focus-list-onscreen 還沒併回 master。'
push "$title" "$prio" "$tags" "$msg"
printf '%s\n' "$report"
