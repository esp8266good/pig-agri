from fastapi import APIRouter, HTTPException, Request

import database
from config import settings as app_settings
from db_writer import get_all_settings, upsert_settings

router = APIRouter(prefix="/settings", tags=["settings"])

ALLOWED_KEYS = frozenset({
    "analysis_interval_minutes",
    "analysis_window_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
    "temp_anomaly_enabled",
    # 儲存健康監控（storage_monitor loop 每輪讀 DB → 即時生效、不需 reload）
    "storage_check_interval_seconds",
    "storage_min_free_gb",
    "storage_min_free_inodes_ratio",
    "storage_debounce_count",
    "storage_volume_marker",
    # 夜間 no-record 排程
    "recording_schedule_enabled",
    "recording_off_start",
    "recording_off_end",
})

_RELOAD_KEYS = {
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "analysis_window_minutes",
    "temp_anomaly_enabled",
}


def _as_bool(value: str) -> bool:
    return str(value).strip().lower() == "true"


@router.get("")
async def get_settings():
    pool = database.get_pool()
    if pool is None:
        return {
            "analysis_interval_minutes":   str(app_settings.analysis_interval_minutes),
            "analysis_window_minutes":     str(app_settings.analysis_window_minutes),
            "anomaly_std_threshold":       str(app_settings.anomaly_std_threshold),
            "hls_retention_days":          str(app_settings.hls_retention_days),
            "temp_anomaly_enabled":        str(app_settings.temp_anomaly_enabled).lower(),
            "storage_check_interval_seconds": str(app_settings.storage_check_interval_seconds),
            "storage_min_free_gb":            str(app_settings.storage_min_free_gb),
            "storage_min_free_inodes_ratio":  str(app_settings.storage_min_free_inodes_ratio),
            "storage_debounce_count":         str(app_settings.storage_debounce_count),
            "storage_volume_marker":          app_settings.storage_volume_marker,
            "recording_schedule_enabled":     str(app_settings.recording_schedule_enabled).lower(),
            "recording_off_start":            app_settings.recording_off_start,
            "recording_off_end":              app_settings.recording_off_end,
        }
    return await get_all_settings(pool)


@router.put("")
async def update_settings(request: Request, body: dict[str, str]):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates = {k: v for k, v in body.items() if k in ALLOWED_KEYS}
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    if _RELOAD_KEYS & updates.keys():
        current = await get_all_settings(pool)
        request.app.state.scheduler.reload(
            interval_minutes=int(current.get(
                "analysis_interval_minutes", app_settings.analysis_interval_minutes)),
            std_threshold=float(current.get(
                "anomaly_std_threshold", app_settings.anomaly_std_threshold)),
            window_minutes=int(current.get(
                "analysis_window_minutes", app_settings.analysis_window_minutes)),
            temp_anomaly_enabled=_as_bool(current.get(
                "temp_anomaly_enabled", str(app_settings.temp_anomaly_enabled).lower())),
        )
    return {"ok": True, "updated": list(updates.keys())}
