import json
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
# 2. 移除「輸出端 -vf fps 重採樣器」（造成漸進漂移的元兇）；但保留「輸入端
#    -framerate=TARGET_FPS」：writer 真實牆鐘節拍器每秒確實只餵 TARGET_FPS 幀，
#    不告知 mjpeg pipe demuxer 它會預設 25fps 打 PTS → .ts 被以 25/TARGET_FPS
#    倍速燒進播放。宣告真實輸入速率使媒體 PTS≡牆鐘，且不重引入舊漂移。
# 3. 移除 -tune zerolatency：不需要低延遲，穩定性優先
# 4. 加上 -g (GOP size) = 2 * FPS：讓 HLS 切割更整齊
TARGET_FPS: int = getattr(settings, "hls_target_fps", 25)
FFMPEG_LOG_LEVEL: str = getattr(settings, "ffmpeg_log_level", "warning")  # debug/info/warning/error/quiet
# segment 時長（與 _make_ffmpeg_cmd 的 -hls_time 一致）
_HLS_TIME: int = 4
# _emit_log 環形上限（約 30 分鐘餘量，遠超單一小時所需）
_FED_LOG_MAX: int = TARGET_FPS * 1800
# 錄影監督者：串流僅在「近期確實有送幀」時才確保（rgb 與 thermal 皆然）。
# 斷線／從未連上的攝影機不會被平白 mkdir 出小時目錄 + 空 ffmpeg（否則前端
# timeline 會把空目錄誤判成有錄影片段）。窗須小於 watchdog 逐出門檻
# （NO_FRAME_TIMEOUT=30s），否則會在「已逐出但仍算近期」的空窗重建出空目錄。
_RECORDING_SEEN_WINDOW: float = 20.0
# 非單調 capture_ts 的 clamp 增量
_PDT_MONOTONIC_EPS: float = 1e-3


def _iso_local(ts: float) -> str:
    """Unix ts → 本地時區 ISO8601（毫秒 + +HH:MM），對齊前端 hls.playingDate
    與 vod_generator 的 PDT 格式。"""
    dt = datetime.fromtimestamp(ts).astimezone()
    base = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    off = dt.strftime("%z")  # e.g. +0800
    return f"{base}{off[:3]}:{off[3:]}"


def _make_ffmpeg_cmd(out_dir: Path, start_number: int = 0, *,
                     rolling: bool = False) -> list[str]:
    gop = TARGET_FPS * 2
    crf = str(getattr(settings, "hls_crf", 23))
    codec = getattr(settings, "hls_video_codec", "libx264")
    # rolling=True（ephemeral live）：限制清單長度 + delete_segments → 滾動視窗、
    # 舊段自動刪、磁碟佔用恆定為數秒。rolling=False（錄影）：全留（現狀）。
    list_size = "8" if rolling else "0"
    flags = "append_list+program_date_time"
    if rolling:
        flags = "delete_segments+" + flags
    return [
        "ffmpeg", "-y",
        "-f", "mjpeg",
        "-framerate", str(TARGET_FPS),  # 輸入速率（writer 已鎖此真實速率）；非輸出 -vf fps
        "-i", "pipe:0",
        "-an",
        "-c:v", codec,
        "-preset", "veryfast",
        "-crf", crf,
        "-g", str(gop),
        "-hls_time", str(_HLS_TIME),
        "-hls_list_size", list_size,
        "-hls_flags", flags,
        "-hls_segment_filename", str(out_dir / "seg_%03d.ts"),
        # start_number 用於 _restart_in_place 接續編號：HLS spec 要求 segment URI 不可變，
        # ffmpeg 中途死後復生不能用 seg_000 覆蓋既存段，須從 max+1 起算。
        "-start_number", str(start_number),
        "-loglevel", FFMPEG_LOG_LEVEL,
        str(out_dir / "index.m3u8"),
    ]


