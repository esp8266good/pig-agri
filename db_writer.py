"""Database writer module for tracking logs."""

from typing import Optional
import asyncpg


async def write_tracking_log(
    pool: asyncpg.Pool,
    *,
    camera_id: str,
    timestamp: float,
    frame_id: int,
    object_id: int,
    bb_left: float,
    bb_top: float,
    bb_width: float,
    bb_height: float,
    confidence: float,
    thermal_intensity: Optional[float],
) -> None:
    """Write a single tracking log entry to the database.

    Args:
        pool: asyncpg connection pool
        camera_id: Camera identifier
        timestamp: Unix timestamp
        frame_id: Frame number
        object_id: Object identifier
        bb_left: Bounding box left coordinate
        bb_top: Bounding box top coordinate
        bb_width: Bounding box width
        bb_height: Bounding box height
        confidence: Detection confidence score
        thermal_intensity: Optional thermal intensity value
    """
    # timestamp: Unix epoch seconds stored as DOUBLE PRECISION
    sql = """
    INSERT INTO tracking_logs
    (camera_id, timestamp, frame_id, object_id, bb_left, bb_top, bb_width, bb_height, confidence, thermal_intensity)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    """

    await pool.execute(
        sql,
        camera_id,
        timestamp,
        frame_id,
        object_id,
        bb_left,
        bb_top,
        bb_width,
        bb_height,
        confidence,
        thermal_intensity,
    )


async def query_tracking_logs(
    pool: asyncpg.Pool,
    camera_id: str,
    start: float,
    end: float,
    object_id: Optional[int] = None,
) -> list[dict]:
    """Query tracking logs for a camera in a time range.

    timestamp is stored as DOUBLE PRECISION (Unix epoch seconds).

    Args:
        pool: asyncpg connection pool
        camera_id: Camera identifier
        start: Start timestamp (inclusive)
        end: End timestamp (exclusive)
        object_id: Optional object identifier filter

    Returns:
        List of dicts with keys: object_id, bbox, confidence, timestamp, frame_id
    """
    filters = "camera_id=$1 AND timestamp >= $2 AND timestamp < $3"
    params: list = [camera_id, start, end]
    if object_id is not None:
        filters += " AND object_id=$4"
        params.append(object_id)

    sql = f"""SELECT object_id, bb_left, bb_top, bb_width, bb_height,
                     confidence, timestamp, frame_id
              FROM tracking_logs
              WHERE {filters}
              ORDER BY timestamp"""

    rows = await pool.fetch(sql, *params)

    result = []
    for row in rows:
        result.append({
            "object_id": row["object_id"],
            "bbox": [row["bb_left"], row["bb_top"], row["bb_width"], row["bb_height"]],
            "confidence": row["confidence"],
            "timestamp": row["timestamp"],
            "frame_id": row["frame_id"],
        })

    return result


async def query_timeline_hours(
    pool: asyncpg.Pool,
    camera_id: str,
    start_ts: float,
    end_ts: float,
) -> list[int]:
    """Query distinct hours with tracking data for a camera.

    Args:
        pool: asyncpg connection pool
        camera_id: Camera identifier
        start_ts: Start timestamp (inclusive)
        end_ts: End timestamp (exclusive)

    Returns:
        List of Unix hour timestamps (integers)
    """
    sql = """
    SELECT DISTINCT CAST(floor(timestamp / 3600) * 3600 AS BIGINT) AS hour
    FROM tracking_logs
    WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3
    ORDER BY hour
    """

    rows = await pool.fetch(sql, camera_id, start_ts, end_ts)
    return [int(row["hour"]) for row in rows]


async def write_health_alert(
    pool: asyncpg.Pool,
    *,
    camera_id: str,
    object_id: int,
    metric: str,
    current_value: float,
    mean_value: float,
    std_value: float,
) -> int:
    row = await pool.fetchrow(
        """INSERT INTO health_alerts
           (camera_id, object_id, metric, current_value, mean_value, std_value)
           VALUES ($1, $2, $3, $4, $5, $6)
           RETURNING id""",
        camera_id, object_id, metric, current_value, mean_value, std_value,
    )
    return row["id"]


async def query_health_alerts(
    pool: asyncpg.Pool,
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> list[dict]:
    conditions = []
    params: list = []
    idx = 1

    if camera_id is not None:
        conditions.append(f"camera_id=${idx}")
        params.append(camera_id)
        idx += 1
    if unread_only:
        conditions.append("is_read = FALSE")
    if start_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) >= ${idx}")
        params.append(start_ts)
        idx += 1
    if end_ts is not None:
        conditions.append(f"EXTRACT(EPOCH FROM triggered_at) < ${idx}")
        params.append(end_ts)
        idx += 1

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.append(limit)
    limit_ph = f"${idx}"

    sql = f"""
        SELECT id, camera_id, object_id, metric,
               current_value, mean_value, std_value, is_read,
               EXTRACT(EPOCH FROM triggered_at)::float AS triggered_at_unix
        FROM health_alerts
        {where}
        ORDER BY triggered_at DESC
        LIMIT {limit_ph}
    """
    rows = await pool.fetch(sql, *params)
    return [dict(r) for r in rows]


