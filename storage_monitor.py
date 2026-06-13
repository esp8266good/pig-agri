# storage_monitor.py
"""儲存健康監控 + 目標模式決策（record / ephemeral / drop）。

設計對齊 hls_retention.py（純函式可測）與 analysis/scheduler（遲滯狀態機）。
本模組不 import hls_manager / database（避免循環依賴、保持純函式可測）；
DB 寫入告警由 main.py 以 alert_cb 注入。
"""
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from loguru import logger


@dataclass(frozen=True)
class StorageSettings:
    check_interval_seconds: int = 20
    min_free_bytes: int = 10 * 1024**3
    min_free_inodes_ratio: float = 0.02
    debounce_count: int = 2
    volume_marker: str = ""
    schedule_enabled: bool = True
    off_start_min: int = 17 * 60          # 17:00
    off_end_min: int = 6 * 60 + 30        # 06:30


def parse_hhmm(s: str) -> int:
    """'HH:MM' → minutes-of-day（0..1439）。解析失敗回 -1。"""
    try:
        h, m = str(s).strip().split(":")
        h_i, m_i = int(h), int(m)
        if 0 <= h_i < 24 and 0 <= m_i < 60:
            return h_i * 60 + m_i
    except (ValueError, AttributeError):
        pass
    return -1


def is_recording_time(now: datetime, off_start_min: int, off_end_min: int,
                      enabled: bool) -> bool:
    """now 是否落在「錄影時段」（no-record 窗之外）。停用/無效/空窗 → 永遠錄。
    跨午夜：off 17:00→06:30 ⇒ 錄影 ON 僅 06:30–17:00。"""
    if not enabled:
        return True
    if off_start_min < 0 or off_end_min < 0 or off_start_min == off_end_min:
        return True
    cur = now.hour * 60 + now.minute
    if off_start_min <= off_end_min:
        in_off = off_start_min <= cur < off_end_min
    else:
        in_off = cur >= off_start_min or cur < off_end_min
    return not in_off


def check_free_space(path) -> tuple[int, float, float]:
    """(free_bytes, free_ratio, free_inodes_ratio)。路徑不存在 → OSError。"""
    st = os.statvfs(str(path))
    free_bytes = st.f_bavail * st.f_frsize
    total_bytes = st.f_blocks * st.f_frsize
    free_ratio = (free_bytes / total_bytes) if total_bytes else 0.0
    free_inodes_ratio = (st.f_favail / st.f_files) if st.f_files else 1.0
    return free_bytes, free_ratio, free_inodes_ratio


def classify_health(probe_ok: bool, marker_ok: bool, free_bytes: int,
                    free_inodes_ratio: float, settings: StorageSettings) -> str:
    """probe/marker 任一失敗 → down；空間或 inode 低於門檻 → degraded；否則 ok。"""
    if not probe_ok or not marker_ok:
        return "down"
    if (free_bytes < settings.min_free_bytes
            or free_inodes_ratio < settings.min_free_inodes_ratio):
        return "degraded"
    return "ok"


def next_state(current: str, reading: str, count: int, debounce: int) -> tuple[str, int]:
    """遲滯：需連續 debounce 次 reading != current 才翻轉。回 (new_state, new_count)。"""
    if reading == current:
        return current, 0
    count += 1
    if count >= debounce:
        return reading, 0
    return current, count
