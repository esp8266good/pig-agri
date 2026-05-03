# Phase 2 — 串流 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement FFmpeg HLS pipeline (RGB + Thermal streams per camera) with on-demand process management, watchdog cleanup, and a minimal live stream player frontend.

**Architecture:** `HLSManager` maintains `dict[(camera_id, stream_type) → HLSStream]` with a daemon watchdog thread. Each `HLSStream` wraps a `subprocess.Popen` FFmpeg process fed via stdin. `zmq_receiver._run()` calls `hls_manager.feed()` for each non-empty frame. Streams start on-demand when `GET /stream/{camera_id}/live` is called. FastAPI serves `.m3u8`/`.ts` files via `FileResponse` and a single `static/index.html` player.

**Tech Stack:** Python `subprocess`, `threading`, FastAPI `FileResponse`/`StaticFiles`, `aiofiles`, FFmpeg (system binary), hls.js (CDN)

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `hls_manager.py` | Rewrite | `_make_ffmpeg_cmd`, `_start_ffmpeg`, `HLSStream`, `HLSManager` |
| `zmq_receiver.py` | Modify | Extract `_process_frame`, add `hls_manager.feed()` calls |
| `routers/stream.py` | Rewrite | `/hls/...` file serving, `/{camera_id}/live`, VOD stub |
| `main.py` | Modify | `GET /cameras`, `StaticFiles`, `hls_manager.stop_all()` in lifespan |
| `static/index.html` | Create | Minimal hls.js live player |
| `pyproject.toml` | Modify | Add `aiofiles>=23` dependency |
| `tests/test_hls_manager.py` | Create | Unit tests for HLSManager |
| `tests/test_stream_router.py` | Create | Stream router integration tests |
| `tests/test_main.py` | Modify | Add hls_manager mock to fixture, update live stream assertion |

---

## Task 1: FFmpeg command builder

**Files:**
- Modify: `hls_manager.py` (rewrite from stub)
- Create: `tests/test_hls_manager.py`

- [ ] **Step 1: Create test file with failing test**

```python
# tests/test_hls_manager.py
import pytest
import time
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from hls_manager import _make_ffmpeg_cmd


def test_ffmpeg_cmd_has_correct_hls_settings(tmp_path):
    cmd = _make_ffmpeg_cmd(tmp_path)
    assert cmd[cmd.index("-hls_time") + 1] == "4"
    assert cmd[cmd.index("-hls_list_size") + 1] == "3"
    assert "delete_segments+append_list" in " ".join(cmd)
    assert str(tmp_path / "index.m3u8") in cmd
    assert str(tmp_path / "seg_%03d.ts") in " ".join(cmd)
```

- [ ] **Step 2: Run test to confirm failure**

```bash
uv run pytest tests/test_hls_manager.py::test_ffmpeg_cmd_has_correct_hls_settings -v
```

Expected: `ImportError` or `FAILED` — `_make_ffmpeg_cmd` not defined yet

- [ ] **Step 3: Implement `_make_ffmpeg_cmd` and `_start_ffmpeg` in `hls_manager.py`**

Replace the entire file:

```python
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, Tuple

from loguru import logger

from config import settings

StreamKey = Tuple[str, str]


def _make_ffmpeg_cmd(out_dir: Path) -> list[str]:
    return [
        "ffmpeg", "-y", "-f", "mjpeg", "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-hls_time", "4", "-hls_list_size", "3",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        str(out_dir / "index.m3u8"),
    ]


def _start_ffmpeg(out_dir: Path) -> subprocess.Popen:
    return subprocess.Popen(
        _make_ffmpeg_cmd(out_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
```

Do NOT add `HLSStream`, `HLSManager`, or `hls_manager = HLSManager()` yet — those come in later tasks.

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest tests/test_hls_manager.py::test_ffmpeg_cmd_has_correct_hls_settings -v
```

Expected: PASSED

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat: add FFmpeg command builder for HLS pipeline"
```

---

