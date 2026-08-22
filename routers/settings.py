import ipaddress
import re
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Request

import database
from inference.pipeline import inference_pipeline
from config import settings as app_settings
from db_writer import get_all_settings, upsert_settings

router = APIRouter(prefix="/settings", tags=["settings"])

ALLOWED_KEYS = frozenset({
    "analysis_interval_minutes",
    "analysis_window_minutes",
    "anomaly_std_threshold",
    "hls_retention_days",
    "temp_anomaly_enabled",
    # 關注清單
    "focus_lowest_enabled",
    "focus_lowest_n",
    "focus_top_n",
    "mask_enabled",
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
    # ntfy 推播
    "ntfy_url",
    "ntfy_enabled",
    "ntfy_revive_priority",
    # 夜間停 GPU 排程
    "gpu_off_schedule_enabled",
    "gpu_off_start",
    "gpu_off_end",
})

_RELOAD_KEYS = {
    "analysis_interval_minutes",
    "anomaly_std_threshold",
    "analysis_window_minutes",
    "temp_anomaly_enabled",
}

# ── 值域檢查 ────────────────────────────────────────────────────────
# ALLOWED_KEYS 只擋 key，不看 value；但這些值會直接驅動破壞性行為，最狠的是
# hls_retention_days=0 → hls_retention 的 cutoff 變成「現在」→ 下一輪巡檢
# （最多 1 小時）把所有未受保護的小時目錄 rmtree 掉。上下界刻意對齊
# static/index.html 各 input 的 min/max，正常操作絕不會撞到 400。

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")

# key → (型別, 下界, 上界)，皆含端點。
_NUMERIC_BOUNDS: dict[str, tuple] = {
    "analysis_interval_minutes":      (int,   1,   1440),
    "analysis_window_minutes":        (int,   1,   10080),
    "anomaly_std_threshold":          (float, 0.1, 100.0),
    # 上界 20 是為了擋「打 999 把整欄列出來」，關注清單就失去意義了。
    "focus_lowest_n":                 (int,   1,   20),
    # 下界 0 = 關閉對照組。
    "focus_top_n":                    (int,   0,   20),
    "hls_retention_days":             (int,   1,   3650),
    "storage_check_interval_seconds": (int,   5,   86400),
    "storage_min_free_gb":            (float, 0.0, 1_000_000.0),
    "storage_min_free_inodes_ratio":  (float, 0.0, 1.0),
    "storage_debounce_count":         (int,   1,   1000),
}

_BOOL_KEYS = frozenset({
    "temp_anomaly_enabled", "recording_schedule_enabled",
    "ntfy_enabled", "gpu_off_schedule_enabled",
})

_HHMM_KEYS = frozenset({
    "recording_off_start", "recording_off_end", "gpu_off_start", "gpu_off_end",
})

_NTFY_PRIORITIES = frozenset({"min", "low", "default", "high", "max", "urgent"})

# 這兩個 key 的空字串是有意義的（＝不啟用推播／不檢查掛載標記），要放行。
_EMPTY_OK = frozenset({"ntfy_url", "storage_volume_marker"})


def _is_internal_host(host: str) -> bool:
    """本機／私有／link-local／保留位址 → True。

    只看字面值、不做 DNS 解析：解析會把一次存檔變成阻塞事件迴圈的網路 I/O，
    而且 TTL 一過就失效（DNS rebinding 本來就擋不住）。字面 IP 是實務上唯一
    好用的打法，擋住它就夠。
    """
    h = host.strip("[]").lower()
    if h == "localhost" or h.endswith((".localhost", ".local")):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return (ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified)


def _validate_ntfy_url(raw: str) -> None:
    """ntfy_url 會被 ntfy_notifier.notify 直接 POST 出去，等於讓呼叫端指定
    「後端要連哪台主機」。不擋的話這個端點就是一支 SSRF：任何能打到 /settings
    的人都能叫後端去戳內網服務（例如 http://192.168.x.x/... 或 169.254.169.254）。
    """
    if raw == "":
        return   # 空＝不推播，由 ntfy_notifier 自行 no-op
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("ntfy_url 必須以 http:// 或 https:// 開頭")
    host = parsed.hostname
    if not host:
        raise ValueError("ntfy_url 缺少主機名稱")
    if _is_internal_host(host):
        raise ValueError("ntfy_url 不可指向本機或私有網段位址")
    if not parsed.path.strip("/"):
        raise ValueError("ntfy_url 結尾需要 topic 路徑，例如 https://ntfy.example.com/pig")