async def mark_alert_read(pool: asyncpg.Pool, alert_id: int) -> bool:
    result = await pool.execute(
        "UPDATE health_alerts SET is_read = TRUE WHERE id = $1",
        alert_id,
    )
    return result != "UPDATE 0"


async def get_all_settings(pool: asyncpg.Pool) -> dict[str, str]:
    rows = await pool.fetch("SELECT key, value FROM user_settings")
    return {r["key"]: r["value"] for r in rows}


async def upsert_settings(pool: asyncpg.Pool, updates: dict[str, str]) -> None:
    await pool.executemany(
        """INSERT INTO user_settings (key, value, updated_at)
           VALUES ($1, $2, NOW())
           ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()""",
        [(k, v) for k, v in updates.items()],
    )


async def list_saved_segments(
    pool: asyncpg.Pool, camera_id: str, start_ts: float, end_ts: float
) -> list[dict]:
    rows = await pool.fetch(
        """SELECT id, camera_id, hour_ts, label, note
           FROM saved_segments
           WHERE camera_id=$1 AND hour_ts >= $2 AND hour_ts < $3
           ORDER BY hour_ts""",
        camera_id, int(start_ts), int(end_ts),
    )
    return [dict(r) for r in rows]


async def list_bookmarks(
    pool: asyncpg.Pool, camera_id: Optional[str] = None
) -> list[dict]:
    if camera_id is not None:
        rows = await pool.fetch(
            """SELECT id, camera_id, hour_ts, label, note
               FROM saved_segments
               WHERE label IS NOT NULL AND camera_id=$1
               ORDER BY hour_ts DESC""",
            camera_id,
        )
    else:
        rows = await pool.fetch(
            """SELECT id, camera_id, hour_ts, label, note
               FROM saved_segments
               WHERE label IS NOT NULL
               ORDER BY hour_ts DESC""",
        )
    return [dict(r) for r in rows]


async def upsert_saved_segment(
    pool: asyncpg.Pool,
    camera_id: str,
    hour_ts: int,
    label: Optional[str] = None,
    note: Optional[str] = None,
) -> int:
    """label/note 為 None 時 COALESCE 保留既有值（保留動作不覆蓋既有書籤）；
    非 None 則設定/覆蓋（書籤動作）。回傳 row id。"""
    row = await pool.fetchrow(
        """INSERT INTO saved_segments (camera_id, hour_ts, label, note)
           VALUES ($1, $2, $3, $4)
           ON CONFLICT (camera_id, hour_ts) DO UPDATE
             SET label = COALESCE($3, saved_segments.label),
                 note  = COALESCE($4, saved_segments.note)
           RETURNING id""",
        camera_id, int(hour_ts), label, note,
    )
    return row["id"]


async def update_saved_segment(
    pool: asyncpg.Pool, seg_id: int, label: Optional[str], note: Optional[str]
) -> bool:
    """明確 SET（label 可被設成 NULL → 降級成純保留）。"""
    status = await pool.execute(
        "UPDATE saved_segments SET label=$2, note=$3 WHERE id=$1",
        seg_id, label, note,
    )
    return status != "UPDATE 0"


async def delete_saved_segment(pool: asyncpg.Pool, seg_id: int) -> bool:
    status = await pool.execute(
        "DELETE FROM saved_segments WHERE id=$1", seg_id
    )
    return status != "DELETE 0"


async def get_protected_hours(pool: asyncpg.Pool) -> set[tuple[str, int]]:
    rows = await pool.fetch("SELECT camera_id, hour_ts FROM saved_segments")
    return {(r["camera_id"], int(r["hour_ts"])) for r in rows}


async def delete_saved_segments_by_hours(
    pool: asyncpg.Pool, camera_id: str, hours: list[int]
) -> int:
    status = await pool.execute(
        "DELETE FROM saved_segments WHERE camera_id=$1 AND hour_ts = ANY($2)",
        camera_id, [int(h) for h in hours],
    )
    return int(status.split()[-1]) if status else 0


async def delete_recordings_in_range(
    pool: asyncpg.Pool, camera_id: str, start_ts: float, end_ts: float
) -> dict:
    """刪該時段的 DB 軌跡與告警，回傳各自刪除列數。"""
    tl_status = await pool.execute(
        """DELETE FROM tracking_logs
           WHERE camera_id=$1 AND timestamp >= $2 AND timestamp < $3""",
        camera_id, start_ts, end_ts,
    )
    ha_status = await pool.execute(
        """DELETE FROM health_alerts
           WHERE camera_id=$1
             AND EXTRACT(EPOCH FROM triggered_at) >= $2
             AND EXTRACT(EPOCH FROM triggered_at) < $3""",
        camera_id, start_ts, end_ts,
    )
    return {
        "tracking_logs": int(tl_status.split()[-1]) if tl_status else 0,
        "health_alerts": int(ha_status.split()[-1]) if ha_status else 0,
    }