## Task 2: HLSStream class

**Files:**
- Modify: `hls_manager.py`
- Modify: `tests/test_hls_manager.py`

- [ ] **Step 1: Add failing tests for HLSStream**

Append to `tests/test_hls_manager.py`:

```python
def _make_stream(tmp_path, monkeypatch, proc=None):
    from hls_manager import HLSStream
    if proc is None:
        proc = MagicMock()
        proc.stdin = MagicMock()
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    out_dir = tmp_path / "cam_01" / "rgb" / datetime.now().strftime("%Y-%m-%d-%H")
    out_dir.mkdir(parents=True)
    return HLSStream("cam_01", "rgb", proc, out_dir), proc


def test_hlsstream_feed_writes_to_stdin(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream.feed(b"\xff\xd8\xff")
    proc.stdin.write.assert_called_once_with(b"\xff\xd8\xff")
    proc.stdin.flush.assert_called_once()


def test_hlsstream_feed_updates_last_feed_time(tmp_path, monkeypatch):
    stream, _ = _make_stream(tmp_path, monkeypatch)
    before = stream.last_feed_time
    time.sleep(0.02)
    stream.feed(b"\xff\xd8\xff")
    assert stream.last_feed_time > before


def test_hlsstream_stop_closes_stdin_and_terminates(tmp_path, monkeypatch):
    stream, proc = _make_stream(tmp_path, monkeypatch)
    stream.stop()
    proc.stdin.close.assert_called_once()
    proc.terminate.assert_called_once()
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_hls_manager.py -v
```

Expected: `ImportError` — `HLSStream` not defined

- [ ] **Step 3: Implement `HLSStream` — append to `hls_manager.py` after `_start_ffmpeg`**

```python
class HLSStream:
    def __init__(
        self,
        camera_id: str,
        stream_type: str,
        proc: subprocess.Popen,
        out_dir: Path,
    ) -> None:
        self.camera_id = camera_id
        self.stream_type = stream_type
        self.proc = proc
        self.out_dir = out_dir
        self.last_feed_time: float = time.time()
        self._lock = threading.Lock()

    def _hour_dir(self) -> Path:
        return (
            Path(settings.hls_base_dir)
            / self.camera_id
            / self.stream_type
            / datetime.now().strftime("%Y-%m-%d-%H")
        )

    def feed(self, jpeg_bytes: bytes) -> None:
        with self._lock:
            new_dir = self._hour_dir()
            if new_dir != self.out_dir:
                self._restart(new_dir)
            self.last_feed_time = time.time()
            try:
                self.proc.stdin.write(jpeg_bytes)
                self.proc.stdin.flush()
            except Exception as e:
                logger.warning(
                    f"[{self.camera_id}/{self.stream_type}] stdin write error: {e}"
                )

    def _restart(self, new_dir: Path) -> None:
        self.stop()
        new_dir.mkdir(parents=True, exist_ok=True)
        self.proc = _start_ffmpeg(new_dir)
        self.out_dir = new_dir
        logger.info(
            f"Rolled over HLS stream {self.camera_id}/{self.stream_type} → {new_dir}"
        )

    def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_hls_manager.py -v
```

Expected: all 4 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat: add HLSStream with feed, stop, and hourly rollover"
```

---

## Task 3: HLSManager.ensure_started + feed

**Files:**
- Modify: `hls_manager.py`
- Modify: `tests/test_hls_manager.py`

- [ ] **Step 1: Add failing tests for HLSManager.ensure_started and feed**

Append to `tests/test_hls_manager.py`:

```python
@pytest.fixture
def fake_proc():
    proc = MagicMock()
    proc.stdin = MagicMock()
    return proc


@pytest.fixture
def manager(tmp_path, monkeypatch, fake_proc):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    from hls_manager import HLSManager
    m = HLSManager()
    yield m, fake_proc
    m.stop_all()