def validate_setting(key: str, value: str) -> None:
    """單一設定的值域檢查。不合法丟 ValueError，訊息會原樣回給前端顯示。"""
    raw = str(value).strip()
    if key in _NUMERIC_BOUNDS:
        caster, lo, hi = _NUMERIC_BOUNDS[key]
        try:
            n = caster(raw)
        except (TypeError, ValueError):
            kind = "整數" if caster is int else "數字"
            raise ValueError(f"{key} 必須是{kind}（收到 '{value}'）")
        if not (lo <= n <= hi):
            raise ValueError(f"{key} 必須介於 {lo} 與 {hi} 之間（收到 '{value}'）")
    elif key in _BOOL_KEYS:
        if raw.lower() not in ("true", "false"):
            raise ValueError(f"{key} 必須是 true 或 false（收到 '{value}'）")
    elif key in _HHMM_KEYS:
        if not _HHMM_RE.match(raw):
            raise ValueError(f"{key} 必須是 HH:MM 24 小時制（收到 '{value}'）")
    elif key == "ntfy_revive_priority":
        if raw.lower() not in _NTFY_PRIORITIES:
            raise ValueError("ntfy_revive_priority 必須是 min/low/default/high/max/urgent 之一")
    elif key == "ntfy_url":
        _validate_ntfy_url(raw)
    elif key == "storage_volume_marker":
        # marker 會被 storage_monitor.marker_present 接在錄影碟路徑後面做存在
        # 檢查，限制成單一檔名避免探測掛載點以外的路徑。
        if "/" in raw or "\\" in raw or raw in (".", ".."):
            raise ValueError("storage_volume_marker 必須是單一檔名，不可含路徑分隔符")


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
            "focus_lowest_enabled":        str(app_settings.focus_lowest_enabled).lower(),
            "focus_lowest_n":              str(app_settings.focus_lowest_n),
            "focus_top_n":                 str(app_settings.focus_top_n),
            "mask_enabled":                str(app_settings.mask_enabled).lower(),
            "storage_check_interval_seconds": str(app_settings.storage_check_interval_seconds),
            "storage_min_free_gb":            str(app_settings.storage_min_free_gb),
            "storage_min_free_inodes_ratio":  str(app_settings.storage_min_free_inodes_ratio),
            "storage_debounce_count":         str(app_settings.storage_debounce_count),
            "storage_volume_marker":          app_settings.storage_volume_marker,
            "recording_schedule_enabled":     str(app_settings.recording_schedule_enabled).lower(),
            "recording_off_start":            app_settings.recording_off_start,
            "recording_off_end":              app_settings.recording_off_end,
            "ntfy_url":                       app_settings.ntfy_url,
            "ntfy_enabled":                   str(app_settings.ntfy_enabled).lower(),
            "ntfy_revive_priority":           app_settings.ntfy_revive_priority,
            "gpu_off_schedule_enabled":       str(app_settings.gpu_off_schedule_enabled).lower(),
            "gpu_off_start":                  app_settings.gpu_off_start,
            "gpu_off_end":                    app_settings.gpu_off_end,
        }
    return await get_all_settings(pool)


@router.put("")
async def update_settings(request: Request, body: dict[str, str]):
    pool = database.get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail="Database not available")
    updates: dict[str, str] = {}
    for k, v in body.items():
        if k not in ALLOWED_KEYS:
            continue
        # 空字串在數值/開關/時間欄位沒有意義（前端只有在使用者手動清空時才會送）。
        # 略過不寫、保留既有值，而不是回 400 讓整次存檔看起來失敗。
        if str(v).strip() == "" and k not in _EMPTY_OK:
            continue
        try:
            validate_setting(k, v)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        updates[k] = v
    if not updates:
        raise HTTPException(status_code=400, detail="No valid keys provided")
    await upsert_settings(pool, updates)
    # 遮罩總開關要立刻生效：它的用途是「遮罩把真的豬吃掉了，馬上關掉」，
    # 等下次重啟才生效等於沒有這個開關。
    if "mask_enabled" in updates:
        inference_pipeline.set_mask_enabled(
            str(updates["mask_enabled"]).strip().lower() == "true")

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
