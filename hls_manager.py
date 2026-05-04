import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional, Tuple

from loguru import logger

from config import settings

StreamKey = Tuple[str, str]

# ─── Log 等級控制 ────────────────────────────────────────────────────────────
# 在 settings 或環境變數裡設定 LOG_LEVEL = "DEBUG" / "INFO" / "WARNING" 等
# 預設使用 "INFO"
def configure_logging(level: str = "INFO") -> None:
    """設定 loguru 全域 log 等級。啟動時呼叫一次即可。"""
    import sys
    logger.remove()  # 移除預設 handler
    logger.add(
        sys.stderr,
        level=level.upper(),
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
        colorize=True,
    )
    # 選擇性：同時輸出到檔案
    # logger.add("logs/hls_{time}.log", level=level.upper(), rotation="1 day", retention="7 days")


# ─── FFmpeg 指令 ─────────────────────────────────────────────────────────────
# 改良重點：
# 1. 加上 -an：不需要聲音
# 2. 加上 -r {TARGET_FPS}：強制輸出固定 FPS，由 ffmpeg 自動補幀/丟幀
# 3. 加上 -vf fps={TARGET_FPS}：更精確的 FPS 控制（配合 frame drop/dup）
# 4. 移除 -tune zerolatency：不需要低延遲，穩定性優先
# 5. 加上 -g (GOP size) = 2 * FPS：讓 HLS 切割更整齊
TARGET_FPS: int = getattr(settings, "hls_target_fps", 25)
FFMPEG_LOG_LEVEL: str = getattr(settings, "ffmpeg_log_level", "warning")  # debug/info/warning/error/quiet


def _make_ffmpeg_cmd(out_dir: Path) -> list[str]:
    gop = TARGET_FPS * 2
    return [
        "ffmpeg", "-y",
        # 輸入：MJPEG pipe，不使用 wallclock timestamp，改由 ffmpeg 自己產生穩定時間軸
        "-f", "mjpeg",
        "-framerate", str(TARGET_FPS),  # 告知 ffmpeg 輸入預期 FPS（配合 fps filter）
        "-i", "pipe:0",
        # 不要聲音
        "-an",
        # 影像編碼
        "-c:v", "libx264",
        "-preset", "veryfast",          # ultrafast 省 CPU 但 bitrate 較高；veryfast 在穩定性上更佳
        "-crf", "23",                   # 固定品質，避免 bitrate 忽高忽低
        # 強制輸出固定 FPS：來源過快則丟幀，過慢則補幀
        "-vf", f"fps={TARGET_FPS}",
        "-g", str(gop),                 # GOP 大小對齊 HLS segment
        # HLS 輸出
        "-hls_time", "4",
        "-hls_list_size", "5",
        "-hls_flags", "delete_segments+append_list",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        # FFmpeg 自身的 log 等級
        "-loglevel", FFMPEG_LOG_LEVEL,
        str(out_dir / "index.m3u8"),
    ]


def _start_ffmpeg(out_dir: Path) -> subprocess.Popen:
    # 當 FFMPEG_LOG_LEVEL 為 debug/info 時，把 stderr pipe 出來方便檢查
    stderr_target = (
        subprocess.PIPE
        if FFMPEG_LOG_LEVEL in ("debug", "info", "verbose")
        else subprocess.DEVNULL
    )
    proc = subprocess.Popen(
        _make_ffmpeg_cmd(out_dir),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=stderr_target,
    )
    if stderr_target == subprocess.PIPE:
        # 背景執行緒消耗 stderr，避免 pipe buffer 滿了造成卡住
        threading.Thread(
            target=_drain_stderr,
            args=(proc,),
            daemon=True,
            name=f"ffmpeg-stderr-{out_dir.name}",
        ).start()
    return proc


def _drain_stderr(proc: subprocess.Popen) -> None:
    """讀取 ffmpeg stderr 並以 DEBUG 等級寫入 log。"""
    try:
        for line in proc.stderr:
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                logger.debug(f"[ffmpeg] {text}")
    except Exception:
        pass