def test_ensure_started_creates_dir_and_launches_ffmpeg(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc) as mock_start:
        out_dir = m.ensure_started("cam_01", "rgb")
    assert mock_start.call_count == 1
    assert out_dir.exists()
    assert ("cam_01", "rgb") in m._streams


def test_ensure_started_is_idempotent(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc) as mock_start:
        dir1 = m.ensure_started("cam_01", "rgb")
        dir2 = m.ensure_started("cam_01", "rgb")
    assert dir1 == dir2
    assert mock_start.call_count == 1


def test_feed_writes_bytes_when_stream_exists(manager):
    m, fake_proc = manager
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        m.ensure_started("cam_01", "rgb")
    m.feed("cam_01", "rgb", b"\xff\xd8\xff")
    fake_proc.stdin.write.assert_called_once_with(b"\xff\xd8\xff")


def test_feed_is_noop_when_stream_not_started(manager):
    m, fake_proc = manager
    m.feed("cam_99", "rgb", b"\xff\xd8\xff")
    fake_proc.stdin.write.assert_not_called()
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_hls_manager.py -v
```

Expected: `AttributeError` — `HLSManager` not defined

- [ ] **Step 3: Implement `HLSManager` (ensure_started + feed + watchdog skeleton) — append to `hls_manager.py`**

```python
class HLSManager:
    NO_FRAME_TIMEOUT: int = 30
    WATCHDOG_INTERVAL: int = 10

    def __init__(self) -> None:
        self._streams: Dict[StreamKey, HLSStream] = {}
        self._lock = threading.Lock()
        self._watchdog = threading.Thread(
            target=self._watchdog_loop, daemon=True, name="hls-watchdog"
        )
        self._watchdog.start()

    def ensure_started(self, camera_id: str, stream_type: str) -> Path:
        key: StreamKey = (camera_id, stream_type)
        with self._lock:
            if key not in self._streams:
                out_dir = (
                    Path(settings.hls_base_dir)
                    / camera_id
                    / stream_type
                    / datetime.now().strftime("%Y-%m-%d-%H")
                )
                out_dir.mkdir(parents=True, exist_ok=True)
                proc = _start_ffmpeg(out_dir)
                self._streams[key] = HLSStream(camera_id, stream_type, proc, out_dir)
                logger.info(f"Started HLS stream {camera_id}/{stream_type} → {out_dir}")
            return self._streams[key].out_dir

    def feed(self, camera_id: str, stream_type: str, jpeg_bytes: bytes) -> None:
        key: StreamKey = (camera_id, stream_type)
        with self._lock:
            stream = self._streams.get(key)
        if stream is not None:
            stream.feed(jpeg_bytes)

    def stop_all(self) -> None:
        with self._lock:
            streams = list(self._streams.values())
            self._streams.clear()
        for stream in streams:
            stream.stop()
            logger.info(f"Stopped HLS stream {stream.camera_id}/{stream.stream_type}")

    def _evict_stale(self) -> None:
        now = time.time()
        with self._lock:
            stale = [
                key
                for key, stream in self._streams.items()
                if now - stream.last_feed_time > self.NO_FRAME_TIMEOUT
            ]
            for key in stale:
                stream = self._streams.pop(key)
                stream.stop()
                logger.warning(f"Watchdog evicted stale stream {key[0]}/{key[1]}")

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(self.WATCHDOG_INTERVAL)
            self._evict_stale()


hls_manager = HLSManager()
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_hls_manager.py -v
```

Expected: all 8 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add hls_manager.py tests/test_hls_manager.py
git commit -m "feat: add HLSManager with ensure_started, feed, watchdog, and stop_all"
```

---

## Task 4: HLSManager watchdog tests

**Files:**
- Modify: `tests/test_hls_manager.py`

(`_evict_stale` and `stop_all` are already implemented in Task 3; this task adds tests for them.)

- [ ] **Step 1: Append watchdog and stop_all tests**

