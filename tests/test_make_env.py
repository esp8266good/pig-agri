"""scripts/make_env.py 的驗證器測試。

只測純函式（`check_*` / `parse_env_file` / `_render`）——互動的部分是薄殼，
負責問與印，沒有邏輯。

網路探測一律注入假的 probe：真的去連 TCP 會讓測試變慢又不穩定。
"""

import pytest

from scripts import make_env as me

ERROR, WARN, OK = me.ERROR, me.WARN, me.OK


def levels(findings):
    return {f.level for f in findings}


def _probe(reachable: set[tuple[str, int]]):
    return lambda host, port, timeout=3.0: (host, port) in reachable


# ── ZMQ_SOURCES ──────────────────────────────────────────────────

def test_zmq_empty_is_error():
    assert levels(me.check_zmq_sources("")) == {ERROR}


def test_zmq_bad_format_is_error():
    # 少一段（缺 label）
    f = me.check_zmq_sources("cam:1.2.3.4:5555:topic", probe=_probe(set()))
    assert ERROR in levels(f)


def test_zmq_non_numeric_port_is_error():
    f = me.check_zmq_sources("cam:1.2.3.4:abc:topic:label", probe=_probe(set()))
    assert ERROR in levels(f)


def test_zmq_reachable_source_is_ok():
    f = me.check_zmq_sources("cam:1.2.3.4:5555:topic:cam_01",
                             probe=_probe({("1.2.3.4", 5555)}))
    assert levels(f) == {OK}


def test_zmq_unreachable_is_warn_not_error():
    """相機暫時斷線不該擋住產生 .env，但一定要講出來——服務會起來、網頁會通、
    就是一幀資料都沒有。"""
    f = me.check_zmq_sources("cam:1.2.3.4:5555:topic:cam_01", probe=_probe(set()))
    assert levels(f) == {WARN}


def test_zmq_duplicate_label_is_error():
    """label 是相機在整個系統裡的身分，重複會讓兩路影像互相蓋掉。"""
    raw = "a:1.1.1.1:5555:t:same;b:2.2.2.2:5555:t:same"
    f = me.check_zmq_sources(raw, probe=_probe({("1.1.1.1", 5555), ("2.2.2.2", 5555)}))
    assert ERROR in levels(f)
    assert any("label 重複" in x.message for x in f)


def test_zmq_multiple_sources_each_probed():
    raw = "a:1.1.1.1:5555:t:cam_01;b:2.2.2.2:5555:t:cam_02"
    f = me.check_zmq_sources(raw, probe=_probe({("1.1.1.1", 5555)}))
    assert {x.level for x in f} == {OK, WARN}


# ── 目錄 ─────────────────────────────────────────────────────────

def test_writable_dir_ok(tmp_path):
    assert levels(me.check_writable_dir(str(tmp_path), "X")) == {OK}


def test_writable_dir_missing_but_parent_ok_is_warn(tmp_path):
    f = me.check_writable_dir(str(tmp_path / "not_yet"), "X")
    assert levels(f) == {WARN}


def test_writable_dir_pointing_at_a_file_is_error(tmp_path):
    p = tmp_path / "afile"
    p.write_text("x")
    assert levels(me.check_writable_dir(str(p), "X")) == {ERROR}


def test_writable_dir_empty_is_error():
    assert levels(me.check_writable_dir("  ", "X")) == {ERROR}


def test_recording_disk_low_space_is_warn(tmp_path):
    """門檻拉到荒謬的高，一定會低於——驗的是「低於就出聲」這條規則本身。"""
    f = me.check_recording_disk(str(tmp_path), min_free_gb=10 ** 9)
    assert any(x.level == WARN and "低於" in x.message for x in f)


def test_recording_disk_same_device_as_root_warns(tmp_path):
    """錄影跟系統碟共用時要出聲——低空間保護保護不到東西。

    root_path 直接指向 tmp_path，保證「同一裝置」成立；不能拿真的 `/` 當前提，
    有些機器的 /tmp 是獨立 tmpfs，測試會時好時壞。
    """
    f = me.check_recording_disk(str(tmp_path), min_free_gb=0, root_path=str(tmp_path))
    assert any(x.level == WARN and "系統碟" in x.message for x in f)


def test_recording_disk_on_separate_device_does_not_warn(tmp_path):
    f = me.check_recording_disk(str(tmp_path), min_free_gb=0, root_path="/proc")
    assert not any("系統碟" in x.message for x in f)


# ── 模型檔 ───────────────────────────────────────────────────────

def test_model_file_missing_is_error():
    f = me.check_model_files(("/nope/does/not/exist.pth", "MODEL_WEIGHTS"))
    assert levels(f) == {ERROR}
    assert "rsync" in f[0].message


def test_model_file_present_is_ok(tmp_path):
    p = tmp_path / "w.pth"
    p.write_bytes(b"x" * 1024)
    assert levels(me.check_model_files((str(p), "MODEL_WEIGHTS"))) == {OK}


# ── 執行緒數 ─────────────────────────────────────────────────────

def test_worker_threads_above_cpu_count_is_error():
    assert levels(me.check_worker_threads("16", cpu_count=12)) == {ERROR}


def test_worker_threads_most_of_cpu_is_warn():
    """設得太滿會跟 ffmpeg 搶核心——這是遷移時實際踩到的。"""
    assert levels(me.check_worker_threads("11", cpu_count=12)) == {WARN}


def test_worker_threads_reasonable_is_ok():
    assert levels(me.check_worker_threads("8", cpu_count=12)) == {OK}


def test_worker_threads_non_integer_is_error():
    assert levels(me.check_worker_threads("八", cpu_count=12)) == {ERROR}