# ─── Frame Buffer（平滑輸入） ────────────────────────────────────────────────
# 如果來源忽快忽慢，用一個小 buffer 緩衝輸入，讓寫入 ffmpeg 的速率更穩定
# buffer 只保留最新的 N 幀，避免記憶體無限增長
FRAME_BUFFER_SIZE: int = getattr(settings, "hls_frame_buffer_size", 10)


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

        # Frame buffer：用 deque 限制長度，來源過快時自動丟最舊的幀
        self._frame_buffer: deque[bytes] = deque(maxlen=FRAME_BUFFER_SIZE)
        self._buffer_event = threading.Event()
        self._stopped = False

        # 啟動 writer 執行緒，以固定節奏把 buffer 裡的幀送進 ffmpeg
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"hls-writer-{camera_id}-{stream_type}",
        )
        self._writer_thread.start()

    # ── 公開方法 ──────────────────────────────────────────────────────────

    def feed(self, jpeg_bytes: bytes) -> None:
        """把新幀放入 buffer；若 buffer 滿則自動丟棄最舊幀（deque maxlen 行為）。"""
        with self._lock:
            new_dir = self._hour_dir()
            if new_dir != self.out_dir:
                self._restart(new_dir)
        self.last_feed_time = time.time()
        self._frame_buffer.append(jpeg_bytes)
        self._buffer_event.set()

    def stop(self) -> None:
        self._stopped = True
        self._buffer_event.set()  # 喚醒 writer 讓它結束
        self._writer_thread.join(timeout=5)
        self._close_proc()

    # ── 內部方法 ──────────────────────────────────────────────────────────

    def _hour_dir(self) -> Path:
        return (
            Path(settings.hls_base_dir)
            / self.camera_id
            / self.stream_type
            / datetime.now().strftime("%Y-%m-%d-%H")
        )

    def _writer_loop(self) -> None:
        """以接近 TARGET_FPS 的速率從 buffer 取幀並寫入 ffmpeg stdin。"""
        interval = 1.0 / TARGET_FPS
        last_frame: Optional[bytes] = None

        while not self._stopped:
            deadline = time.monotonic() + interval

            # 取一幀（若有）
            try:
                frame = self._frame_buffer.popleft()
                last_frame = frame
            except IndexError:
                # buffer 空：重複上一幀（補幀），維持 ffmpeg 時間軸連續
                frame = last_frame

            if frame is not None:
                try:
                    self.proc.stdin.write(frame)
                    self.proc.stdin.flush()
                except BrokenPipeError:
                    logger.warning(
                        f"[{self.camera_id}/{self.stream_type}] ffmpeg stdin pipe broken, "
                        "stream may have crashed"
                    )
                    break
                except Exception as e:
                    logger.warning(f"[{self.camera_id}/{self.stream_type}] stdin write error: {e}")

            # 精確等待到下個寫入時間點
            sleep_time = deadline - time.monotonic()
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _restart(self, new_dir: Path) -> None:
        """切換到新小時目錄時重啟 ffmpeg process。"""
        self._close_proc()
        new_dir.mkdir(parents=True, exist_ok=True)
        self.proc = _start_ffmpeg(new_dir)
        self.out_dir = new_dir
        logger.info(
            f"Rolled over HLS stream {self.camera_id}/{self.stream_type} → {new_dir}"
        )

    def _close_proc(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            logger.warning(
                f"[{self.camera_id}/{self.stream_type}] ffmpeg did not terminate in 3s, killing"
            )
            self.proc.kill()


# ─── HLS Manager ─────────────────────────────────────────────────────────────

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
        logger.info(
            f"HLSManager started — target FPS: {TARGET_FPS}, "
            f"frame buffer: {FRAME_BUFFER_SIZE}, ffmpeg log: {FFMPEG_LOG_LEVEL}"
        )

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
        else:
            logger.debug(
                f"[{camera_id}/{stream_type}] feed() called but stream not started, dropping frame"
            )

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
            logger.warning(
                f"Watchdog evicted stale stream {stream.camera_id}/{stream.stream_type}"
            )

    def _watchdog_loop(self) -> None:
        while True:
            time.sleep(self.WATCHDOG_INTERVAL)
            try:
                self._evict_stale()
            except Exception as e:
                logger.error(f"Watchdog loop error: {e}")


# ─── 初始化 ──────────────────────────────────────────────────────────────────
# 從 settings 讀取 log 等級（預設 INFO）
configure_logging(getattr(settings, "log_level", "INFO"))

hls_manager = HLSManager()