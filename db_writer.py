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
