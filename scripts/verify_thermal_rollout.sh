#!/usr/bin/env bash
# 2026-08-24 熱像/體溫改版的落地驗證。可重複執行，只讀不寫。
#
# 為什麼需要它：改版當晚豬舍沒開燈，rgb 全黑 → 偵測不到豬 → 沒有 tracking_logs
# 也就沒有體溫可看。整條鏈只在本機用真實 Y16 樣本驗過，正式機上要等天亮。
#
# ⚠ 判定門檻寫在這裡，不是看到數字之後才決定的。特別是 spread：舊版的 bug 特徵
# 就是「每隻豬拿到同一個數字」（取樣被 clamp 在圖的左上角同一小塊），所以光看
# 「有資料、範圍合理」會漏掉退化。同一秒內不同 object_id 的體溫必須有差異。
set -uo pipefail
cd "$(dirname "$0")/.."

PASS_MIN_ROWS=100        # 這段時間內至少要有這麼多筆帶體溫的紀錄
PASS_LO_C=20.0           # 體溫合理範圍（超出＝溫度換算或 colormap 路徑錯了）
PASS_HI_C=45.0
PASS_MIN_SPREAD=0.3      # 同一秒不同豬之間的體溫標準差下限，單位 °C
WINDOW_SEC=${WINDOW_SEC:-1800}

DB_URL=$(grep -E '^DATABASE_URL=' .env | head -1 | cut -d= -f2-)
psql_q() { psql "$DB_URL" -tAF'|' -c "$1" 2>&1; }

out=""
verdict="PASS"
add() { out+="$1"$'\n'; }
fail() { verdict="FAIL"; }
warn() { [ "$verdict" = "PASS" ] && verdict="WARN"; }

add "檢查區間：過去 $((WINDOW_SEC/60)) 分鐘"
add ""

# ── 1. 體溫有沒有落地，值合不合理 ───────────────────────────
row=$(psql_q "SELECT count(*), count(thermal_celsius),
                     coalesce(min(thermal_celsius),0)::numeric(5,2),
                     coalesce(max(thermal_celsius),0)::numeric(5,2),
                     coalesce(avg(thermal_celsius),0)::numeric(5,2)
              FROM tracking_logs
              WHERE timestamp > extract(epoch from now()) - $WINDOW_SEC;")
IFS='|' read -r n_rows n_temp t_min t_max t_avg <<< "$row"

if [ "${n_rows:-0}" = "0" ]; then
  add "❌ tracking_logs 完全沒有新資料（偵測沒在跑，或豬舍還是暗的）"
  fail
elif [ "${n_temp:-0}" -lt "$PASS_MIN_ROWS" ]; then
  add "❌ 帶體溫的紀錄只有 ${n_temp}/${n_rows} 筆（門檻 ${PASS_MIN_ROWS}）"
  add "   → 熱像可能又走回舊的 JPEG 路徑，那條路徑體溫一律回 None"
  fail
else
  add "✅ 體溫落地 ${n_temp}/${n_rows} 筆"
  add "   範圍 ${t_min} ~ ${t_max}°C，平均 ${t_avg}°C"
  ok_lo=$(awk -v a="$t_min" -v b="$PASS_LO_C" 'BEGIN{print (a>=b)?1:0}')
  ok_hi=$(awk -v a="$t_max" -v b="$PASS_HI_C" 'BEGIN{print (a<=b)?1:0}')
  if [ "$ok_lo$ok_hi" != "11" ]; then
    add "❌ 超出合理範圍 ${PASS_LO_C}~${PASS_HI_C}°C → 溫度換算或上色路徑有問題"
    fail
  fi
fi

# ── 2. 不同豬的體溫要真的不一樣（舊 bug 的特徵是全部一樣）──
spread=$(psql_q "SELECT coalesce(round(avg(sd)::numeric,3),0) FROM (
                   SELECT stddev_pop(thermal_celsius) AS sd
                   FROM tracking_logs
                   WHERE timestamp > extract(epoch from now()) - $WINDOW_SEC
                     AND thermal_celsius IS NOT NULL
                   GROUP BY camera_id, round(timestamp::numeric, 0)
                   HAVING count(DISTINCT object_id) >= 2
                 ) s;")
if [ -z "$spread" ] || [ "$spread" = "0" ] || [ "$spread" = "0.000" ]; then
  add "⚠ 算不出同一秒多隻豬的體溫差異（可能只偵測到一隻，或還沒有資料）"
  warn
else
  ok=$(awk -v a="$spread" -v b="$PASS_MIN_SPREAD" 'BEGIN{print (a>=b)?1:0}')
  if [ "$ok" = "1" ]; then
    add "✅ 同一秒不同豬的體溫標準差 ${spread}°C（門檻 ${PASS_MIN_SPREAD}）"
    add "   → 取樣確實跟著各自的 bbox 走，不是全部取同一塊"
  else
    add "❌ 同一秒不同豬的體溫標準差只有 ${spread}°C"
    add "   → 這正是舊 bug 的特徵：每隻豬都取到圖上同一小塊"
    fail
  fi
fi

# ── 3. 影像鏈路：熱像尺寸、擷取端還活著嗎 ───────────────────
add ""
th_dir=$(ls -dt /dev/shm/pig_live/*/thermal/_live 2>/dev/null | head -1)
seg=$(ls -t "$th_dir"/seg_*.ts 2>/dev/null | head -1)
if [ -n "$seg" ]; then
  wh=$(ffprobe -v error -select_streams v:0 -show_entries stream=width,height -of csv=p=0 "$seg" 2>/dev/null | head -1)
  if [ "$wh" = "640,480" ]; then
    add "✅ 熱像 HLS ${wh}（原生 4:3，沒有被拉伸）"
  else
    add "❌ 熱像 HLS ${wh}，預期 640,480"
    add "   → ffmpeg 還停在舊尺寸，重啟 app 才會跟上"
    fail
  fi
  sz=$(stat -c %s "$seg"); add "   最新 segment $((sz/1024)) KB / 4s"
else
  add "⚠ 找不到 live 熱像 segment"
  warn
fi

if systemctl --user is-active pig-agri-tmux.service >/dev/null 2>&1; then
  add "✅ app service 運作中"
else
  add "❌ app service 沒在跑"
  fail
fi

echo "$verdict"
echo "$out"
