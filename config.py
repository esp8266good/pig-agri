from __future__ import annotations

from dataclasses import dataclass
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict


class _NonJsonDotEnvSource(DotEnvSettingsSource):
    """Disable JSON-decoding so comma/semicolon-separated strings reach the validator."""

    def field_is_complex(self, field):  # type: ignore[override]
        return False


# ================================================================
# ZMQ Source 資料結構
# ================================================================
@dataclass(frozen=True)
class ZmqSource:
    name:      str
    src_host:  str
    src_port:  int
    src_topic: str
    label:     str   # HLS / inference 的辨識標籤

    @classmethod
    def from_str(cls, raw: str) -> "ZmqSource":
        """
        解析單一 source 字串。
        格式：name:host:port:src_topic:label
        範例：rpi_local:192.168.50.5:5555:rpi_sensors:cam_01
        """
        parts = [p.strip() for p in raw.split(":")]
        if len(parts) != 5:
            raise ValueError(
                f"ZMQ_SOURCES 格式錯誤：'{raw}'\n"
                "正確格式：name:host:port:src_topic:label"
            )
        name, host, port_str, topic, label = parts
        try:
            port = int(port_str)
        except ValueError:
            raise ValueError(f"ZMQ_SOURCES port 必須是整數，收到：'{port_str}'")
        return cls(name=name, src_host=host, src_port=port, src_topic=topic, label=label)


# ================================================================
# Settings
# ================================================================
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # ── HLS ────────────────────────────────────────────────────
    hls_target_fps: int = 20
    hls_frame_buffer_size: int = 10
    hls_base_dir: str = "data/pig_monitoring/hls"
    hls_slip_resync_seconds: float = 0.5   # writer 落後超過此值即重置截止時間（不爆衝補償）
    hls_discontinuity_seconds: float = 8.0  # 相鄰段 capture_ts 差超過此值 → #EXT-X-DISCONTINUITY
    hls_retention_days: int = 90

    # ── Logging ────────────────────────────────────────────────
    ffmpeg_log_level: str = "error"
    log_level: str = "INFO"

    # ── Database ───────────────────────────────────────────────
    database_url: str = "postgresql://pig:pig_password@localhost:15432/pig_monitoring"

    # ── ZMQ Multi-Source ───────────────────────────────────────
    # 格式：name:host:port:src_topic:label  多個 source 以分號分隔
    # 範例：rpi_local:192.168.50.5:5555:rpi_sensors:cam_01;rpi_tailscale:100.67.51.73:5555:rpi_sensors:rpi_sensors
    zmq_sources: List[ZmqSource] = []
    zmq_warmup_secs: float = 0.5   # slow joiner warm-up（秒）
    zmq_stale_ms: float = 500.0    # 幀過期門檻（毫秒）

    # ── 影像 ───────────────────────────────────────────────────
    jpeg_quality: int = 70

    # ── 推論 ───────────────────────────────────────────────────
    model_weights: str = "./ref/HybridSORT/pretrained/best_ckpt.pth.tar"
    # Note: pydantic reserves 'model_config'; use model_config_path here.
    # In .env, write MODEL_CONFIG_PATH (not MODEL_CONFIG).
    model_config_path: str = (
        "./ref/HybridSORT/exps/example/mot/yolox_oink_test_hybrid_sort_reid.py"
    )
    fast_reid_config: str = (
        "./ref/HybridSORT/fast_reid/configs/CUHKSYSU_DanceTrack/sbs_S50.yml"
    )
    fast_reid_weights: str = "./ref/HybridSORT/pretrained/model_0054.pth"
    device: str = "cuda"
    mot_worker_threads: int = 12

    # ── 分析排程 ───────────────────────────────────────────────
    analysis_interval_minutes: int = 30
    analysis_window_minutes: int = 60
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50
    # 活動量（同伴相對）參數
    activity_low_ratio: float = 0.3
    activity_recover_ratio: float = 0.5
    activity_abs_floor: float = 2.0
    # 合格門檻：object_id 在視窗內首→末筆軌跡的「絕對最短跨度」（秒）。
    # 用絕對秒數而非視窗比例——否則視窗放大時門檻等比變嚴，撞上 MOT ID
    # 跳號（軌跡被切成數段、每段壽命有限）→ 長視窗永遠無人合格、永不標記。
    activity_min_span_seconds: float = 300.0
    # 體溫異常偵測總開關
    temp_anomaly_enabled: bool = True

    # ── Validators ────────────────────────────────────────────
    @field_validator("zmq_sources", mode="before")
    @classmethod
    def parse_zmq_sources(cls, v: object) -> object:
        """
        接受字串（來自 .env）或已是 list（程式直接傳入）。
        字串格式：source1;source2;...
        每個 source：name:host:port:src_topic:label
        """
        if isinstance(v, str):
            return [ZmqSource.from_str(s) for s in v.split(";") if s.strip()]
        return v

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls,
        init_settings,
        env_settings,
        dotenv_settings,
        file_secret_settings,
    ):
        return (init_settings, _NonJsonDotEnvSource(settings_cls), file_secret_settings)


settings = Settings()