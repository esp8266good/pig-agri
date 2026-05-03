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
        "ffmpeg", "-y", "-f", "mjpeg", "-r", "10", "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-hls_time", "4", "-hls_list_size", "5",
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
        evicted: list[HLSStream] = []
        with self._lock:
            stale = [
                key
                for key, stream in self._streams.items()
                if now - stream.last_feed_time > self.NO_FRAME_TIMEOUT
            ]
            for key in stale:
                evicted.append(self._streams.pop(key))
        for stream in evicted:
            stream.stop()
            logger.warning(f"Watchdog evicted stale stream {stream.camera_id}/{stream.stream_type}")

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(self.WATCHDOG_INTERVAL)
            self._evict_stale()


hls_manager = HLSManager()
