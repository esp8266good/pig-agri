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
