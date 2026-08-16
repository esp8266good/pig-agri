#!/usr/bin/env bash
#
# 把舊機（lazoark）的資料融合進新機（ed716-pig），可以分很多次跑。
#
# 設計成「每個子命令都能重複執行」：進度不是記在某個 state 檔裡，而是每次都去
# 問新機「這天你已經有幾筆了」。中途 Ctrl-C、斷網、關機都不會壞掉，重跑只會補缺的。
#
# 用法（都在舊機上跑）：
#   scripts/migrate_merge.sh status          # 只看，不動任何東西
#   scripts/migrate_merge.sh prep            # 新機建暫存表與去重索引
#   scripts/migrate_merge.sh db-day 2026-05-05
#   scripts/migrate_merge.sh db-all          # 逐日跑完（最花時間，可以中斷）
#   scripts/migrate_merge.sh small           # health_alerts / pig_notes / saved_segments
#   scripts/migrate_merge.sh hls             # 影像，一個小時目錄一個單位
#   scripts/migrate_merge.sh verify          # 逐日比對兩邊筆數
#   scripts/migrate_merge.sh finish          # 收尾：丟掉暫存表、對齊 sequence
#
# ⚠ 三件事在動手之前一定要先懂，`status` 會幫你檢查：
#
#   1. object_id 是兩個獨立的號碼空間。舊機的 3 號豬跟新機的 3 號豬沒有任何關係。
#      如果同一台相機、同一段時間，兩邊都有資料，融合之後 analysis/scheduler.py
#      會把兩隻不同的豬的 bbox 中心點串成同一條軌跡，活動量直接算成垃圾。
#      所以預設**只搬「早於新機該相機最早一筆」的時間範圍**，有重疊就擋下來。
#
#   2. tracking_logs.id 不搬。舊機的 id 已經跑到一億八千多萬，新機自己有一條
#      sequence。搬過去的列一律由新機重新配號，去重靠自然鍵
#      (camera_id, frame_id, object_id, timestamp)——跟 dedup_tracking_logs.sql
#      同一把鍵，理由見那支腳本的註解（frame_id 會回繞重用，單獨拿它當鍵不夠）。
#
#   3. user_settings 預設不搬。兩台機器的 ntfy topic（pig / swine）、保留天數、
#      錄影排程本來就該不一樣，蓋過去只會把新機設定弄壞。`status` 會列出差異
#      讓你自己判斷要不要手動搬某幾個 key。
#
set -uo pipefail

# ── 兩邊的位置（要改就改這裡）─────────────────────────────────────
REMOTE_SSH="${REMOTE_SSH:-pig-agri}"
PG_CONTAINER="${PG_CONTAINER:-pig-agri-postgres-1}"
PG_USER="${PG_USER:-pig}"
PG_DB="${PG_DB:-pig_monitoring}"
LOCAL_HLS="${LOCAL_HLS:-/media/lazoark/1TB-HDD/data/pig_monitoring/hls}"
REMOTE_HLS="${REMOTE_HLS:-/home/chen/lobby/pig-agri-data/hls}"

STAGE_TABLE="stage_tracking_migrate"
BOUNDARY_TABLE="migrate_own_boundary"
# tracking_logs 除了 id 以外的欄位。id 不搬（見上面第 2 點）。
COLS="camera_id,timestamp,frame_id,object_id,bb_left,bb_top,bb_width,bb_height,confidence,thermal_intensity"
# 同一份欄位，但每個都加上 s. 前綴，給去重那段的 SELECT 用。
SCOLS="s.camera_id,s.timestamp,s.frame_id,s.object_id,s.bb_left,s.bb_top,s.bb_width,s.bb_height,s.confidence,s.thermal_intensity"

# ── 跑 SQL 的小工具 ───────────────────────────────────────────────
# 遠端一律用 stdin 餵 SQL（psql -f -），不要把 SQL 塞進 ssh 的命令字串裡——
# 巢狀引號在中文、雙引號欄位名、$$ 之間一定會出事。
lsql()  { docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 "$@"; }
rsql()  { ssh "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1 -f -"; }
# -tA：只要值，不要表頭與對齊空白，好給 shell 接
lval()  { lsql -tAc "$1"; }
rval()  { printf '%s' "$1" | ssh "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -tA -f -"; }
rshow() { printf '%s' "$1" | ssh "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -f -"; }

