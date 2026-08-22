CREATE TABLE IF NOT EXISTS tracking_logs (
    id                BIGSERIAL PRIMARY KEY,
    camera_id         VARCHAR(16) NOT NULL,
    timestamp         DOUBLE PRECISION NOT NULL,
    frame_id          BIGINT NOT NULL,
    object_id         INTEGER NOT NULL,
    bb_left           REAL,
    bb_top            REAL,
    bb_width          REAL,
    bb_height         REAL,
    confidence        REAL,
    thermal_intensity REAL
);
CREATE INDEX IF NOT EXISTS idx_tracking ON tracking_logs (camera_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS health_alerts (
    id            BIGSERIAL PRIMARY KEY,
    camera_id     VARCHAR(16) NOT NULL,
    object_id     INTEGER NOT NULL,
    triggered_at  TIMESTAMPTZ DEFAULT NOW(),
    metric        VARCHAR(32) NOT NULL,
    current_value REAL,
    mean_value    REAL,
    std_value     REAL,
    is_read       BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS pig_notes (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   VARCHAR(16) NOT NULL,
    object_id   INTEGER,
    note_time   TIMESTAMPTZ NOT NULL,
    content     TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    key        VARCHAR(64) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS saved_segments (
    id         BIGSERIAL PRIMARY KEY,
    camera_id  VARCHAR(16) NOT NULL,
    hour_ts    BIGINT NOT NULL,
    label      TEXT,
    note       TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    UNIQUE (camera_id, hour_ts)
);

INSERT INTO user_settings (key, value, updated_at) VALUES
    ('analysis_interval_minutes', '30', NOW()),
    ('anomaly_std_threshold', '3.0', NOW()),
    ('focus_lowest_enabled', 'true', NOW()),
    ('focus_lowest_n', '3', NOW()),
    ('focus_top_n', '3', NOW()),
    ('hls_retention_days', '90', NOW()),
    ('ntfy_url', 'https://ntfy.ed716.duckdns.org/pig', NOW()),
    ('ntfy_enabled', 'true', NOW()),
    ('ntfy_revive_priority', 'default', NOW()),
    ('gpu_off_schedule_enabled', 'false', NOW()),
    ('gpu_off_start', '22:00', NOW()),
    ('gpu_off_end', '06:00', NOW())
ON CONFLICT (key) DO NOTHING;
