import subprocess
import threading
import time
import re
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
# 2. 移除 -vf fps / 輸入 -framerate：改由 writer 真實牆鐘節拍器控速，消除 ffmpeg 媒體時鐘脫鉤牆鐘
# 3. 移除 -tune zerolatency：不需要低延遲，穩定性優先
# 4. 加上 -g (GOP size) = 2 * FPS：讓 HLS 切割更整齊
TARGET_FPS: int = getattr(settings, "hls_target_fps", 25)
FFMPEG_LOG_LEVEL: str = getattr(settings, "ffmpeg_log_level", "warning")  # debug/info/warning/error/quiet
# segment 時長（與 _make_ffmpeg_cmd 的 -hls_time 一致）
_HLS_TIME: int = 4
# emit/fed log 環形上限（約 30 分鐘餘量，遠超單一小時所需）
_FED_LOG_MAX: int = TARGET_FPS * 1800


def _iso_local(ts: float) -> str:
    """Unix ts → 本地時區 ISO8601（毫秒 + +HH:MM），對齊前端 hls.playingDate
    與 vod_generator 的 PDT 格式。"""
    dt = datetime.fromtimestamp(ts).astimezone()
    base = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    off = dt.strftime("%z")  # e.g. +0800
    return f"{base}{off[:3]}:{off[3:]}"