```python
def test_evict_stale_removes_expired_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
    m._streams[("cam_01", "rgb")].last_feed_time = time.time() - 60
    m._evict_stale()
    assert ("cam_01", "rgb") not in m._streams
    fake_proc.terminate.assert_called()


def test_evict_stale_keeps_fresh_stream(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    fake_proc = MagicMock()
    fake_proc.stdin = MagicMock()
    with patch("hls_manager._start_ffmpeg", return_value=fake_proc):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
    m._evict_stale()
    assert ("cam_01", "rgb") in m._streams


def test_stop_all_terminates_all_streams(tmp_path, monkeypatch):
    monkeypatch.setattr("hls_manager.settings.hls_base_dir", str(tmp_path))
    proc1, proc2 = MagicMock(), MagicMock()
    proc1.stdin, proc2.stdin = MagicMock(), MagicMock()
    with patch("hls_manager._start_ffmpeg", side_effect=[proc1, proc2]):
        from hls_manager import HLSManager
        m = HLSManager()
        m.ensure_started("cam_01", "rgb")
        m.ensure_started("cam_02", "rgb")
        m.stop_all()
    proc1.terminate.assert_called()
    proc2.terminate.assert_called()
    assert len(m._streams) == 0
```

- [ ] **Step 2: Run tests to confirm pass**

```bash
uv run pytest tests/test_hls_manager.py -v
```

Expected: all 11 tests PASSED

- [ ] **Step 3: Commit**

```bash
git add tests/test_hls_manager.py
git commit -m "test: add watchdog eviction and stop_all tests for HLSManager"
```

---

## Task 5: zmq_receiver HLS integration

**Files:**
- Modify: `zmq_receiver.py`
- Modify: `tests/test_zmq_receiver.py`

- [ ] **Step 1: Add failing tests for `_process_frame`**

Append to `tests/test_zmq_receiver.py`:

```python
import struct
from unittest.mock import patch, MagicMock


def test_process_frame_feeds_hls_manager(monkeypatch):
    import hls_manager as hls_mod
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    rgb = b"\xff\xd8\xff" + b"\x00" * 10
    thermal = b"\xff\xd8\xff" + b"\x00" * 5
    receiver._process_frame([topic, metadata, rgb, thermal])

    mock_manager.feed.assert_any_call("cam_01", "rgb", rgb)
    mock_manager.feed.assert_any_call("cam_01", "thermal", thermal)


def test_process_frame_skips_empty_rgb(monkeypatch):
    import hls_manager as hls_mod
    mock_manager = MagicMock()
    monkeypatch.setattr(hls_mod, "hls_manager", mock_manager)

    receiver = ZMQReceiver()
    topic = b"cam_01"
    metadata = struct.pack("dQ", 1234567890.0, 42)
    receiver._process_frame([topic, metadata, b"", b"\xff\xd8\xff"])

    calls = mock_manager.feed.call_args_list
    assert not any(c.args[1] == "rgb" for c in calls)
```

- [ ] **Step 2: Run tests to confirm failure**

```bash
uv run pytest tests/test_zmq_receiver.py -v
```

Expected: `AttributeError` — `ZMQReceiver` has no `_process_frame`

- [ ] **Step 3: Refactor `zmq_receiver.py` — extract `_process_frame`, add hls feed calls**

Replace the entire file:

