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


def is_inference_active(now: datetime, off_start_min: int, off_end_min: int,
                        enabled: bool) -> bool:
    """now 是否在「GPU 推論開啟時段」（gpu_off 窗之外）。停用/無效/空窗 →
    永遠 active。語意與 is_recording_time 相同（皆判斷『是否在 off 窗外』）。"""
    return is_recording_time(now, off_start_min, off_end_min, enabled)


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


def write_probe(base_dir) -> bool:
    """在 base_dir 寫極小探針檔 → fsync → 刪除；任何 OSError → False。
    一次抓到唯讀 remount / 掛載消失 / 權限不足。"""
    base = Path(base_dir)
    probe = base / ".storage_probe"
    try:
        base.mkdir(parents=True, exist_ok=True)
        with open(probe, "wb") as fh:
            fh.write(str(time.time()).encode())
            fh.flush()
            os.fsync(fh.fileno())
        probe.unlink()
        return True
    except OSError:
        return False


def marker_present(base_dir, marker: str) -> bool:
    """掛載防誤判：marker 為空＝不檢查（回 True）；否則該標記檔須存在於 base_dir。
    USB 碟 unmount 後目錄變回 root fs 空目錄、probe 仍可寫 → 靠 marker 抓出。"""
    if not marker:
        return True
    return (Path(base_dir) / marker).exists()


def effective_ephemeral_dir(configured: str,
                            fallback: str = "data/pig_monitoring/hls_live") -> str:
    """configured 指向 /dev/shm 但該路徑不可用 → 回退 fallback（系統碟）。"""
    if configured.startswith("/dev/shm") and not os.path.isdir("/dev/shm"):
        logger.warning(f"/dev/shm 不可用，ephemeral live 改用 {fallback}")
        return fallback
    return configured


def _coerce_float(v, default: float) -> float:
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _coerce_int(v, default: int) -> int:
    try:
        return int(float(v))
    except (ValueError, TypeError):
        return default


def _coerce_bool(v, default: bool) -> bool:
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    return str(v).strip().lower() == "true"


def resolve_settings(db: "dict | None", app_settings) -> StorageSettings:
    """合併 DB（前端可調）與 app_settings（建構時 .env/預設）→ StorageSettings。
    DB 有值且可解析 → 用 DB；否則回退 app_settings。"""
    def g(key, default):
        if db and key in db and db[key] is not None:
            return db[key]
        return default

    min_gb = _coerce_float(
        g("storage_min_free_gb", app_settings.storage_min_free_gb),
        app_settings.storage_min_free_gb,
    )
    return StorageSettings(
        check_interval_seconds=_coerce_int(
            g("storage_check_interval_seconds", app_settings.storage_check_interval_seconds),
            app_settings.storage_check_interval_seconds),
        min_free_bytes=int(min_gb * 1024**3),
        min_free_inodes_ratio=_coerce_float(
            g("storage_min_free_inodes_ratio", app_settings.storage_min_free_inodes_ratio),
            app_settings.storage_min_free_inodes_ratio),
        debounce_count=_coerce_int(
            g("storage_debounce_count", app_settings.storage_debounce_count),
            app_settings.storage_debounce_count),
        volume_marker=str(g("storage_volume_marker", app_settings.storage_volume_marker) or ""),
        schedule_enabled=_coerce_bool(
            g("recording_schedule_enabled", app_settings.recording_schedule_enabled),
            app_settings.recording_schedule_enabled),
        off_start_min=parse_hhmm(str(g("recording_off_start", app_settings.recording_off_start))),
        off_end_min=parse_hhmm(str(g("recording_off_end", app_settings.recording_off_end))),
    )


def resolve_gpu_active(db: "dict | None", app_settings, now: datetime) -> bool:
    """合併 DB（前端可調）與 app_settings → 算當下 GPU 推論是否該開啟。"""
    def g(key, default):
        if db and key in db and db[key] is not None:
            return db[key]
        return default

    enabled = _coerce_bool(
        g("gpu_off_schedule_enabled", app_settings.gpu_off_schedule_enabled),
        app_settings.gpu_off_schedule_enabled)
    start = parse_hhmm(str(g("gpu_off_start", app_settings.gpu_off_start)))
    end = parse_hhmm(str(g("gpu_off_end", app_settings.gpu_off_end)))
    return is_inference_active(now, start, end, enabled)


