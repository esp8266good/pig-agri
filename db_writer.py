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