```python
import struct
import threading
from typing import Optional

import zmq
from loguru import logger

import hls_manager as hls_mod
from config import settings


class ZMQReceiver:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="zmq-receiver"
        )
        self._thread.start()
        logger.info("ZMQ receiver started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
            if self._thread.is_alive():
                logger.warning("ZMQ receiver thread did not stop within timeout")
        logger.info("ZMQ receiver stopped")

    def _process_frame(self, parts: list) -> None:
        if len(parts) < 4:
            return
        topic = parts[0].decode()
        ts, frame_id = struct.unpack("dQ", parts[1])
        rgb_bytes: bytes = parts[2]
        thermal_bytes: bytes = parts[3]
        logger.info(
            f"[{topic}] frame={frame_id} ts={ts:.3f} "
            f"rgb={len(rgb_bytes)}B thermal={len(thermal_bytes)}B"
        )
        if rgb_bytes:
            hls_mod.hls_manager.feed(topic, "rgb", rgb_bytes)
        if thermal_bytes:
            hls_mod.hls_manager.feed(topic, "thermal", thermal_bytes)

    def _run(self) -> None:
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(f"tcp://{settings.rpi_ip}:{settings.zmq_port}")
        sock.setsockopt(zmq.SUBSCRIBE, b"rpi_sensors")
        for topic in settings.camera_topics:
            sock.setsockopt(zmq.SUBSCRIBE, topic.encode())

        while self._running:
            if sock.poll(100) == 0:
                continue
            try:
                parts = sock.recv_multipart()
                self._process_frame(parts)
            except zmq.ZMQError as e:
                logger.error(f"ZMQ fatal error, stopping receiver: {e}")
                self._running = False
                break
            except Exception as e:
                logger.warning(f"ZMQ frame parse error: {e}")

        sock.close()
        ctx.term()


zmq_receiver = ZMQReceiver()
```

- [ ] **Step 4: Run all zmq tests to confirm pass**

```bash
uv run pytest tests/test_zmq_receiver.py -v
```

Expected: all 5 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add zmq_receiver.py tests/test_zmq_receiver.py
git commit -m "feat: integrate hls_manager.feed into ZMQ receiver via _process_frame"
```

---

## Task 6: GET /cameras endpoint

**Files:**
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Add failing test**

Append to `tests/test_main.py`:

```python
def test_cameras_returns_list(client):
    resp = client.get("/cameras")
    assert resp.status_code == 200
    data = resp.json()
    assert "cameras" in data
    assert isinstance(data["cameras"], list)
    assert len(data["cameras"]) > 0
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_main.py::test_cameras_returns_list -v
```

Expected: FAILED — 404 Not Found

- [ ] **Step 3: Add import and endpoint to `main.py`**

After the existing `from fastapi import FastAPI` line, add:

```python
from config import settings as app_settings
```

After the existing `@app.get("/health")` endpoint, add:

```python
@app.get("/cameras", tags=["system"])
async def list_cameras():
    return {"cameras": app_settings.camera_topics}
```

- [ ] **Step 4: Run test to confirm pass**

```bash
uv run pytest tests/test_main.py::test_cameras_returns_list -v
```

Expected: PASSED

- [ ] **Step 5: Commit**

```bash
git add main.py tests/test_main.py
git commit -m "feat: add GET /cameras endpoint returning camera_topics from config"
```

---

## Task 7: GET /stream/{camera_id}/live endpoint

**Files:**
- Modify: `routers/stream.py`
- Create: `tests/test_stream_router.py`

- [ ] **Step 1: Create test file with failing tests**

```python
# tests/test_stream_router.py
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_live_returns_m3u8_url(client):
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir):
        resp = client.get("/stream/cam_01/live?type=rgb")
    assert resp.status_code == 200
    assert resp.json()["url"] == "/stream/hls/cam_01/rgb/2026-05-04-14/index.m3u8"


def test_live_default_type_is_rgb(client):
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir) as mock_start:
        resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    mock_start.assert_called_with("cam_01", "rgb")


def test_live_invalid_type_returns_400(client):
    resp = client.get("/stream/cam_01/live?type=invalid")
    assert resp.status_code == 400
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_stream_router.py -v
```

Expected: FAILED — live endpoint returns `{"status": "not implemented"}`

- [ ] **Step 3: Rewrite `routers/stream.py`**

```python
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse

from config import settings
from hls_manager import hls_manager

router = APIRouter(prefix="/stream", tags=["stream"])