def _start_ffmpeg(out_dir: Path, start_number: int = 0, *,
                  rolling: bool = False) -> subprocess.Popen:
    stderr_target = (
        subprocess.PIPE
        if FFMPEG_LOG_LEVEL in ("debug", "info", "verbose")
        else subprocess.DEVNULL
    )
    proc = subprocess.Popen(
        _make_ffmpeg_cmd(out_dir, start_number=start_number, rolling=rolling),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=stderr_target,
    )
    if stderr_target == subprocess.PIPE:
        threading.Thread(
            target=_drain_stderr, args=(proc,), daemon=True,
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

import storage_monitor as _sm
_EPHEMERAL_BASE: str = _sm.effective_ephemeral_dir(
    getattr(settings, "hls_ephemeral_dir", "/dev/shm/pig_live")
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

        # 真實時間軸授權：每寫入 ffmpeg 一幀記 (emit_idx, capture_ts)，
        # 與 ffmpeg 輸出幀 1:1（writer 等速 tick）。_scan_new_segments
        # 據此推每段首幀真實擷取牆鐘。
        self._emit_idx: int = 0
        self._emit_log: deque[tuple[int, float]] = deque(maxlen=_FED_LOG_MAX)
        self._writer_last_frame: Optional[tuple[bytes, Optional[float]]] = None

        # 序列化 writer._emit_frame 的 stdin 寫入 vs _restart/_restart_in_place 的
        # proc swap：避免 writer 寫到剛被 close 的 stdin 觸發 BrokenPipeError → 過去
        # 這是 race 殺死 writer thread、造成 8 小時 segment 空檔的根因。
        self._proc_lock = threading.Lock()
        # _restart_in_place（ffmpeg 中途死復生）用 -start_number 接續舊 segment 編號；
        # _scan_new_segments 需扣除此偏移才能把新段名 (seg_<offset>.ts) 映射到新 ffmpeg
        # 內部的 emit_idx=0 對應 capture_ts。hour rollover (_restart) 重置為 0。
        self._seg_index_offset: int = 0
        self._revive_count: int = 0  # 觀測 ffmpeg 中途死的次數

        # 模式感知（storage_monitor.target_mode）：record（小時目錄全留）/
        # ephemeral（_live 滾動 buffer）。drop 由 feed/writer 守衛處理。
        self.mode: str = "record"
        self.rolling: bool = False
        self._dropped_frames: int = 0

        # 啟動 writer 執行緒，以固定節奏把 buffer 裡的幀送進 ffmpeg
        self._start_writer()

    def _start_writer(self) -> None:
        self._writer_thread = threading.Thread(
            target=self._writer_loop,
            daemon=True,
            name=f"hls-writer-{self.camera_id}-{self.stream_type}",
        )
        self._writer_thread.start()

    # ── 公開方法 ──────────────────────────────────────────────────────────

    def feed(
        self,
        jpeg_bytes: bytes,
        capture_ts: Optional[float] = None,
    ) -> None:
        """把新幀放入 buffer。依 target_mode 切換輸出目標：drop→丟幀；
        ephemeral/record 目標目錄變更→_restart。capture_ts 為真實擷取牆鐘。"""
        mode, target = self._desired_target()
        if mode == "drop":
            # drop = 錄影碟與 ephemeral 碟同時不可寫（雙重故障）。丟幀且刻意不更新
            # last_feed_time → 30s 後 watchdog 逐出此 stream。磁碟恢復後，若當下無人
            # 觀看（無 /live 請求觸發 ensure_started 重建 stream），錄影要等下一個
            # /live 請求才自動續錄；有觀看者則前端 12s checkLiveHandoff 會自癒。
            self._dropped_frames += 1
            return
        with self._lock:
            if mode != self.mode or target != self.out_dir:
                self._restart(target, rolling=(mode == "ephemeral"), mode=mode)
        if capture_ts is not None:
            self._last_capture_ts = capture_ts
        self.last_feed_time = time.time()
        self._frame_buffer.append((jpeg_bytes, capture_ts))
        self._buffer_event.set()

    def _scan_new_segments(self) -> None:
        """偵測 out_dir 新出現的 seg_*.ts，用 _emit_log 推該段首幀真實
        擷取牆鐘（emit_idx ≈ NNN*TARGET_FPS*_HLS_TIME），存 _seg_pdt 並
        append 到 sidecar pdt.jsonl（VOD 跨小時讀得到）。非單調則 clamp。"""
        out_dir = self.out_dir  # 快照：防止 _restart（另一執行緒）切換目錄後
                                # sidecar 寫到新小時的目錄但用舊小時的 seg 名稱
        try:
            names = sorted(p.name for p in out_dir.glob("seg_*.ts"))
        except OSError:
            return
        with self._seg_lock:
            emit_log = None
            new_rows: list[tuple[str, float]] = []
            for name in names:
                if name in self._seen_segs:
                    continue
                self._seen_segs.add(name)
                m = re.match(r"seg_(\d+)\.ts$", name)
                if not m:
                    continue
                # _seg_index_offset：_restart_in_place 復生時讓新 ffmpeg
                # 從 -start_number=offset 開始；新段名為 seg_<offset+k>.ts，
                # 但其首幀對應的是新 ffmpeg 內部 emit_idx = k*TARGET_FPS*_HLS_TIME。
                # hour rollover 重置 offset=0、segment 編號從 seg_000 起算 → 行為不變。
                relative = int(m.group(1)) - self._seg_index_offset
                if relative < 0:
                    continue  # 舊 ffmpeg 寫的段，PDT 應該已記錄
                expected = round(relative * TARGET_FPS * _HLS_TIME)
                if emit_log is None:
                    emit_log = list(self._emit_log)
                if emit_log:
                    cap = min(emit_log, key=lambda p: abs(p[0] - expected))[1]
                    if self._seg_pdt:
                        # global max == 前一 ordinal 的 PDT：ffmpeg 依序產生 segment，
                        # 且本次掃描已 sorted(names)，故全域 max 即上一段時間，可安全
                        # 用來強制 PDT 單調（避免回退使 hls.js 拒絕非單調 PDT）。
                        prev = max(self._seg_pdt.values())
                        if cap <= prev:
                            cap = prev + _PDT_MONOTONIC_EPS
                    self._seg_pdt[name] = cap
                    new_rows.append((name, cap))
            if len(self._seg_pdt) > 2000:
                for k in sorted(self._seg_pdt)[:-2000]:
                    self._seg_pdt.pop(k, None)
        for seg_name, cap in new_rows:
            if self.rolling:
                continue  # ephemeral：不寫 pdt.jsonl（夜間不需 VOD、省寫入）
            try:
                with (out_dir / "pdt.jsonl").open("a") as fh:
                    fh.write(json.dumps({"seg": seg_name, "pdt": cap}) + "\n")
            except OSError as e:
                logger.warning(f"[{self.camera_id}/{self.stream_type}] sidecar write failed: {e}")

    def corrected_m3u8(self, date_hour: str) -> Optional[str]:
        """live index.m3u8：每段 #EXT-X-PROGRAM-DATE-TIME 改寫成 _seg_pdt
        真實擷取時間，#EXTINF 改為相鄰段 PDT 差；差 > 不連續門檻則於該段
        前插 #EXT-X-DISCONTINUITY 並用 nominal _HLS_TIME。非當前小時→None。"""
        if date_hour != self.out_dir.name:
            return None
        m3u8_path = self.out_dir / "index.m3u8"
        try:
            text = m3u8_path.read_text()
        except OSError:
            return None
        with self._seg_lock:
            seg_pdt = dict(self._seg_pdt)
        disc = getattr(settings, "hls_discontinuity_seconds", 8.0)
        seg_order = [
            ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.startswith("#")
        ]

        def _dur(name: str, nxt: Optional[str]) -> tuple[float, bool]:
            a = seg_pdt.get(name)
            b = seg_pdt.get(nxt) if nxt else None
            if a is None or b is None:
                return float(_HLS_TIME), False
            gap = b - a
            if gap <= 0 or gap > disc:
                return float(_HLS_TIME), True
            return gap, False

        out: list[str] = []
        last_pdt_idx: Optional[int] = None
        pending_extinf_idx: Optional[int] = None
        pending_disc: bool = False
        for raw in text.splitlines():
            line = raw.rstrip("\r")
            if line.startswith("#EXT-X-PROGRAM-DATE-TIME:"):
                last_pdt_idx = len(out)
                out.append(line)
                continue
            if line.startswith("#EXTINF:"):
                pending_extinf_idx = len(out)
                out.append(line)
                continue
            if line and not line.startswith("#"):
                seg_name = line.strip()
                idx = seg_order.index(seg_name) if seg_name in seg_order else -1
                nxt = seg_order[idx + 1] if 0 <= idx < len(seg_order) - 1 else None
                cap = seg_pdt.get(seg_name)
                if cap is not None:
                    # RFC 8216 §4.3.2.3: DISCONTINUITY belongs immediately before
                    # the segment that begins after the gap, not before the segment
                    # whose PDT-distance-to-next is large.  pending_disc carries the
                    # flag computed for the *previous* segment's gap forward to THIS
                    # segment's insertion point.
                    if pending_disc:
                        ins = pending_extinf_idx if pending_extinf_idx is not None else len(out)
                        out.insert(ins, "#EXT-X-DISCONTINUITY")
                        # Indices shifted by 1 after insert — adjust pending_extinf_idx
                        if pending_extinf_idx is not None:
                            pending_extinf_idx += 1
                        if last_pdt_idx is not None:
                            last_pdt_idx += 1
                    corrected = f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(cap)}"
                    if last_pdt_idx is not None:
                        out[last_pdt_idx] = corrected
                    else:
                        out.append(corrected)
                    dur, is_disc = _dur(seg_name, nxt)
                    # When this segment itself is discontinuous from the previous one
                    # (pending_disc was True), its EXTINF must use nominal _HLS_TIME —
                    # the real PDT-diff to the previous segment is meaningless across a gap.
                    if pending_disc:
                        dur = float(_HLS_TIME)
                    if pending_extinf_idx is not None:
                        out[pending_extinf_idx] = f"#EXTINF:{dur:.6f},"
                    # Carry is_disc forward: DISC will be emitted before the NEXT segment.
                    pending_disc = is_disc
                else:
                    # Unknown segment — don't bleed a stale flag past a no-PDT segment.
                    pending_disc = False
                last_pdt_idx = None
                pending_extinf_idx = None
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

    def _desired_target(self) -> tuple[str, "Optional[Path]"]:
        """依 storage_monitor.target_mode 決定 (mode, out_dir)。drop → (drop, None)。"""
        import storage_monitor
        mode = storage_monitor.get_target_mode()
        if mode == "drop":
            return "drop", None
        if mode == "ephemeral":
            return "ephemeral", (
                Path(_EPHEMERAL_BASE) / self.camera_id / self.stream_type / "_live"
            )
        return "record", self._hour_dir()

    def _emit_frame(self, frame: bytes, capture_ts: Optional[float]) -> bool:
        """寫一幀進 ffmpeg stdin，並在寫入那刻記 (emit_idx, capture_ts)。
        writer 等速每 tick 寫一幀（含補幀）→ emit_idx 與 ffmpeg 輸出幀
        1:1；segment NNN 首幀 == emit_idx round(NNN*TARGET_FPS*_HLS_TIME)。
        回傳 False 表示這次寫入失敗（pipe 斷／stdin 已關），由下一輪 writer_tick
        的 proc.poll() 健康檢查走 _restart_in_place 復生路徑。**不再設
        self._stopped**——那是過去殺死 writer 導致 8 小時 gap 的根因。"""
        with self._proc_lock:
            try:
                self.proc.stdin.write(frame)
                self.proc.stdin.flush()
            except BrokenPipeError:
                logger.warning(
                    f"[{self.camera_id}/{self.stream_type}] ffmpeg stdin pipe broken, "
                    "will revive on next tick"
                )
                return False
            except ValueError:
                # stdin 已 close（_close_proc 與 write 競態），同樣由下輪 poll 處理
                return False
            except Exception as e:
                logger.warning(f"[{self.camera_id}/{self.stream_type}] stdin write error: {e}")
                return True
        if capture_ts is not None:
            self._emit_log.append((self._emit_idx, capture_ts))
        self._emit_idx += 1
        return True

    def _writer_tick(self) -> None:
        """單次：(1) 若 ffmpeg 已死則原地復生並退出本 tick；(2) 取一幀（空
        則複製上一幀沿用其 capture_ts）寫入 ffmpeg。_emit_frame 失敗不再
        設 _stopped，交給下輪 proc.poll() 自癒。"""
        import storage_monitor
        if storage_monitor.get_target_mode() == "drop":
            return  # drop：不 emit、不 revive（磁碟死時不 spawn 失敗 ffmpeg）
        if self.proc.poll() is not None:
            try:
                self._restart_in_place()
            except Exception as e:
                logger.error(
                    f"[{self.camera_id}/{self.stream_type}] revive failed: {e}"
                )
                time.sleep(2.0)  # 退避，避免 spawn 連續失敗時緊密重試
            return
        try:
            frame = self._frame_buffer.popleft()
            self._writer_last_frame = frame
        except IndexError:
            frame = self._writer_last_frame
        if frame is not None:
            jpeg_bytes, cap = frame
            self._emit_frame(jpeg_bytes, cap)
            # 回傳值刻意 ignore：失敗→下輪 poll 偵測→revive

    def _writer_loop(self) -> None:
        """真實牆鐘節拍器：每 1/TARGET_FPS 真實秒寫一幀，落後過多即重置
        截止時間（不爆衝補償，避免時間軸扭曲）。長期餵入速率因此嚴格
        鎖在 TARGET_FPS×真實秒，消除造成漂移斜線的持續性速率偏差。
        所有 exception 一律 catch + log，writer thread 只能由 stop() 終結
        （任何未捕捉錯誤造成 writer 死亡是 8 小時 gap bug 的同源失敗模式）。"""
        interval = 1.0 / TARGET_FPS
        slip = getattr(settings, "hls_slip_resync_seconds", 0.5)
        deadline = time.monotonic()
        while not self._stopped:
            try:
                self._writer_tick()
                now_m = time.monotonic()
                if now_m - self._last_scan >= 0.5:
                    self._last_scan = now_m
                    self._scan_new_segments()
            except Exception as e:
                logger.exception(
                    f"[{self.camera_id}/{self.stream_type}] writer tick exception: {e}"
                )
            deadline += interval
            now_m = time.monotonic()
            if now_m - deadline > slip:
                deadline = now_m
            sleep_time = deadline - now_m
            if sleep_time > 0:
                time.sleep(sleep_time)

    def _restart(self, new_dir: Path, *, rolling: bool = False,
                 mode: str = "record") -> None:
        """切換輸出目標（小時 rollover 或 record↔ephemeral 模式切換）時重啟 ffmpeg。"""
        with self._proc_lock:
            self._close_proc()
            try:
                new_dir.mkdir(parents=True, exist_ok=True)
                self.proc = _start_ffmpeg(new_dir, rolling=rolling)
            except OSError as e:
                # 整點換目錄 / 模式切換失敗：不可向上拋（會冒泡到 feed →
                # zmq thread 死）。保留舊 out_dir，交給 writer poll() 自癒
                # 或錄影監督者下一輪重建。
                logger.warning(
                    f"[{self.camera_id}/{self.stream_type}] _restart 失敗（{e}），"
                    "保留舊狀態待自癒"
                )
                return
            self.out_dir = new_dir
            self.mode = mode
            self.rolling = rolling
        with self._seg_lock:
            self._seg_pdt.clear()
            self._seen_segs.clear()
        self._emit_log.clear()
        self._emit_idx = 0
        self._seg_index_offset = 0
        self._writer_last_frame = None
        self._last_scan = 0.0
        logger.info(
            f"HLS {self.camera_id}/{self.stream_type} → {new_dir} (mode={mode})"
        )

    def _restart_in_place(self) -> None:
        """ffmpeg 中途死（OOM/libx264 internal/被 oom-killer 殺）→ 原小時
        目錄原地復生新 ffmpeg，用 -start_number=(max現存編號+1) 接續編號避免
        覆蓋舊段（HLS spec 要求 segment URI 不可變）。_emit_log/_emit_idx 重置
        為 0：新 ffmpeg 從它自己的 frame 0 開始計算輸出 segment 內部位置；
        _seg_index_offset 記錄 start_number，供 _scan_new_segments 映射段名
        →新 ffmpeg 內 emit_idx。_seg_pdt 不清——舊段 PDT 仍有效。"""
        next_num = 0
        try:
            nums: list[int] = []
            for p in self.out_dir.glob("seg_*.ts"):
                m = re.match(r"seg_(\d+)\.ts$", p.name)
                if m:
                    nums.append(int(m.group(1)))
            if nums:
                next_num = max(nums) + 1
        except OSError:
            pass
        with self._proc_lock:
            self._close_proc()
            self.proc = _start_ffmpeg(self.out_dir, start_number=next_num,
                                      rolling=self.rolling)
        self._emit_log.clear()
        self._emit_idx = 0
        self._seg_index_offset = next_num
        self._writer_last_frame = None
        self._last_scan = 0.0
        self._revive_count += 1
        logger.warning(
            f"[{self.camera_id}/{self.stream_type}] revived dead ffmpeg "
            f"in-place (count={self._revive_count}, start_number={next_num})"
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
        self._last_seen: Dict[StreamKey, float] = {}
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
        import storage_monitor
        key: StreamKey = (camera_id, stream_type)
        with self._lock:
            if key not in self._streams:
                mode = storage_monitor.get_target_mode()
                rolling = mode == "ephemeral"
                if rolling:
                    out_dir = Path(_EPHEMERAL_BASE) / camera_id / stream_type / "_live"
                else:
                    out_dir = (Path(settings.hls_base_dir) / camera_id / stream_type
                               / datetime.now().strftime("%Y-%m-%d-%H"))
                try:
                    out_dir.mkdir(parents=True, exist_ok=True)
                except OSError as e:
                    # 錄影碟在此刻不可寫（cold-start 早於 monitor 首輪、或碟掛了）→
                    # 依設計降級 ephemeral live（寫健康的 ephemeral base），不讓 /live 噴 500。
                    logger.warning(
                        f"[{camera_id}/{stream_type}] record dir mkdir 失敗（{e}）→ 降級 ephemeral live"
                    )
                    rolling = True
                    mode = "ephemeral"
                    out_dir = Path(_EPHEMERAL_BASE) / camera_id / stream_type / "_live"
                    out_dir.mkdir(parents=True, exist_ok=True)
                proc = _start_ffmpeg(out_dir, rolling=rolling)
                stream = HLSStream(camera_id, stream_type, proc, out_dir)
                # stream.mode 追蹤這條 ffmpeg 的實際輸出狀態（不是瞬時 storage 決策）；
                # drop 不是合法的 ffmpeg 輸出模式，故落到 record（feed/writer 守衛會處理丟幀）。
                stream.mode = mode if mode != "drop" else "record"
                stream.rolling = rolling
                self._streams[key] = stream
                logger.info(f"Started HLS stream {camera_id}/{stream_type} → {out_dir} (mode={mode})")
            return self._streams[key].out_dir

    def active_out_dir(self, camera_id: str, stream_type: str,
                       date_hour: str) -> "Optional[Path]":
        """若有 active stream 且其 out_dir.name 命中 date_hour（含 ephemeral '_live'）
        → 回該 out_dir（serve_hls 據此撈 .ts，不限定 hls_base）；否則 None。"""
        with self._lock:
            stream = self._streams.get((camera_id, stream_type))
        if stream is not None and stream.out_dir.name == date_hour:
            return stream.out_dir
        return None

    def feed(
        self,
        camera_id: str,
        stream_type: str,
        jpeg_bytes: bytes,
        capture_ts: float | None = None,
    ) -> None:
        key: StreamKey = (camera_id, stream_type)
        self._last_seen[key] = time.time()
        with self._lock:
            stream = self._streams.get(key)
        if stream is not None:
            stream.feed(jpeg_bytes, capture_ts)
        else:
            logger.debug(
                f"[{camera_id}/{stream_type}] feed() called but stream not started, dropping frame"
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

    def has_stream(self, camera_id: str, stream_type: str) -> bool:
        with self._lock:
            return (camera_id, stream_type) in self._streams

    def desired_recording_keys(self, cameras: list[str]) -> list[StreamKey]:
        """錄影監督者要確保的串流：rgb 與 thermal 皆僅當該攝影機近期
        （_RECORDING_SEEN_WINDOW 秒內）確實送過該類型的幀才納入。斷線／
        從未連上的攝影機不納入 → 不會被建出空的小時錄影目錄。"""
        now = time.time()
        keys: list[StreamKey] = []
        for cam in cameras:
            for stype in ("rgb", "thermal"):
                seen = self._last_seen.get((cam, stype))
                if seen is not None and now - seen <= _RECORDING_SEEN_WINDOW:
                    keys.append((cam, stype))
        return keys

    def active_types_map(self, cameras: list[str]) -> dict[str, list[str]]:
        """每台攝影機近期（_RECORDING_SEEN_WINDOW 秒內）有送幀的串流型別。
        供 /cameras 曝露給前端判斷 thermal 是否有來源（無來源 → 無訊號佔位）。"""
        now = time.time()
        return {
            cam: [stype for stype in ("rgb", "thermal")
                  if (seen := self._last_seen.get((cam, stype))) is not None
                  and now - seen <= _RECORDING_SEEN_WINDOW]
            for cam in cameras
        }


# ─── 初始化 ──────────────────────────────────────────────────────────────────
# 從 settings 讀取 log 等級（預設 INFO）
configure_logging(getattr(settings, "log_level", "INFO"))

hls_manager = HLSManager()