die() { printf '\n✗ %s\n' "$*" >&2; exit 1; }
say() { printf '%s\n' "$*"; }

require_remote_up() {
  ssh -o ConnectTimeout=10 "$REMOTE_SSH" "docker exec $PG_CONTAINER pg_isready -U $PG_USER -d $PG_DB" >/dev/null 2>&1 \
    || die "新機的 postgres 連不上。先在新機跑 docker compose up -d。"
}

# ────────────────────────────────────────────────────────────────
# status：只看不動。先跑這個。
# ────────────────────────────────────────────────────────────────
cmd_status() {
  require_remote_up

  say "═══ tracking_logs 兩邊的範圍 ═══"
  say "舊機："
  lsql -c "SELECT camera_id,
                  count(*) AS 筆數,
                  to_timestamp(min(timestamp)) AS 最舊,
                  to_timestamp(max(timestamp)) AS 最新
           FROM tracking_logs GROUP BY camera_id ORDER BY camera_id;"
  say "新機："
  rshow "SELECT camera_id,
                count(*) AS 筆數,
                to_timestamp(min(timestamp)) AS 最舊,
                to_timestamp(max(timestamp)) AS 最新
         FROM tracking_logs GROUP BY camera_id ORDER BY camera_id;"

  say ""
  say "═══ 有沒有時間重疊（這是會算壞活動量的那件事）═══"
  local has_snap
  has_snap=$(rval "SELECT to_regclass('public.$BOUNDARY_TABLE') IS NOT NULL")
  if [[ "$has_snap" != "t" ]]; then
    say "  （還沒跑 prep，下面用新機目前最早的一筆當基準；prep 之後會改用快照）"
  fi
  local overlap=0
  for cam in $(lval "SELECT DISTINCT camera_id FROM tracking_logs ORDER BY 1"); do
    local lmax bound
    lmax=$(lval "SELECT max(timestamp) FROM tracking_logs WHERE camera_id='$cam'")
    if [[ "$has_snap" == "t" ]]; then
      bound=$(rval "SELECT coalesce(
                      (SELECT boundary_ts FROM $BOUNDARY_TABLE WHERE camera_id='$cam'),
                      (SELECT boundary_ts FROM $BOUNDARY_TABLE WHERE camera_id='__default__'))")
    else
      bound=$(rval "SELECT coalesce(min(timestamp),'infinity') FROM tracking_logs WHERE camera_id='$cam'")
    fi
    if [[ "$bound" == "Infinity" || "$bound" == "infinity" ]]; then
      printf '  %-12s ✅ 新機還沒有這台的資料，整段都可以搬\n' "$cam"
    elif awk "BEGIN{exit !($lmax < $bound)}"; then
      printf '  %-12s ✅ 舊機資料整段早於新機的邊界，可以搬\n' "$cam"
    else
      printf '  %-12s ⚠ 越界！舊機最新 %s，新機邊界 %s\n' "$cam" \
        "$(lval "SELECT to_timestamp($lmax)")" "$(rval "SELECT to_timestamp($bound)")"
      overlap=1
    fi
  done
  if [[ $overlap -eq 1 ]]; then
    say ""
    say "  ⚠ 越界的相機，db-day / db-all 會直接擋下來。"
    say "    越界代表那段時間兩台機器各自在追同一群豬，object_id 卻互不相干。"
    say "    要嘛先把新機那段刪掉，要嘛接受那段的活動量不準、用 ALLOW_OVERLAP=1 硬跑。"
  fi

  say ""
  say "═══ 小表 ═══"
  for t in health_alerts pig_notes saved_segments; do
    printf '  %-16s 舊機 %-10s 新機 %s\n' "$t" \
      "$(lval "SELECT count(*) FROM $t")" "$(rval "SELECT count(*) FROM $t")"
  done

  say ""
  say "═══ user_settings 差異（預設不搬，自己判斷）═══"
  local tmp; tmp=$(mktemp)
  lval "SELECT key || '=' || value FROM user_settings ORDER BY key" | sort > "$tmp"
  rval "SELECT key || '=' || value FROM user_settings ORDER BY key" | sort > "$tmp.r"
  # -：舊機的值 / +：新機的值。
  # 先接成變數再判斷：diff 有差異時回傳 1，開了 pipefail 的話整條 pipeline 就是
  # 失敗，`|| say 完全相同` 會在「有差異」時反過來被觸發。
  local d
  d=$(diff --label 舊機 --label 新機 -u "$tmp" "$tmp.r" | tail -n +3 | grep -E '^[+-]')
  if [[ -n "$d" ]]; then printf '%s\n' "$d"; else say "  （完全相同）"; fi
  rm -f "$tmp" "$tmp.r"

  say ""
  say "═══ 影像小時目錄 ═══"
  local lhours rhours both
  lhours=$(find "$LOCAL_HLS" -mindepth 3 -maxdepth 3 -type d 2>/dev/null | wc -l)
  rhours=$(ssh "$REMOTE_SSH" "find '$REMOTE_HLS' -mindepth 3 -maxdepth 3 -type d 2>/dev/null | wc -l")
  both=$(hls_overlapping_hours | wc -l)
  printf '  舊機 %s 個、新機 %s 個，其中 %s 個小時兩邊都有\n' "$lhours" "$rhours" "$both"
  local need_gb free_gb left_gb minfree
  need_gb=$(du -sBG "$LOCAL_HLS" 2>/dev/null | cut -f1 | tr -d 'G')
  free_gb=$(ssh "$REMOTE_SSH" "df -BG --output=avail '$REMOTE_HLS' | tail -1 | tr -d ' G'")
  left_gb=$((free_gb - need_gb))
  printf '  舊機影像 %sG，新機剩 %sG，全搬完之後剩 %sG\n' "$need_gb" "$free_gb" "$left_gb"
  # storage_monitor 低於 storage_min_free_gb 就會把錄影切到 ephemeral 並發告警。
  minfree=$(lval "SELECT value FROM user_settings WHERE key='storage_min_free_gb'")
  minfree=${minfree:-100}
  if [[ "$left_gb" -lt "$minfree" ]]; then
    say "  ⚠ 全部搬完會剩 ${left_gb}G，低於 storage_min_free_gb=${minfree}——新機一落地錄影"
    say "    就會判定空間不足、切到 ephemeral 並開始發告警。先加硬碟，或者只搬近期的："
    say "    hls <camera> 可以一台一台搬，或自己改 hls_missing_hours 的篩選條件。"
  fi
  if [[ "$both" -gt 0 ]]; then
    say "  ⚠ 兩邊都有的小時，hls 子命令會整個跳過（不會合併同一個小時目錄裡的檔案——"
    say "    兩台機器的 seg 編號都從 000 開始，混在一起 index.m3u8 就對不上了）。"
  fi
}

