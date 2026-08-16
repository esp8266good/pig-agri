#!/usr/bin/env bash
# 去重 tracking_logs 的執行包裝：安全閘門 + 落地 log。
# 排程於夜間 GPU-off 窗（無推論寫入）執行。SQL 邏輯見同目錄 dedup_tracking_logs.sql。
set -euo pipefail

# systemd --user 排程環境 PATH 較精簡，補上常見路徑以找到 docker/tee。
export PATH="/usr/local/bin:/usr/bin:/bin:$PATH"

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$DIR/dedup_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG") 2>&1

echo "=== dedup start $(date '+%F %T') ==="

DB="docker exec -e PGPASSWORD=pig_password pig-agri-postgres-1 psql -U pig -d pig_monitoring"

# 安全閘門：確認目前無推論寫入。tracking_logs.timestamp 是 capture_ts；
# 推論在跑時 max(timestamp)≈now，夜間 GPU-off 則為數小時前。近 120s 有寫入就中止。
AGE=$($DB -tA -c "SELECT round(extract(epoch from now()) - max(timestamp))::bigint FROM tracking_logs;")
echo "last tracking write age = ${AGE}s"
if [ "$AGE" -lt 120 ]; then
  echo "ABORT: 最近 ${AGE}s 內仍有寫入（推論可能在跑），不在無寫入窗，取消。"
  exit 1
fi

docker exec -i -e PGPASSWORD=pig_password pig-agri-postgres-1 \
  psql -U pig -d pig_monitoring -v ON_ERROR_STOP=1 < "$DIR/dedup_tracking_logs.sql"

echo "=== dedup done $(date '+%F %T') ==="
$DB -c "SELECT 'tracking_logs' AS tbl, count(*) FROM tracking_logs
        UNION ALL SELECT 'tracking_logs_old', count(*) FROM tracking_logs_old;"
echo "舊表保留為 tracking_logs_old（備份）。確認無誤後可手動 DROP TABLE tracking_logs_old; 回收空間。"
