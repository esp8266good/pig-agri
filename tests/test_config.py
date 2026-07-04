from config import Settings, ZmqSource

# 共用的最小必填參數，避免每個 test 重複
_BASE = dict(
    database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
)

_SAMPLE_SOURCES = (
    "rpi_local:192.168.50.5:5555:rpi_sensors:cam_01;"
    "rpi_tailscale:100.67.51.73:5555:rpi_sensors:rpi_sensors"
)


# ── ZMQ Sources 解析 ──────────────────────────────────────────────

def test_zmq_sources_parses_two_entries():
    s = Settings(**_BASE, zmq_sources=_SAMPLE_SOURCES)
    assert len(s.zmq_sources) == 2


def test_zmq_sources_fields():
    s = Settings(**_BASE, zmq_sources=_SAMPLE_SOURCES)
    src = s.zmq_sources[0]
    assert src.name      == "rpi_local"
    assert src.src_host  == "192.168.50.5"
    assert src.src_port  == 5555
    assert src.src_topic == "rpi_sensors"
    assert src.label     == "cam_01"


def test_zmq_sources_labels():
    s = Settings(**_BASE, zmq_sources=_SAMPLE_SOURCES)
    labels = [src.label for src in s.zmq_sources]
    assert labels == ["cam_01", "rpi_sensors"]


def test_zmq_sources_port_is_int():
    s = Settings(**_BASE, zmq_sources="only_one:10.0.0.1:5678:rpi_sensors:cam_99")
    assert isinstance(s.zmq_sources[0].src_port, int)
    assert s.zmq_sources[0].src_port == 5678


def test_zmq_sources_strips_whitespace():
    # 分號前後有空白，應正常解析
    s = Settings(**_BASE, zmq_sources=" rpi_local:192.168.50.5:5555:rpi_sensors:cam_01 ")
    assert len(s.zmq_sources) == 1
    assert s.zmq_sources[0].name == "rpi_local"


def test_zmq_sources_default_empty():
    # 未設定 ZMQ_SOURCES 時預設為空 list（啟動時由 ZMQReceiver 拋錯）
    # _env_file=None：隔離真實部署 .env（裡面設了實際的 ZMQ_SOURCES）
    s = Settings(**_BASE, _env_file=None)
    assert s.zmq_sources == []


def test_zmq_warmup_secs_default():
    s = Settings(**_BASE)
    assert s.zmq_warmup_secs == 0.5


def test_zmq_stale_ms_default():
    # _env_file=None：隔離真實部署 .env（可能設了非預設的 ZMQ_STALE_MS）
    s = Settings(**_BASE, _env_file=None)
    assert s.zmq_stale_ms == 500.0


# ── 其他設定（不受此次修改影響）────────────────────────────────────

def test_default_mot_worker_threads():
    s = Settings(**_BASE)
    assert s.mot_worker_threads == 12


def test_default_anomaly_threshold():
    s = Settings(**_BASE)
    assert s.anomaly_std_threshold == 3.0



def test_settings_has_fast_reid_config():
    s = Settings()
    assert s.fast_reid_config == "./ref/HybridSORT/fast_reid/configs/CUHKSYSU_DanceTrack/sbs_S50.yml"


def test_settings_has_fast_reid_weights():
    s = Settings()
    assert s.fast_reid_weights == "./ref/HybridSORT/pretrained/model_0054.pth"


def test_storage_and_encoder_defaults():
    from config import Settings
    s = Settings()
    assert s.hls_ephemeral_dir == "/dev/shm/pig_live"
    assert s.hls_crf == 23
    assert s.hls_video_codec == "libx264"
    assert s.storage_check_interval_seconds == 20
    assert s.storage_min_free_gb == 10.0
    assert s.storage_min_free_inodes_ratio == 0.02
    assert s.storage_debounce_count == 2
    assert s.storage_volume_marker == ""
    assert s.recording_schedule_enabled is True
    assert s.recording_off_start == "17:00"
    assert s.recording_off_end == "06:30"


def test_new_ops_settings_defaults():
    from config import Settings
    s = Settings(_env_file=None)
    assert s.ntfy_url == "https://ntfy.ed716.duckdns.org/pig"
    assert s.ntfy_enabled is True
    assert s.gpu_off_schedule_enabled is False
    assert s.gpu_off_start == "22:00"
    assert s.gpu_off_end == "06:00"