# /hls/... must be defined BEFORE /{camera_id}/live to prevent the
# parametric route from capturing the literal "hls" path segment.
@router.get("/hls/{camera_id}/{stream_type}/{date_hour}/{filename}")
async def serve_hls(
    camera_id: str, stream_type: str, date_hour: str, filename: str
):
    file_path = (
        Path(settings.hls_base_dir) / camera_id / stream_type / date_hour / filename
    )
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@router.get("/{camera_id}/live")
async def get_live_stream(
    camera_id: str,
    stream_type: str = Query("rgb", alias="type"),
):
    if stream_type not in ("rgb", "thermal"):
        raise HTTPException(status_code=400, detail="type must be 'rgb' or 'thermal'")
    out_dir = hls_manager.ensure_started(camera_id, stream_type)
    return {
        "url": f"/stream/hls/{camera_id}/{stream_type}/{out_dir.name}/index.m3u8"
    }


@router.get("/{camera_id}/vod")
async def get_vod_stream(camera_id: str, start: float = 0, end: float = 0):
    return {"status": "not implemented"}
```

- [ ] **Step 4: Run tests to confirm pass**

```bash
uv run pytest tests/test_stream_router.py -v
```

Expected: all 3 tests PASSED

- [ ] **Step 5: Commit**

```bash
git add routers/stream.py tests/test_stream_router.py
git commit -m "feat: implement /stream/{camera_id}/live endpoint and HLS file serving"
```

---

## Task 8: HLS file serving tests

**Files:**
- Modify: `tests/test_stream_router.py`

(`serve_hls` is already implemented in Task 7; this task adds the 200/404 tests.)

- [ ] **Step 1: Append HLS file serving tests**

```python
def test_serve_hls_file_returns_200(tmp_path, monkeypatch):
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    ts_file = tmp_path / "cam_01" / "rgb" / "2026-05-04-14" / "seg_000.ts"
    ts_file.parent.mkdir(parents=True)
    ts_file.write_bytes(b"fake ts content")
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            resp = c.get("/stream/hls/cam_01/rgb/2026-05-04-14/seg_000.ts")
    assert resp.status_code == 200


