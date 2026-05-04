from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, DotEnvSettingsSource, SettingsConfigDict


class _NonJsonDotEnvSource(DotEnvSettingsSource):
    """Disable JSON-decoding so comma-separated strings reach the validator."""

    def field_is_complex(self, field):  # type: ignore[override]
        return False


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    hls_target_fps: int = 20
    hls_frame_buffer_size: int = 10
    ffmpeg_log_level: str = "error"  # debug/info/warning/error/quiet
    log_level: str = "INFO"
    
    database_url: str = "postgresql://pig:pig_password@localhost:15432/pig_monitoring"
    zmq_port: int = 5555
    rpi_ip: str = "127.0.0.1"
    camera_topics: List[str] = [
        "cam_01", "cam_02", "cam_03", "cam_04", "cam_05", "cam_06"
    ]
    hls_base_dir: str = "data/pig_monitoring/hls"
    hls_retention_days: int = 90
    jpeg_quality: int = 70
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
    mot_worker_threads: int = 20
    analysis_interval_minutes: int = 30
    analysis_window_hours: int = 6
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50

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

    @field_validator("camera_topics", mode="before")
    @classmethod
    def parse_comma_separated(cls, v: object) -> object:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v


settings = Settings()