# ────────────────────────────────────────────────────────────────
# prep：新機建暫存表 + 去重用的索引
# ────────────────────────────────────────────────────────────────
cmd_prep() {
  require_remote_up

  # 邊界快照怎麼取。
  #
  # 預設：拿新機每台相機現有最早的一筆（用 prep 當下的時間封頂）。這假設 prep
  # 是在第一次 db-day 之前跑的——搬進去的列會把 min 拉低，事後補跑就會拍到
  # 被自己污染的邊界。
  #
  # BOUNDARY=now：所有相機的邊界一律訂在 prep 當下。當新機的 tracking_logs
  # 還是空的、或你確定裡面的資料全是搬進來的（不是新機自己錄的），就用這個。
  # 遷移剛開始時這其實是常態。
  local boundary_sql
  if [[ "${BOUNDARY:-}" == "now" ]]; then
    say "BOUNDARY=now：所有相機的邊界一律訂在現在。"
    boundary_sql="-- BOUNDARY=now：不從現有資料推邊界，全部吃 __default__"
  else
    boundary_sql="INSERT INTO $BOUNDARY_TABLE (camera_id, boundary_ts)
SELECT camera_id, LEAST(min(\"timestamp\"), extract(epoch FROM now()))
FROM tracking_logs GROUP BY camera_id
ON CONFLICT (camera_id) DO NOTHING;"
  fi

  say "在新機建暫存表與去重索引（新機資料還少的時候很快；等它長到上億列才會久）…"
  rsql <<SQL || die "prep 失敗"
CREATE UNLOGGED TABLE IF NOT EXISTS $STAGE_TABLE (LIKE tracking_logs INCLUDING DEFAULTS);
ALTER TABLE $STAGE_TABLE DROP COLUMN IF EXISTS id;
-- 去重靠這把索引。跟 dedup_tracking_logs.sql 同一把自然鍵。
CREATE INDEX IF NOT EXISTS idx_tracking_natkey
  ON tracking_logs (camera_id, frame_id, object_id, "timestamp");

-- 「新機自己的資料從哪裡開始」的快照。
--
-- 為什麼要拍快照而不是每次去問 min(timestamp)：搬進去的列也會把 min 拉低，
-- 於是搬完第一天之後，第二天就會被「新機最早是 5/5」擋下來——被自己剛寫的
-- 資料擋住。這張表只在第一次 prep 時寫入，之後 db-day 一律跟它比。
--
-- 邊界取「新機自己最早那筆」與「prep 執行當下」兩者較小的：新機是一直在錄的，
-- 現在還沒有的相機之後也會有，用 prep 時間封頂才不會讓後來錄的那段被蓋到。
CREATE TABLE IF NOT EXISTS $BOUNDARY_TABLE (
  camera_id   varchar(16) PRIMARY KEY,
  boundary_ts double precision NOT NULL
);
$boundary_sql
-- 沒有列在上面的相機都吃這個預設邊界（prep 執行的當下）。
INSERT INTO $BOUNDARY_TABLE (camera_id, boundary_ts)
VALUES ('__default__', extract(epoch FROM now()))
ON CONFLICT (camera_id) DO NOTHING;
SQL
  say ""
  say "新機自己的資料邊界（早於這個時間的舊機資料才准搬）："
  rshow "SELECT camera_id, to_timestamp(boundary_ts) AS 邊界 FROM $BOUNDARY_TABLE ORDER BY camera_id;"
  say "✅ prep 完成。finish 子命令會把暫存表清掉（索引留著，之後去重還用得到）。"
}

# ────────────────────────────────────────────────────────────────
# db-day：搬一天。可以重複跑，只會補缺的。
# ────────────────────────────────────────────────────────────────
cmd_db_day() {
  local day="$1"
  [[ "$day" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || die "日期格式要是 YYYY-MM-DD，收到：$day"
  require_remote_up

  local t0 t1
  t0=$(lval "SELECT extract(epoch FROM timestamptz '$day 00:00:00+08')")
  t1=$(lval "SELECT extract(epoch FROM timestamptz '$day 00:00:00+08' + interval '1 day')")

  local n_local
  n_local=$(lval "SELECT count(*) FROM tracking_logs WHERE timestamp >= $t0 AND timestamp < $t1")
  if [[ "$n_local" == "0" ]]; then
    say "· $day 舊機沒有資料，跳過"
    return 0
  fi

  local n_remote
  n_remote=$(rval "SELECT count(*) FROM tracking_logs WHERE timestamp >= $t0 AND timestamp < $t1")
  if [[ "$n_remote" -ge "$n_local" ]]; then
    say "· $day 新機已有 $n_remote 筆（舊機 $n_local 筆），跳過"
    return 0
  fi

  # 重疊檢查：這一天有沒有踩到「新機自己錄的」範圍。
  # 比的是 prep 拍下的快照，不是活的 min(timestamp)——後者會被本腳本剛搬進去的
  # 資料拉低，導致搬完第一天之後就被自己擋住。
  if [[ "${ALLOW_OVERLAP:-0}" != "1" ]]; then
    for cam in $(lval "SELECT DISTINCT camera_id FROM tracking_logs WHERE timestamp >= $t0 AND timestamp < $t1"); do
      local bound
      bound=$(rval "SELECT coalesce(
                      (SELECT boundary_ts FROM $BOUNDARY_TABLE WHERE camera_id='$cam'),
                      (SELECT boundary_ts FROM $BOUNDARY_TABLE WHERE camera_id='__default__'))")
      [[ -z "$bound" ]] && die "找不到 $cam 的邊界快照。先跑一次 prep。"
      if awk "BEGIN{exit !($t1 > $bound)}"; then
        die "$day 的 $cam 越過新機自己的資料邊界（$(rval "SELECT to_timestamp($bound)")）。
     那之後兩邊的 object_id 是不同號碼空間，硬融合會把活動量算壞。
     真的要搬就 ALLOW_OVERLAP=1 再跑一次，並且知道那段活動量不可信。"
      fi
    done
  fi

  say "→ $day 搬運中（舊機 $n_local 筆，新機目前 $n_remote 筆）…"

  # 清暫存 → COPY 過去 → 去重插入 → 清暫存。去重插入那段在新機是一個交易。
  printf 'TRUNCATE %s;' "$STAGE_TABLE" | rsql >/dev/null || die "$day 清暫存表失敗"

  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 \
    -c "COPY (SELECT $COLS FROM tracking_logs WHERE timestamp >= $t0 AND timestamp < $t1) TO STDOUT" \
  | ssh -C "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1 -c 'COPY $STAGE_TABLE ($COLS) FROM STDIN'" \
    || die "$day COPY 失敗（暫存表可能寫了一半；下次重跑會先 TRUNCATE，不會重複）"

  rsql <<SQL >/dev/null || die "$day 去重插入失敗（交易已 rollback，新機沒有半套資料，重跑即可）"
BEGIN;
SET LOCAL work_mem = '512MB';
INSERT INTO tracking_logs ($COLS)
SELECT DISTINCT ON (s.camera_id, s.frame_id, s.object_id, s."timestamp") $SCOLS
FROM $STAGE_TABLE s
WHERE NOT EXISTS (
  SELECT 1 FROM tracking_logs t
  WHERE t.camera_id   = s.camera_id
    AND t.frame_id    = s.frame_id
    AND t.object_id   = s.object_id
    AND t."timestamp" = s."timestamp"
);
TRUNCATE $STAGE_TABLE;
COMMIT;
SQL

  local after
  after=$(rval "SELECT count(*) FROM tracking_logs WHERE timestamp >= $t0 AND timestamp < $t1")
  say "  ✅ $day 完成：新機 $n_remote → $after 筆"
}

# ────────────────────────────────────────────────────────────────
# db-all：從最舊跑到最新。中斷了直接再跑一次。
# ────────────────────────────────────────────────────────────────
cmd_db_all() {
  require_remote_up
  local days
  days=$(lval "SELECT DISTINCT to_char(to_timestamp(timestamp) AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD')
               FROM tracking_logs ORDER BY 1")
  # SINCE=YYYY-MM-DD 或 DAYS=N：只搬近期的。分階段遷移（先讓服務轉過去，
  # 完整歷史等加了硬碟再補）的時候用。
  local since="${SINCE:-}"
  if [[ -z "$since" && -n "${DAYS:-}" ]]; then
    since=$(date -d "$DAYS days ago" +%Y-%m-%d)
  fi
  if [[ -n "$since" ]]; then
    say "只搬 $since（含）之後的。"
    days=$(printf '%s\n' "$days" | awk -v s="$since" '$1 >= s')
  fi
  [[ -n "$days" ]] || { say "沒有符合條件的日期。"; return 0; }
  local total done_n=0
  total=$(printf '%s\n' "$days" | wc -l)
  say "共 $total 天要處理。中斷了重跑這個子命令即可，已搬過的會自動跳過。"
  for d in $days; do
    done_n=$((done_n + 1))
    printf '[%d/%d] ' "$done_n" "$total"
    cmd_db_day "$d" || die "停在 $d。修好之後重跑 db-all 會從這裡接下去。"
  done
  say "✅ tracking_logs 全部搬完。"
}

# ────────────────────────────────────────────────────────────────
# small：三張小表。量小，一次搬完，靠自然鍵去重。
# ────────────────────────────────────────────────────────────────
cmd_small() {
  require_remote_up

  # saved_segments 本來就有 UNIQUE(camera_id, hour_ts)，直接 ON CONFLICT
  say "→ saved_segments"
  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 \
    -c "COPY (SELECT camera_id,hour_ts,label,note,created_at FROM saved_segments) TO STDOUT" \
  | ssh -C "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1 -c '
      CREATE TEMP TABLE s (camera_id varchar(16), hour_ts bigint, label text, note text, created_at timestamptz);
      COPY s FROM STDIN;
      INSERT INTO saved_segments (camera_id,hour_ts,label,note,created_at)
        SELECT camera_id,hour_ts,label,note,created_at FROM s
        ON CONFLICT (camera_id,hour_ts) DO NOTHING;'" || die "saved_segments 失敗"

  # health_alerts：沒有唯一鍵，用 (camera_id, object_id, triggered_at, metric) 當自然鍵
  say "→ health_alerts"
  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 \
    -c "COPY (SELECT camera_id,object_id,triggered_at,metric,current_value,mean_value,std_value,is_read FROM health_alerts) TO STDOUT" \
  | ssh -C "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1 -c '
      CREATE TEMP TABLE s (camera_id varchar(16), object_id int, triggered_at timestamptz, metric varchar(32),
                           current_value real, mean_value real, std_value real, is_read boolean);
      COPY s FROM STDIN;
      INSERT INTO health_alerts (camera_id,object_id,triggered_at,metric,current_value,mean_value,std_value,is_read)
        SELECT s.* FROM s WHERE NOT EXISTS (
          SELECT 1 FROM health_alerts h
          WHERE h.camera_id=s.camera_id AND h.object_id=s.object_id
            AND h.triggered_at=s.triggered_at AND h.metric=s.metric);'" || die "health_alerts 失敗"

  # pig_notes：人手打的備註，用 (camera_id, object_id, note_time, content) 當自然鍵
  say "→ pig_notes"
  docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 \
    -c "COPY (SELECT camera_id,object_id,note_time,content,created_at FROM pig_notes) TO STDOUT" \
  | ssh -C "$REMOTE_SSH" "docker exec -i $PG_CONTAINER psql -U $PG_USER -d $PG_DB -v ON_ERROR_STOP=1 -c '
      CREATE TEMP TABLE s (camera_id varchar(16), object_id int, note_time timestamptz, content text, created_at timestamptz);
      COPY s FROM STDIN;
      INSERT INTO pig_notes (camera_id,object_id,note_time,content,created_at)
        SELECT s.* FROM s WHERE NOT EXISTS (
          SELECT 1 FROM pig_notes p
          WHERE p.camera_id=s.camera_id AND p.object_id IS NOT DISTINCT FROM s.object_id
            AND p.note_time=s.note_time AND p.content IS NOT DISTINCT FROM s.content);'" || die "pig_notes 失敗"

  say "✅ 小表搬完。"
}

# ────────────────────────────────────────────────────────────────
# 影像。小時目錄是最小單位，兩邊都有的整個跳過。
# ────────────────────────────────────────────────────────────────
hls_local_hours()  { (cd "$LOCAL_HLS" 2>/dev/null && find . -mindepth 3 -maxdepth 3 -type d | sed 's|^\./||' | sort); }
hls_remote_hours() { ssh "$REMOTE_SSH" "cd '$REMOTE_HLS' 2>/dev/null && find . -mindepth 3 -maxdepth 3 -type d | sed 's|^\./||' | sort"; }
hls_overlapping_hours() { comm -12 <(hls_local_hours) <(hls_remote_hours); }
hls_missing_hours()     { comm -23 <(hls_local_hours) <(hls_remote_hours); }

cmd_hls() {
  local filter="${1:-}"
  require_remote_up
  [[ -d "$LOCAL_HLS" ]] || die "找不到舊機的影像目錄：$LOCAL_HLS"

  local incoming="$REMOTE_HLS/.incoming"

  local list; list=$(mktemp)
  if [[ -n "$filter" ]]; then
    hls_missing_hours | grep "^${filter}/" > "$list"
  else
    hls_missing_hours > "$list"
  fi
  # SINCE=YYYY-MM-DD 或 DAYS=N：只搬近期的小時目錄。目錄名是 YYYY-MM-DD-HH，
  # 直接拿字串比大小就是時間順序。
  local since="${SINCE:-}"
  if [[ -z "$since" && -n "${DAYS:-}" ]]; then
    since=$(date -d "$DAYS days ago" +%Y-%m-%d)
  fi
  if [[ -n "$since" ]]; then
    say "只搬 $since（含）之後的小時目錄。"
    awk -F/ -v s="$since" '$NF >= s' "$list" > "$list.cut" && mv "$list.cut" "$list"
  fi
  # HOURS_LIMIT=N：這一輪只搬 N 個小時目錄。要一點一點搬、或者只想先試一小批
  # 的時候用。不設就是全部。
  if [[ -n "${HOURS_LIMIT:-}" ]]; then
    head -n "$HOURS_LIMIT" "$list" > "$list.cut" && mv "$list.cut" "$list"
  fi

  local n skipped
  n=$(wc -l < "$list")
  skipped=$(hls_overlapping_hours | wc -l)
  if [[ "$n" == "0" ]]; then
    say "沒有要搬的小時目錄（兩邊都有的 $skipped 個已跳過）。"
    rm -f "$list"; return 0
  fi

  # ── 空間煞車 ──────────────────────────────────────────────────
  # 沒有這道的話，一個 hls 子命令就能把新機的碟塞到 storage_monitor 的門檻以下，
  # 錄影當場切成 ephemeral 並開始發告警。
  local need_gb free_gb left_gb minfree
  need_gb=$( (cd "$LOCAL_HLS" && tr '\n' '\0' < "$list" | du -scBG --files0-from=- 2>/dev/null | tail -1 | cut -f1 | tr -d 'G') )
  free_gb=$(ssh "$REMOTE_SSH" "df -BG --output=avail '$REMOTE_HLS' | tail -1 | tr -d ' G'")
  left_gb=$((free_gb - ${need_gb:-0}))
  minfree=$(lval "SELECT value FROM user_settings WHERE key='storage_min_free_gb'")
  minfree=${minfree:-100}
  say "要搬 $n 個小時目錄（約 ${need_gb:-?}G），跳過兩邊都有的 $skipped 個。"
  say "新機現在剩 ${free_gb}G，搬完會剩 ${left_gb}G（門檻 ${minfree}G）。"
  if [[ "$left_gb" -lt "$minfree" ]] && [[ "${FORCE:-0}" != "1" ]]; then
    rm -f "$list"
    die "搬完會低於 storage_min_free_gb=${minfree}，擋下來。
     先加硬碟（改 REMOTE_HLS 指到新碟），或用 HOURS_LIMIT=N 分批搬，
     或者你確定要吃掉空間就 FORCE=1。"
  fi

  # ── 先落到 .incoming，整個小時目錄傳完才搬進正式位置 ──────────
  # 為什麼要多這一手：rsync 中途被 Ctrl-C 或斷線，半個小時目錄會留在正式位置，
  # 之後 hls_missing_hours 看它「存在」就永遠跳過——那個小時會永久缺片，而且
  # 不會有任何人發現。落在 .incoming 就沒這問題，正式位置永遠只有完整的。
  say "可以隨時 Ctrl-C：沒傳完的留在 .incoming，正式位置只會有完整的小時目錄。"

  ssh "$REMOTE_SSH" "mkdir -p '$incoming'"
  # 進度條只在終端機顯示。導到檔案或用 tee 接的時候，progress2 那串會刷出
  # 上萬行 \r 更新，把 log 洗到看不懂。
  local progress=(--info=stats1)
  [[ -t 1 ]] && progress=(--info=progress2)
  # --files-from 會關掉遞迴，即使有 -a 也要自己補 -r，否則只會建出空目錄。
  # 清單裡是相對於 $LOCAL_HLS 的小時目錄路徑，--files-from 自帶 --relative，
  # camera/stream/hour 的結構會原樣長在新機上。
  rsync -a -r --partial "${progress[@]}" --files-from="$list" \
        "$LOCAL_HLS/" "$REMOTE_SSH:$incoming/" \
    || die "rsync 失敗（已傳的留在 .incoming，重跑會接續）"

  say "傳完了，搬進正式位置…"
  ssh "$REMOTE_SSH" "bash -s" <<REMOTE || die "搬進正式位置失敗（資料還在 .incoming，重跑即可）"
set -euo pipefail
cd '$incoming' 2>/dev/null || exit 0
moved=0
while IFS= read -r d; do
  [ -d "\$d" ] || continue
  mkdir -p '$REMOTE_HLS'/"\$(dirname "\$d")"
  rm -rf '$REMOTE_HLS'/"\$d"
  mv "\$d" '$REMOTE_HLS'/"\$d"
  moved=\$((moved + 1))
done < <(find . -mindepth 3 -maxdepth 3 -type d | sed 's|^\./||')
# 只清空殼，.incoming 本身留著給下一輪用
find . -mindepth 1 -type d -empty -delete 2>/dev/null || true
echo "搬進正式位置：\$moved 個小時目錄"
REMOTE

  rm -f "$list"
  say "✅ 這一輪完成。再跑一次 hls 會告訴你還缺幾個。"
}

# ────────────────────────────────────────────────────────────────
# verify：逐日比對筆數
# ────────────────────────────────────────────────────────────────
cmd_verify() {
  require_remote_up
  # 兩邊各跑一次 group by 就好。早期版本是一天打一次 count，100 天就是 200 次
  # 跨機往返加 200 次全表掃，跑到天荒地老。
  local q="SELECT to_char(to_timestamp(\"timestamp\") AT TIME ZONE 'Asia/Taipei','YYYY-MM-DD') AS d,
                  count(*)
           FROM tracking_logs GROUP BY d ORDER BY d"
  local lf rf; lf=$(mktemp); rf=$(mktemp)
  say "統計中（兩邊各掃一次表，大表要一兩分鐘）…"
  lval "$q" > "$lf"
  rval "$q" > "$rf"

  say ""
  say "日期            舊機        新機         差"
  # 第一個檔（$rf）先讀進 r[]，第二個檔（$lf）逐行比對。舊機有、新機沒有的
  # 日期，新機算 0。
  awk -F'|' '
    NR==FNR { r[$1]=$2; next }
    { nl=$2; nr=(($1 in r) ? r[$1] : 0); d=nr-nl
      printf "%s  %10d  %10d  %10d%s\n", $1, nl, nr, d, (nr<nl ? "  ⚠ 少了" : "") }
  ' "$rf" "$lf"

  local missing
  missing=$(awk -F'|' 'NR==FNR{r[$1]=$2;next}{nr=(($1 in r)?r[$1]:0); if(nr<$2) c++} END{print c+0}' "$rf" "$lf")
  rm -f "$lf" "$rf"

  say ""
  if [[ "$missing" -gt 0 ]]; then
    say "⚠ 有 $missing 天還沒搬齊，跑 db-all 補。"
  else
    say "✅ 每一天新機的筆數都 ≥ 舊機。"
  fi
  say ""
  say "影像還缺 $(hls_missing_hours | wc -l) 個小時目錄。"
}

# ────────────────────────────────────────────────────────────────
# finish：收尾
# ────────────────────────────────────────────────────────────────
cmd_finish() {
  require_remote_up
  say "丟掉暫存表，並把 sequence 對齊到目前最大的 id…"
  rsql <<SQL >/dev/null || die "finish 失敗"
DROP TABLE IF EXISTS $STAGE_TABLE;
DROP TABLE IF EXISTS $BOUNDARY_TABLE;
SELECT setval('tracking_logs_id_seq',  coalesce((SELECT max(id) FROM tracking_logs), 1));
SELECT setval('health_alerts_id_seq',  coalesce((SELECT max(id) FROM health_alerts), 1));
SELECT setval('pig_notes_id_seq',      coalesce((SELECT max(id) FROM pig_notes), 1));
SELECT setval('saved_segments_id_seq', coalesce((SELECT max(id) FROM saved_segments), 1));
SQL
  say "✅ 收尾完成。"
  say ""
  say "idx_tracking_natkey 這把索引留著沒刪——之後再跑 dedup_tracking_logs.sql 還用得到。"
  say "確定不要了就在新機下：DROP INDEX idx_tracking_natkey;"
}

# ────────────────────────────────────────────────────────────────
case "${1:-}" in
  status)  cmd_status ;;
  prep)    cmd_prep ;;
  db-day)  shift; [[ $# -ge 1 ]] || die "用法：db-day YYYY-MM-DD"; cmd_db_day "$1" ;;
  db-all)  cmd_db_all ;;
  small)   cmd_small ;;
  hls)     shift; cmd_hls "${1:-}" ;;
  verify)  cmd_verify ;;
  finish)  cmd_finish ;;
  *)
    sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