def test_serve_hls_file_returns_404_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr("routers.stream.settings.hls_base_dir", str(tmp_path))
    with (
        patch("database.connect", new_callable=AsyncMock),
        patch("database.disconnect", new_callable=AsyncMock),
        patch("zmq_receiver.zmq_receiver.start"),
        patch("zmq_receiver.zmq_receiver.stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            resp = c.get("/stream/hls/cam_01/rgb/2026-05-04-14/seg_999.ts")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run tests to confirm pass**

```bash
uv run pytest tests/test_stream_router.py -v
```

Expected: all 5 tests PASSED

- [ ] **Step 3: Commit**

```bash
git add tests/test_stream_router.py
git commit -m "test: add HLS file serving 200/404 tests"
```

---

## Task 9: main.py lifespan + StaticFiles + aiofiles dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `main.py`
- Modify: `tests/test_main.py`

- [ ] **Step 1: Add aiofiles to pyproject.toml**

In `pyproject.toml`, add `"aiofiles>=23"` to the `dependencies` list:

```toml
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pyzmq>=26",
    "pydantic-settings>=2.0",
    "loguru>=0.7",
    "opencv-python-headless>=4.9",
    "aiofiles>=23",
]
```

- [ ] **Step 2: Install new dependency**

```bash
uv sync
```

Expected: `aiofiles` installed, no errors

- [ ] **Step 3: Update `tests/test_main.py` — add hls_manager mock and fix live stream test**

The `client` fixture currently does NOT mock `hls_manager.stop_all`. After this task, the lifespan shutdown calls it. Update the fixture and the live stream test.

In `tests/test_main.py`, find the `client` fixture:

```python
@pytest.fixture
def client():
    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c
```

Replace with:

```python
@pytest.fixture
def client():
    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
        patch("hls_manager.hls_manager.stop_all"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c
```

Also add `from unittest.mock import patch` to the existing import line (it already imports `patch` via `from unittest.mock import AsyncMock, patch` — verify this is there).

Then replace the old live stream stub test:

```python
# OLD — remove this:
def test_stream_live_returns_stub(client):
    resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}
```

With:

```python
# NEW — replace with this:
def test_stream_live_returns_url(client):
    from pathlib import Path
    fake_dir = Path("/data/pig_monitoring/hls/cam_01/rgb/2026-05-04-14")
    with patch("hls_manager.hls_manager.ensure_started", return_value=fake_dir):
        resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    assert "url" in resp.json()
```

- [ ] **Step 4: Replace `main.py` with updated version**

```python
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

import database
from config import settings as app_settings
from hls_manager import hls_manager
from routers import alerts, notes, stream, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    hls_manager.stop_all()
    await database.disconnect()


app = FastAPI(title="豬隻疾病監測系統", lifespan=lifespan)

_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return RedirectResponse(url="/static/index.html")


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok"}


@app.get("/cameras", tags=["system"])
async def list_cameras():
    return {"cameras": app_settings.camera_topics}


app.include_router(stream.router)
app.include_router(tracking.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
app.include_router(notes.router)
```

- [ ] **Step 5: Run full test suite to confirm all pass**

```bash
uv run pytest tests/ -v
```

Expected: all tests PASSED. If `test_stream_live_returns_stub` still appears and fails, confirm the old test was replaced with `test_stream_live_returns_url`.

- [ ] **Step 6: Commit**

```bash
git add main.py pyproject.toml tests/test_main.py
git commit -m "feat: update lifespan with hls_manager.stop_all and add StaticFiles mount"
```

---

## Task 10: static/index.html minimal live player

**Files:**
- Create: `static/index.html`

- [ ] **Step 1: Create the HTML file**

```bash
mkdir -p static
```

Create `static/index.html`:

```html
<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>豬隻監測 Live</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background: #111;
      color: #e0e0e0;
      font-family: 'Segoe UI', system-ui, sans-serif;
      display: flex;
      flex-direction: column;
      align-items: center;
      padding: 24px 16px;
      min-height: 100vh;
    }
    h1 { font-size: 1.3rem; margin-bottom: 20px; letter-spacing: 0.05em; }
    .controls {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 16px;
      flex-wrap: wrap;
      justify-content: center;
    }
    label { font-size: 0.9rem; color: #aaa; }
    select {
      background: #222;
      color: #e0e0e0;
      border: 1px solid #444;
      border-radius: 4px;
      padding: 6px 10px;
      font-size: 0.9rem;
      cursor: pointer;
    }
    .type-btn {
      background: #222;
      color: #aaa;
      border: 1px solid #444;
      border-radius: 4px;
      padding: 6px 16px;
      font-size: 0.9rem;
      cursor: pointer;
      transition: background 0.15s, color 0.15s;
    }
    .type-btn.active {
      background: #2a6;
      color: #fff;
      border-color: #2a6;
    }
    #video-wrap {
      width: 100%;
      max-width: 800px;
      background: #000;
      border-radius: 6px;
      overflow: hidden;
      aspect-ratio: 4/3;
    }
    video {
      width: 100%;
      height: 100%;
      display: block;
      background: #000;
    }
    #status {
      margin-top: 12px;
      font-size: 0.85rem;
      color: #888;
    }
    #status.live { color: #2a6; }
    #status.error { color: #c44; }
  </style>
</head>
<body>
  <h1>豬隻監測 Live</h1>
  <div class="controls">
    <label for="cam-select">Camera:</label>
    <select id="cam-select"></select>
    <button class="type-btn active" id="btn-rgb" onclick="setType('rgb')">RGB</button>
    <button class="type-btn" id="btn-thermal" onclick="setType('thermal')">Thermal</button>
  </div>
  <div id="video-wrap">
    <video id="video" autoplay muted playsinline controls></video>
  </div>
  <div id="status">初始化中…</div>

  <script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
  <script>
    let hls = null;
    let currentCamera = null;
    let currentType = 'rgb';

    const video = document.getElementById('video');
    const camSelect = document.getElementById('cam-select');
    const statusEl = document.getElementById('status');

    function setStatus(msg, cls = '') {
      statusEl.textContent = msg;
      statusEl.className = cls;
    }

    function setType(type) {
      currentType = type;
      document.getElementById('btn-rgb').classList.toggle('active', type === 'rgb');
      document.getElementById('btn-thermal').classList.toggle('active', type === 'thermal');
      loadStream();
    }

    async function loadStream() {
      if (!currentCamera) return;
      if (hls) { hls.destroy(); hls = null; }
      setStatus('正在連線…');
      try {
        const res = await fetch(`/stream/${currentCamera}/live?type=${currentType}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const { url } = await res.json();
        if (Hls.isSupported()) {
          hls = new Hls({ lowLatencyMode: false });
          hls.loadSource(url);
          hls.attachMedia(video);
          hls.on(Hls.Events.MANIFEST_PARSED, () => {
            video.play();
            setStatus('● 連線中', 'live');
          });
          hls.on(Hls.Events.ERROR, (_, data) => {
            if (data.fatal) setStatus(`串流錯誤：${data.details}`, 'error');
          });
        } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
          video.src = url;
          video.play();
          setStatus('● 連線中', 'live');
        } else {
          setStatus('瀏覽器不支援 HLS', 'error');
        }
      } catch (e) {
        setStatus(`無法取得串流：${e.message}`, 'error');
      }
    }

    async function init() {
      try {
        const res = await fetch('/cameras');
        const { cameras } = await res.json();
        cameras.forEach(cam => {
          const opt = document.createElement('option');
          opt.value = cam;
          opt.textContent = cam;
          camSelect.appendChild(opt);
        });
        if (cameras.length > 0) {
          currentCamera = cameras[0];
          loadStream();
        }
      } catch (e) {
        setStatus('無法取得 camera 清單', 'error');
      }
    }

    camSelect.addEventListener('change', () => {
      currentCamera = camSelect.value;
      loadStream();
    });

    init();
  </script>