class StorageMonitor:
    """維護錄影碟/ephemeral 碟兩個遲滯狀態，合成單一 target_mode。
    feed/writer 以 get_target_mode() 讀取（cheap、有鎖）；背景 loop 呼叫 run_once。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._record_state = "ok"
        self._eph_state = "ok"
        self._record_count = 0
        self._eph_count = 0
        self._target_mode = "record"   # 啟動預設＝現狀錄影
        self._prev_target_mode = "record"
        self._snapshot: dict = {
            "recording_state": "ok", "ephemeral_state": "ok",
            "target_mode": "record", "recording_time": True,
            "recording_free_gb": 0.0, "recording_free_ratio": 0.0,
            "ephemeral_free_gb": 0.0, "last_transition_ts": None,
        }

    def get_target_mode(self) -> str:
        with self._lock:
            return self._target_mode

    def get_snapshot(self) -> dict:
        with self._lock:
            return dict(self._snapshot)

    def _read_base(self, base, settings: StorageSettings, check_marker: bool):
        probe = write_probe(base)
        marker = marker_present(base, settings.volume_marker) if check_marker else True
        try:
            free_bytes, free_ratio, free_inodes_ratio = check_free_space(base)
        except OSError:
            free_bytes, free_ratio, free_inodes_ratio, probe = 0, 0.0, 0.0, False
        reading = classify_health(probe, marker, free_bytes, free_inodes_ratio, settings)
        return reading, free_bytes, free_ratio

    async def run_once(self, *, recording_base, ephemeral_base,
                       settings: StorageSettings, now: datetime, alert_cb) -> None:
        rec_reading, rec_free, rec_ratio = self._read_base(recording_base, settings, True)
        eph_reading, eph_free, _ = self._read_base(ephemeral_base, settings, False)
        recording_time = is_recording_time(
            now, settings.off_start_min, settings.off_end_min, settings.schedule_enabled)

        with self._lock:
            prev_record = self._record_state
            self._record_state, self._record_count = next_state(
                self._record_state, rec_reading, self._record_count, settings.debounce_count)
            self._eph_state, self._eph_count = next_state(
                self._eph_state, eph_reading, self._eph_count, settings.debounce_count)
            new_record = self._record_state

            rec_writable = new_record != "down"
            eph_writable = self._eph_state != "down"
            if rec_writable and recording_time:
                mode = "record"
            elif eph_writable:
                mode = "ephemeral"
            else:
                mode = "drop"
            self._target_mode = mode

            transitioned = prev_record != new_record
            self._snapshot = {
                "recording_state": new_record,
                "ephemeral_state": self._eph_state,
                "target_mode": mode,
                "recording_time": recording_time,
                "recording_free_gb": round(rec_free / 1024**3, 2),
                "recording_free_ratio": round(rec_ratio, 4),
                "ephemeral_free_gb": round(eph_free / 1024**3, 2),
                "last_transition_ts": (now.timestamp() if transitioned
                                       else self._snapshot.get("last_transition_ts")),
            }

        if transitioned and alert_cb is not None:
            # 註：down→degraded（探針恢復但空間仍低）刻意不發告警——只在完全
            # 恢復 ok 才發 storage_recovered，避免半恢復狀態洗告警。
            min_gb = settings.min_free_bytes / 1024**3
            free_gb = rec_free / 1024**3
            if new_record == "degraded" and prev_record == "ok":
                await alert_cb("storage_low_space", free_gb, min_gb)
            elif new_record == "down":
                await alert_cb("storage_unwritable", free_gb, min_gb)
            elif new_record == "ok" and prev_record != "ok":
                await alert_cb("storage_recovered", free_gb, min_gb)

        # target_mode 轉換告警（與 record 狀態告警獨立）：排程型暫停/恢復錄影。
        # 故障型 ephemeral（record 狀態 down）已由 storage_unwritable 涵蓋，
        # 故 recording_paused 僅在 record 狀態仍 ok 時發（＝純排程造成）。
        prev_mode = self._prev_target_mode
        self._prev_target_mode = mode
        if alert_cb is not None and mode != prev_mode:
            if mode == "ephemeral" and prev_mode == "record" and new_record == "ok":
                await alert_cb("recording_paused", rec_free / 1024**3, 0.0)
            elif mode == "record" and prev_mode == "ephemeral":
                await alert_cb("recording_resumed", rec_free / 1024**3, 0.0)


monitor = StorageMonitor()


def get_target_mode() -> str:
    """hls_manager feed/writer 的廉價讀取點。"""
    return monitor.get_target_mode()
