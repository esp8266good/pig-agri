from config import Settings


def test_default_zmq_port():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.zmq_port == 5555


def test_default_mot_worker_threads():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.mot_worker_threads == 20


def test_default_anomaly_threshold():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.anomaly_std_threshold == 3.0


def test_camera_topics_default_six():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01,cam_02,cam_03,cam_04,cam_05,cam_06",
    )
    assert len(s.camera_topics) == 6


def test_camera_topics_comma_parsing():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01,cam_02",
    )
    assert s.camera_topics == ["cam_01", "cam_02"]


def test_camera_topics_strips_whitespace():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01, cam_02 , cam_03",
    )
    assert s.camera_topics == ["cam_01", "cam_02", "cam_03"]


def test_settings_has_fast_reid_config():
    from config import settings
    assert hasattr(settings, "fast_reid_config")
    assert "fast_reid" in settings.fast_reid_config

def test_settings_has_fast_reid_weights():
    from config import settings
    assert hasattr(settings, "fast_reid_weights")
    assert ".pth" in settings.fast_reid_weights