</body>
</html>
```

- [ ] **Step 2: Smoke test the frontend**

Start the server:

```bash
uv run uvicorn main:app --reload --port 5005
```

Open `http://localhost:5005/` — should redirect to the player. Verify:
- Camera dropdown populates with cam_01..cam_06
- RGB / Thermal buttons toggle correctly
- Status shows "正在連線…" then either "● 連線中" (if FFmpeg running) or an error (expected without live RPi)
- No JavaScript errors in browser console

- [ ] **Step 3: Commit**

```bash
git add static/index.html
git commit -m "feat: add minimal HLS live player frontend"
```

---

## Task 11: Full test suite verification

**Files:**
- Run only

- [ ] **Step 1: Run complete test suite**

```bash
uv run pytest tests/ -v
```

Expected (all passing):

```
tests/test_config.py::...          PASSED
tests/test_database.py::...        PASSED
tests/test_hls_manager.py::...     PASSED  (11 tests)
tests/test_main.py::...            PASSED  (updated tests including test_stream_live_returns_url)
tests/test_stream_router.py::...   PASSED  (5 tests)
tests/test_zmq_receiver.py::...    PASSED  (5 tests)
```

- [ ] **Step 2: Fix any failures**

Common issues:
- `ImportError: No module named 'aiofiles'` → run `uv sync`
- `test_stream_live_returns_stub FAILED` → the old test was not replaced in Task 9 Step 3
- `test_zmq_receiver.py` fails → verify `zmq_receiver.py` imports `import hls_manager as hls_mod`
- `test_hls_manager.py` fixture errors → verify `hls_manager.py` ends with `hls_manager = HLSManager()`

- [ ] **Step 3: Commit any fixes**

```bash
git add -p
git commit -m "fix: resolve test failures after Phase 2 integration"
```

---

Phase 2 complete when `uv run pytest tests/ -v` shows all tests passing and `http://localhost:5005/` loads the live player without JavaScript errors.
