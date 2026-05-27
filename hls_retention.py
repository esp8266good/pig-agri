"""HLS 片段循環刪除（retention）。

`hls_base_dir/<camera>/<stream_type>/<YYYY-MM-DD-HH>/` 下的小時目錄由
`hls_manager` 每整點輪替產生、但從來沒有任何機制清掉（`hls_retention_days`
一直是死設定）。本模組掃出超過保留天數的小時目錄並刪除，讓磁碟用量有界。

設計刻意只認得「`%Y-%m-%d-%H` 命名的小時目錄」，其餘一律略過——絕不誤刪
非預期目錄；且以目錄名解析出的牆鐘時間判斷，與 hls_manager 建目錄用的
`datetime.now()`（本地時區）一致。
"""

import shutil
from datetime import datetime, timedelta
from pathlib import Path

from loguru import logger

_HOUR_FMT = "%Y-%m-%d-%H"


def find_expired_hour_dirs(
    base_dir, retention_days: float, now: datetime
) -> list[Path]:
    """回傳 base_dir 下早於 (now - retention_days) 的小時目錄清單。
    base_dir 不存在或無合格目錄回 []。"""
    base = Path(base_dir)
    if not base.is_dir():
        return []
    cutoff = now - timedelta(days=retention_days)
    expired: list[Path] = []
    # 結構：base/<camera>/<stream_type>/<YYYY-MM-DD-HH>
    for cam_dir in base.iterdir():
        if not cam_dir.is_dir():
            continue
        for type_dir in cam_dir.iterdir():
            if not type_dir.is_dir():
                continue
            for hour_dir in type_dir.iterdir():
                if not hour_dir.is_dir():
                    continue
                try:
                    dt = datetime.strptime(hour_dir.name, _HOUR_FMT)
                except ValueError:
                    continue  # 非小時命名 → 略過，絕不誤刪
                if dt < cutoff:
                    expired.append(hour_dir)
    return expired


def purge_expired_hls(
    base_dir, retention_days: float, now: datetime | None = None
) -> list[Path]:
    """刪除所有過期小時目錄，回傳實際刪除的目錄清單。"""
    now = now or datetime.now()
    expired = find_expired_hour_dirs(base_dir, retention_days, now)
    deleted: list[Path] = []
    for d in expired:
        try:
            shutil.rmtree(d)
            deleted.append(d)
        except OSError as e:
            logger.warning(f"HLS retention：刪除 {d} 失敗：{e}")
    if deleted:
        logger.info(f"HLS retention：刪除 {len(deleted)} 個過期小時目錄（>{retention_days}d）")
    return deleted


def effective_retention_days(
    db_settings: dict | None, fallback_days: float
) -> float:
    """DB 有 hls_retention_days 且可解析 → 用 DB 值（單一權威）；
    否則回退 fallback_days（呼叫端傳入 app_settings 建構時值）。"""
    if db_settings is not None:
        raw = db_settings.get("hls_retention_days")
        if raw is not None:
            try:
                return float(raw)
            except (ValueError, TypeError):
                pass
    return float(fallback_days)
