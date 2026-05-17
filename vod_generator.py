# vod_generator.py
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings


def build_vod_m3u8(
    camera_id: str,
    stream_type: str,
    start_ts: float,
    end_ts: float,
) -> Optional[str]:
    base = Path(settings.hls_base_dir)
    start_hour = int(start_ts // 3600) * 3600
    end_hour = int(end_ts // 3600) * 3600

    all_segments: list[tuple[float, float, str]] = []
    max_target_duration = 4

    current_hour = start_hour
    while current_hour <= end_hour:
        dt = datetime.fromtimestamp(current_hour)  # local time, aligned with hls_manager._hour_dir()
        dir_name = dt.strftime("%Y-%m-%d-%H")
        m3u8_path = base / camera_id / stream_type / dir_name / "index.m3u8"
        if m3u8_path.exists():
            segs, td = _parse_hour_m3u8(m3u8_path, current_hour, camera_id, stream_type, dir_name)
            all_segments.extend(segs)
            max_target_duration = max(max_target_duration, td)
        current_hour += 3600

    in_range = [
        (ts, dur, url) for ts, dur, url in all_segments
        if ts >= start_ts and ts < end_ts
    ]
    if not in_range:
        return None

    first_ts = in_range[0][0]
    first_dt_local = datetime.fromtimestamp(first_ts).astimezone()
    tz_str = first_dt_local.strftime("%z")  # e.g. +0800
    tz_offset = tz_str[:3] + ":" + tz_str[3:]  # e.g. +08:00
    pdt = first_dt_local.strftime("%Y-%m-%dT%H:%M:%S") + tz_offset

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max_target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-PROGRAM-DATE-TIME:{pdt}",
    ]
    for _ts, dur, url in in_range:
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _parse_hour_m3u8(
    m3u8_path: Path,
    hour_unix: int,
    camera_id: str,
    stream_type: str,
    dir_name: str,
) -> tuple[list[tuple[float, float, str]], int]:
    text = m3u8_path.read_text()

    td_match = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
    target_duration = int(td_match.group(1)) if td_match else 4

    # ffmpeg 加 program_date_time 後，#EXTINF 與 segment 檔名之間會多一行
    # #EXT-X-PROGRAM-DATE-TIME。逐行掃描，segment URI = #EXTINF 之後
    # 第一行「非 # 開頭」的非空行，避免把標籤行誤當檔名。
    segments: list[tuple[float, float, str]] = []
    accumulated = 0.0
    pending_duration: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+),", line)
            if m:
                pending_duration = float(m.group(1))
            continue
        if line.startswith("#"):
            continue
        if pending_duration is None:
            continue
        seg_start = float(hour_unix) + accumulated
        url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{line}"
        segments.append((seg_start, pending_duration, url))
        accumulated += pending_duration
        pending_duration = None

    return segments, target_duration