def test_worker_threads_zero_is_error():
    assert levels(me.check_worker_threads("0", cpu_count=12)) == {ERROR}


@pytest.mark.parametrize("cpu,expected", [(1, 1), (2, 1), (12, 8), (24, 16)])
def test_suggest_worker_threads(cpu, expected):
    assert me.suggest_worker_threads(cpu) == expected


# ── DEVICE ───────────────────────────────────────────────────────

def test_device_rejects_unknown_value():
    assert levels(me.check_device("gpu")) == {ERROR}


def test_device_cpu_is_warn():
    assert levels(me.check_device("cpu")) == {WARN}


# ── DATABASE_URL ─────────────────────────────────────────────────

def test_database_url_wrong_scheme_is_error():
    assert levels(me.check_database_url("mysql://a:b@h:3306/d")) == {ERROR}


def test_database_url_unparsable_is_error():
    assert levels(me.check_database_url("postgresql://no-at-sign")) == {ERROR}


def test_database_url_reachable_is_ok():
    f = me.check_database_url("postgresql://u:p@localhost:15432/db",
                              probe=_probe({("localhost", 15432)}))
    assert levels(f) == {OK}


def test_database_url_unreachable_is_warn():
    """還沒 docker compose up -d 是很正常的狀態，不該擋。"""
    f = me.check_database_url("postgresql://u:p@localhost:15432/db", probe=_probe(set()))
    assert levels(f) == {WARN}


def test_database_url_default_port_when_omitted():
    f = me.check_database_url("postgresql://u:p@h/db", probe=_probe({("h", 5432)}))
    assert levels(f) == {OK}


# ── 解析與輸出 ───────────────────────────────────────────────────

def test_parse_env_file_ignores_comments_and_blanks():
    got = me.parse_env_file("# c\n\nA=1\n  B = two \n#D=4\nnot_a_pair\n")
    assert got == {"A": "1", "B": "two"}


def test_parse_env_file_keeps_value_containing_equals():
    """DATABASE_URL 之類的值裡面可能有 = 或 :，只能切第一個 =。"""
    got = me.parse_env_file("DATABASE_URL=postgresql://u:p=x@h:1/db")
    assert got["DATABASE_URL"] == "postgresql://u:p=x@h:1/db"


def test_render_roundtrips_through_parse():
    v = {"ZMQ_SOURCES": "a:1.1.1.1:5555:t:cam_01", "HLS_BASE_DIR": "/x",
         "DEVICE": "cuda", "MOT_WORKER_THREADS": "8"}
    assert me.parse_env_file(me._render(v)) == v


def test_render_omits_keys_that_were_not_set():
    out = me._render({"DEVICE": "cpu"})
    assert "AUTH_PASSWORD_HASH" not in out


# ── 整份檢查 ─────────────────────────────────────────────────────

def _minimal_ok_env(tmp_path):
    w = tmp_path / "w.pth"
    w.write_bytes(b"x")
    c = tmp_path / "c.py"
    c.write_text("x")
    return {
        "ZMQ_SOURCES": "a:1.1.1.1:5555:t:cam_01",
        "HLS_BASE_DIR": str(tmp_path),
        "MODEL_WEIGHTS": str(w),
        "MODEL_CONFIG_PATH": str(c),
        "MOT_WORKER_THREADS": "2",
        "DEVICE": "cpu",
        "DATABASE_URL": "postgresql://u:p@localhost:15432/db",
    }


def test_check_env_has_no_errors_on_a_sane_config(tmp_path):
    f = me.check_env(_minimal_ok_env(tmp_path),
                     probe=_probe({("1.1.1.1", 5555), ("localhost", 15432)}))
    assert ERROR not in levels(f)


def test_auth_enabled_without_hash_is_error(tmp_path):
    v = _minimal_ok_env(tmp_path) | {"AUTH_ENABLED": "true"}
    f = me.check_env(v, probe=_probe(set()))
    assert any(x.field == "AUTH_PASSWORD_HASH" and x.level == ERROR for x in f)


def test_auth_enabled_with_short_secret_is_error(tmp_path):
    v = _minimal_ok_env(tmp_path) | {
        "AUTH_ENABLED": "true", "AUTH_PASSWORD_HASH": "scrypt$16384$8$1$a$b",
        "AUTH_SESSION_SECRET": "short"}
    f = me.check_env(v, probe=_probe(set()))
    assert any(x.field == "AUTH_SESSION_SECRET" and x.level == ERROR for x in f)


def test_cookie_secure_without_tls_is_warn(tmp_path):
    """設 true 但前面沒有 TLS 的話 cookie 根本不會送出，沒有人登得進去。"""
    v = _minimal_ok_env(tmp_path) | {
        "AUTH_ENABLED": "true", "AUTH_PASSWORD_HASH": "scrypt$16384$8$1$a$b",
        "AUTH_SESSION_SECRET": "x" * 40, "AUTH_COOKIE_SECURE": "true"}
    f = me.check_env(v, probe=_probe(set()))
    assert any(x.field == "AUTH_COOKIE_SECURE" and x.level == WARN for x in f)


def test_auth_disabled_skips_auth_checks(tmp_path):
    v = _minimal_ok_env(tmp_path) | {"AUTH_ENABLED": "false"}
    f = me.check_env(v, probe=_probe(set()))
    assert not any(x.field.startswith("AUTH_") for x in f)


# ── 嚴重度彙總 ───────────────────────────────────────────────────

@pytest.mark.parametrize("lv,expected", [
    ([OK, OK], OK), ([OK, WARN], WARN), ([WARN, ERROR], ERROR), ([], OK),
])
def test_worst_level(lv, expected):
    assert me.worst_level([me.Finding(x, "f", "m") for x in lv]) == expected