def _make_ffmpeg_cmd(out_dir: Path) -> list[str]:
    gop = TARGET_FPS * 2
    return [
        "ffmpeg", "-y",
        "-f", "mjpeg",
        "-i", "pipe:0",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-g", str(gop),
        "-hls_time", "4",
        "-hls_list_size", "0",
        "-hls_flags", "append_list+program_date_time",
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
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
        self._frame_buffer: deque[tuple[bytes, Optional[float]]] = deque(
            maxlen=FRAME_BUFFER_SIZE
        )
        self._buffer_event = threading.Event()
        self._stopped = False

        # 後端自管 PDT（根治 bbox 漸進落後）：記每個 segment 首幀的「真實
        # 擷取牆鐘」。ffmpeg 媒體導出的 PDT 會相對牆鐘漂移，改寫成這裡的
        # capture_ts 後，前端 hls.playingDate ≡ 真實擷取時間、零漂移。
        self._last_capture_ts: Optional[float] = None
        self._seg_pdt: dict[str, float] = {}
        self._seen_segs: set[str] = set()
        self._seg_lock = threading.Lock()
        self._last_scan: float = 0.0
        # 幀身分對應：餵入幀計數 + (fed_index, frame_id) 環形記錄，
        # 與 segment 首幀 frame_id 對應（_scan_new_segments 用幀計數推算）。
        self._fed_count: int = 0
        self._fed_log: deque[tuple[int, int]] = deque(maxlen=_FED_LOG_MAX)
        self._seg_first_fid: dict[str, int] = {}

        # 真實時間軸授權：每寫入 ffmpeg 一幀記 (emit_idx, capture_ts)，
        # 與 ffmpeg 輸出幀 1:1（writer 等速 tick）。_scan_new_segments
        # 據此推每段首幀真實擷取牆鐘。
        self._emit_idx: int = 0
        self._emit_log: deque[tuple[int, float]] = deque(maxlen=_FED_LOG_MAX)
        self._writer_last_frame: Optional[tuple[bytes, Optional[float]]] = None

        # 啟動 writer 執行緒，以固定節奏把 buffer 裡的幀送進 ffmpeg
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"hls-writer-{camera_id}-{stream_type}",
        )
        self._writer_thread.start()

    # ── 公開方法 ──────────────────────────────────────────────────────────

    def feed(
        self,
        jpeg_bytes: bytes,
        capture_ts: Optional[float] = None,
        frame_id: Optional[int] = None,
    ) -> None:
        """把新幀放入 buffer；若 buffer 滿則自動丟棄最舊幀（deque maxlen 行為）。
        capture_ts 為該幀真實擷取牆鐘（後端自管 PDT，fallback 用）；
        frame_id 保留簽名相容性（舊邏輯暫留，由後續 Task 統一刪除）。"""
        with self._lock:
            new_dir = self._hour_dir()
            if new_dir != self.out_dir:
                self._restart(new_dir)
        if capture_ts is not None:
            self._last_capture_ts = capture_ts
        self.last_feed_time = time.time()
        self._frame_buffer.append((jpeg_bytes, capture_ts))
        self._buffer_event.set()

    def _scan_new_segments(self) -> None:
        """偵測 out_dir 新出現的 seg_*.ts（ffmpeg 於 segment 起始即建檔），
        把當下最新 capture_ts 記為該 segment 首幀的真實擷取時間。"""
        try:
            names = [p.name for p in self.out_dir.glob("seg_*.ts")]
        except OSError:
            return
        cap = self._last_capture_ts
        with self._seg_lock:
            fed_log: list[tuple[int, int]] | None = None  # 惰性快照：僅在有新 segment 時才複製
            for name in names:
                if name in self._seen_segs:
                    continue
                self._seen_segs.add(name)
                if cap is not None:
                    self._seg_pdt[name] = cap
                m = re.match(r"seg_(\d+)\.ts$", name)
                if m:
                    if fed_log is None:
                        fed_log = list(self._fed_log)
                    if fed_log:
                        expected = round(int(m.group(1)) * TARGET_FPS * _HLS_TIME)
                        best_fid = min(
                            fed_log, key=lambda p: abs(p[0] - expected)
                        )[1]
                        self._seg_first_fid[name] = best_fid
            if len(self._seg_pdt) > 2000:  # ffmpeg 每小時 restart 會清，這只是保險
                for k in sorted(self._seg_pdt)[:-2000]:
                    self._seg_pdt.pop(k, None)
            if len(self._seg_first_fid) > 2000:
                for k in sorted(self._seg_first_fid)[:-2000]:
                    self._seg_first_fid.pop(k, None)

    def corrected_m3u8(self, date_hour: str) -> Optional[str]:
        """讀 ffmpeg 的 index.m3u8，把每個 segment 前的
        #EXT-X-PROGRAM-DATE-TIME 改寫成 _seg_pdt 記錄的真實擷取時間；
        未知的 segment 保留 ffmpeg 原值。date_hour 不是當前小時 → None
        （交由 router fallback 服務磁碟檔，避免影響歷史/已輪替小時）。"""
        if date_hour != self.out_dir.name:
            return None
        m3u8_path = self.out_dir / "index.m3u8"
        try:
            text = m3u8_path.read_text()
        except OSError:
            return None
        with self._seg_lock:
            seg_pdt = dict(self._seg_pdt)
            seg_fid = dict(self._seg_first_fid)
        out: list[str] = []
        last_pdt_idx: Optional[int] = None
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                last_pdt_idx = len(out)
                out.append(line)
                continue
            if line and not line.startswith("#"):
                seg_name = line.strip()
                cap = seg_pdt.get(seg_name)
                if cap is not None:
                    corrected = f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap)}"
                    if last_pdt_idx is not None:
                        out[last_pdt_idx] = corrected
                    else:
                        out.append(corrected)
                fid = seg_fid.get(seg_name)
                if fid is not None:
                    out.append(f"#EXT-X-PIG-FRAMEID:{fid}")
                last_pdt_idx = None
            out.append(line)
        return "\n".join(out) + "\n"

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

    def _emit_frame(self, frame: bytes, capture_ts: Optional[float]) -> bool:
        """寫一幀進 ffmpeg stdin，並在寫入那刻記 (emit_idx, capture_ts)。
        writer 等速每 tick 寫一幀（含補幀）→ emit_idx 與 ffmpeg 輸出幀
        1:1；segment NNN 首幀 == emit_idx round(NNN*TARGET_FPS*_HLS_TIME)。
        回傳 False 表示 stdin pipe 已斷（writer 應結束）。"""
        try:
            self.proc.stdin.write(frame)
            self.proc.stdin.flush()
        except BrokenPipeError:
            logger.warning(
                f"[{self.camera_id}/{self.stream_type}] ffmpeg stdin pipe broken, "
                "stream may have crashed"
            )
            return False
        except Exception as e:
            logger.warning(f"[{self.camera_id}/{self.stream_type}] stdin write error: {e}")
            return True
        if capture_ts is not None:
            self._emit_log.append((self._emit_idx, capture_ts))
        self._emit_idx += 1
        return True

    def _writer_tick(self) -> None:
        """單次：取一幀（空則複製上一幀沿用其 capture_ts）寫入 ffmpeg。
        若 _emit_frame 回傳 False（pipe 斷），設 self._stopped = True
        通知 _writer_loop 退出（本函式不回傳值）。"""
        try:
            frame = self._frame_buffer.popleft()
            self._writer_last_frame = frame
        except IndexError:
            frame = self._writer_last_frame
        if frame is not None:
            jpeg_bytes, cap = frame
            if not self._emit_frame(jpeg_bytes, cap):
                self._stopped = True

    def _writer_loop(self) -> None:
        """真實牆鐘節拍器：每 1/TARGET_FPS 真實秒寫一幀，落後過多即重置
        截止時間（不爆衝補償，避免時間軸扭曲）。長期餵入速率因此嚴格
        鎖在 TARGET_FPS×真實秒，消除造成漂移斜線的持續性速率偏差。"""
        interval = 1.0 / TARGET_FPS
        slip = getattr(settings, "hls_slip_resync_seconds", 0.5)
        deadline = time.monotonic()
        while not self._stopped:
            self._writer_tick()
            now_m = time.monotonic()
            if now_m - self._last_scan >= 0.5:
                self._last_scan = now_m
                self._scan_new_segments()
            deadline += interval
            now_m = time.monotonic()
            if now_m - deadline > slip:
                deadline = now_m
            sleep_time = deadline - now_m
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _restart(self, new_dir: Path) -> None:
        """切換到新小時目錄時重啟 ffmpeg process。"""
        self._close_proc()
        new_dir.mkdir(parents=True, exist_ok=True)
        self.proc = _start_ffmpeg(new_dir)
        self.out_dir = new_dir
        with self._seg_lock:  # 新小時、新 ffmpeg：舊 segment 對應已無意義
            self._seg_pdt.clear()
            self._seen_segs.clear()
            self._seg_first_fid.clear()
        # _fed_log/_fed_count 不在 _seg_lock 內清（沿用 feed() 不持 _seg_lock 的慣例）；
        # _restart 由持 self._lock 的 feed() 呼叫，與 writer-loop 的 scan 競態窗口極小且無害。
        self._fed_log.clear()
        self._fed_count = 0
        # TODO Task 2: 此處需 clear self._emit_log / 重置 self._emit_idx=0 / self._writer_last_frame=None（Task 2 消費 _emit_log 後不重置會使跨小時 segment 錨點全錯）
        self._last_scan = 0.0
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

    # ── PDT 偏差量測 ──────────────────────────────────────────────────────
    # ffmpeg 的 #EXT-X-PROGRAM-DATE-TIME 用「ffmpeg host 餵幀/mux 當下的牆鐘」，
    # 但前端 bbox 的 timestamp 用的是「擷取端時鐘」(camera publisher 蓋的 ts)。
    # 兩個時鐘的差 = NTP 偏差 + zmq 傳輸 + 餵入緩衝 ≈ 每個部署固定、與用戶端
    # 網路無關。在「餵 ffmpeg 的瞬間」量 server_now - capture_ts 即得此差，
    # 用 EMA 平滑後給前端做 targetTs = playingDate - offset 校正。
    _PDT_OFFSET_ALPHA: float = 0.05
    _PDT_OFFSET_MIN: float = -2.0   # 容許輕微 clock skew / 抖動
    _PDT_OFFSET_MAX: float = 30.0   # 超過視為 stale frame，丟棄不污染 EMA

    def __init__(self) -> None:
        self._streams: Dict[StreamKey, HLSStream] = {}
        self._pdt_offset: Dict[str, float] = {}
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

    def feed(
        self,
        camera_id: str,
        stream_type: str,
        jpeg_bytes: bytes,
        capture_ts: float | None = None,
        frame_id: int | None = None,
    ) -> None:
        key: StreamKey = (camera_id, stream_type)
        if capture_ts is not None and stream_type == "rgb":
            self._update_pdt_offset(camera_id, capture_ts)
        with self._lock:
            stream = self._streams.get(key)
        if stream is not None:
            stream.feed(jpeg_bytes, capture_ts, frame_id)
        else:
            logger.debug(
                f"[{camera_id}/{stream_type}] feed() called but stream not started, dropping frame"
            )

    def _update_pdt_offset(self, camera_id: str, capture_ts: float) -> None:
        sample = time.time() - capture_ts
        if sample < self._PDT_OFFSET_MIN or sample > self._PDT_OFFSET_MAX:
            return  # stale / clock-glitch frame — don't poison the EMA
        with self._lock:
            prev = self._pdt_offset.get(camera_id)
            self._pdt_offset[camera_id] = (
                sample
                if prev is None
                else self._PDT_OFFSET_ALPHA * sample
                + (1.0 - self._PDT_OFFSET_ALPHA) * prev
            )

    def corrected_m3u8(
        self, camera_id: str, stream_type: str, date_hour: str
    ) -> Optional[str]:
        """live index.m3u8 的 PDT 改寫成真實擷取時間（後端自管 PDT）。
        非當前小時 / 無對應 stream → None（router fallback 服務磁碟檔）。"""
        with self._lock:
            stream = self._streams.get((camera_id, stream_type))
        if stream is None:
            return None
        return stream.corrected_m3u8(date_hour)

    def get_pdt_offset(self, camera_id: str) -> float:
        """Seconds to subtract from hls.playingDate so the resulting time is on
        the same clock as the bbox WS `timestamp` (frame capture time)."""
        with self._lock:
            return self._pdt_offset.get(camera_id, 0.0)

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