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

    # ── Ephemeral live + 編碼旋鈕 ──────────────────────────────
    # 夜間 no-record / 錄影碟掛掉時，live 改寫這裡（滾動 buffer、錄影碟零寫入）。
    # 預設 tmpfs（零磨耗）；不可用時由 storage_monitor.effective_ephemeral_dir 回退系統碟。
    hls_ephemeral_dir: str = "/dev/shm/pig_live"
    hls_crf: int = 23                  # 調高（如 28）→ 檔案變小、寫入量降，畫質降
    hls_video_codec: str = "libx264"

    # ── 儲存健康監控 ───────────────────────────────────────────
    storage_check_interval_seconds: int = 20
    storage_min_free_gb: float = 10.0
    storage_min_free_inodes_ratio: float = 0.02
    storage_debounce_count: int = 2
    storage_volume_marker: str = ""    # 掛載防誤判標記檔名（空＝不檢查）

    # ── 夜間 no-record 排程（前端可調）────────────────────────
    recording_schedule_enabled: bool = True
    recording_off_start: str = "17:00"   # 本地時間 HH:MM
    recording_off_end: str = "06:30"

    # ── ntfy 推播通知（ops/儲存異常 → 手機）────────────────────
    ntfy_url: str = "https://ntfy.ed716.duckdns.org/pig"
    ntfy_enabled: bool = True
    # 錄影監督者重建串流的推播優先級（flaky 攝影機可能較頻，可在前端調低避免吵）。
    # 值為 ntfy priority 字串：min / low / default / high / urgent。
    ntfy_revive_priority: str = "default"

    # ── 夜間停 GPU 省電（獨立排程；預設關閉，零行為改變）────────
    gpu_off_schedule_enabled: bool = False
    gpu_off_start: str = "22:00"   # 本地時間 HH:MM
    gpu_off_end: str = "06:00"

    # ── 存取驗證（預設關閉＝現行行為完全不變）──────────────────
    # 這幾個只從 .env／環境變數讀，刻意「不」加進 routers/settings.py 的
    # ALLOWED_KEYS：/settings 正是要被這道驗證保護的端點，若開關是 DB-backed，
    # 未登入的人就能先打一發 PUT /settings 把鎖拆掉。細節見 auth.py 模組說明。
    #
    # 開啟步驟：
    #   1. uv run python scripts/make_password_hash.py
    #   2. 把輸出的三行貼進 .env（AUTH_ENABLED / AUTH_USERNAME / AUTH_PASSWORD_HASH）
    #   3. 設 AUTH_SESSION_SECRET（同一支腳本會產生），重啟服務
    # ⚠ 對公網開放時務必先有 TLS：cookie 走明文 HTTP 等於把帳密攤開送。
    auth_enabled: bool = False
    auth_username: str = ""
    auth_password_hash: str = ""     # scrypt$n$r$p$<salt>$<key>，見 auth.hash_password
    auth_session_secret: str = ""    # 簽 cookie 用；換掉＝強制所有人重新登入
    auth_session_hours: int = 12
    # 只在純 HTTP 的內網測試時才設 false；對公網一定要 true。
    auth_cookie_secure: bool = True
    auth_max_attempts: int = 10      # 同一 IP 連續登入失敗上限
    auth_lockout_minutes: int = 15
    # 反向代理後面才設 true，並確保代理會覆寫 X-Forwarded-For；直接對外時設 false，
    # 否則攻擊者能自己偽造這個 header 讓每次嘗試都算在不同「IP」上、繞過節流。
    auth_trust_forwarded_for: bool = False

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

    # 關注清單：前端右欄「現在該去看哪幾隻豬」的列表。
    # 最低 N 只在該相機零異常時才出現；對照 N 是活動量最高的幾隻，
    # 用途只有一個——給人眼一個「正常長什麼樣」的參考點。
    focus_lowest_enabled: bool = True
    focus_lowest_n: int = 3
    # 0 表示關閉對照組。不另給啟用開關：0 在這裡沒有語意歧義。
    focus_top_n: int = 3

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
        # 沿用框架已解析好的 dotenv_settings（含呼叫端傳入的 `_env_file` override，
        # 例如測試用 `_env_file=None` 隔離真實部署 .env）；只在其上疊加「不做
        # JSON 解碼」的行為，而非重新以 settings_cls 預設 env_file(".env") 建構
        # ——否則 `_env_file=None` 會被無視，測試永遠讀到真實 .env。
        dotenv_settings.__class__ = _NonJsonDotEnvSource
        return (init_settings, dotenv_settings, file_secret_settings)


settings = Settings()