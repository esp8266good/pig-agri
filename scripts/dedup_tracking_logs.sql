-- 去重 tracking_logs：移除「凍結畫面重灌」產生的重複列。
-- 根因見 docs/handoff-tracking-gap-2026-07-20.md。
-- 去重鍵：(camera_id, frame_id, object_id, timestamp)——必須含 timestamp，因為 frame_id
-- 是 per-camera 計數器、會隨送幀端重啟回繞重用，不同真實幀會共用 frame_id；真正的 dupe
-- 連 timestamp 都完全相同。每組保留 id 最小（最早寫入）的一列。
--
-- 做法：CTAS 建新表 → 建索引/PK → sanity 檢查 → 原子 RENAME 換表。整段單一交易，
-- 失敗自動 rollback、原表不動。舊表改名 tracking_logs_old 保留作備份（本腳本不 DROP）。
-- 必須在「無推論寫入」時段執行（夜間 GPU-off 窗 18:00–06:00），避免換表期間漏寫。

\set ON_ERROR_STOP on

BEGIN;

SET LOCAL statement_timeout = 0;
SET LOCAL work_mem = '512MB';
SET LOCAL maintenance_work_mem = '1GB';

-- 防呆：若上次殘留的中間表/備份表還在，中止（避免覆蓋既有備份）。
DO $$
BEGIN
  IF to_regclass('public.tracking_logs_new') IS NOT NULL THEN
    RAISE EXCEPTION 'tracking_logs_new 已存在，請先清掉上次殘留再執行';
  END IF;
  IF to_regclass('public.tracking_logs_old') IS NOT NULL THEN
    RAISE EXCEPTION 'tracking_logs_old 已存在（疑似已跑過），請先確認/移除再執行';
  END IF;
END $$;

CREATE TABLE tracking_logs_new (LIKE tracking_logs INCLUDING DEFAULTS);

INSERT INTO tracking_logs_new
SELECT DISTINCT ON (camera_id, frame_id, object_id, timestamp) *
FROM tracking_logs
ORDER BY camera_id, frame_id, object_id, timestamp, id;

ALTER TABLE tracking_logs_new ADD CONSTRAINT tracking_logs_new_pkey PRIMARY KEY (id);
CREATE INDEX idx_tracking_new ON tracking_logs_new (camera_id, "timestamp" DESC);

-- sanity：新表列數必須 >0 且 < 舊表，否則中止（交易 rollback）。
DO $$
DECLARE n_old bigint; n_new bigint;
BEGIN
  SELECT count(*) INTO n_old FROM tracking_logs;
  SELECT count(*) INTO n_new FROM tracking_logs_new;
  RAISE NOTICE 'tracking_logs: old=% new=% removed=%', n_old, n_new, n_old - n_new;
  IF n_new = 0 OR n_new >= n_old THEN
    RAISE EXCEPTION 'sanity 檢查失敗 old=% new=%', n_old, n_new;
  END IF;
END $$;

-- 原子換表。
ALTER TABLE tracking_logs RENAME TO tracking_logs_old;
ALTER INDEX idx_tracking RENAME TO idx_tracking_old;
ALTER TABLE tracking_logs_old RENAME CONSTRAINT tracking_logs_pkey TO tracking_logs_old_pkey;

ALTER TABLE tracking_logs_new RENAME TO tracking_logs;
ALTER INDEX idx_tracking_new RENAME TO idx_tracking;
ALTER TABLE tracking_logs RENAME CONSTRAINT tracking_logs_new_pkey TO tracking_logs_pkey;

COMMIT;

-- 換表後回收舊表死空間需另跑（在交易外）；此處僅對新表更新統計。
ANALYZE tracking_logs;
