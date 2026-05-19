import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from config import settings

_DISC = getattr(settings, "hls_discontinuity_seconds", 8.0)


def _iso_local(ts: float) -> str:
    dt = datetime.fromtimestamp(ts).astimezone()
    base = dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}"
    off = dt.strftime("%z")
    return f"{base}{off[:3]}:{off[3:]}"


def build_vod_m3u8(
    camera_id: str,
    stream_type: str,
    start_ts: float,
    end_ts: float,
) -> Optional[str]:
    base = Path(settings.hls_base_dir)
    start_hour = int(start_ts // 3600) * 3600
    end_hour = int(end_ts // 3600) * 3600

    # (seg_start, dur, url, is_discontinuity) — is_discontinuity marks a
    # segment whose START is discontinuous from the PREVIOUS segment.
    all_segments: list[tuple[float, float, str, bool]] = []
    max_target_duration = 4

    current_hour = start_hour
    while current_hour <= end_hour:
        dt = datetime.fromtimestamp(current_hour)
        dir_name = dt.strftime("%Y-%m-%d-%H")
        m3u8_path = base / camera_id / stream_type / dir_name / "index.m3u8"
        if m3u8_path.exists():
            segs, td = _parse_hour_m3u8(
                m3u8_path, current_hour, camera_id, stream_type, dir_name
            )
            all_segments.extend(segs)
            max_target_duration = max(max_target_duration, td)
        current_hour += 3600

    in_range = [
        (ts, dur, url, disc) for ts, dur, url, disc in all_segments
        if ts >= start_ts and ts < end_ts
    ]
    if not in_range:
        return None

    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:3",
        f"#EXT-X-TARGETDURATION:{max_target_duration}",
        "#EXT-X-PLAYLIST-TYPE:VOD",
    ]
    for ts, dur, url, disc in in_range:
        if disc:
            lines.append("#EXT-X-DISCONTINUITY")
        lines.append(f"#EXT-X-PROGRAM-DATE-TIME:{_iso_local(ts)}")
        lines.append(f"#EXTINF:{dur:.6f},")
        lines.append(url)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


def _load_sidecar(hour_dir: Path) -> dict[str, float]:
    path = hour_dir / "pdt.jsonl"
    out: dict[str, float] = {}
    try:
        for raw in path.read_text().splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                rec = json.loads(raw)
                out[rec["seg"]] = float(rec["pdt"])
            except (ValueError, KeyError, TypeError):
                continue   # 容錯：跳過半行/壞行
    except OSError:
        pass
    return out


def _parse_hour_m3u8(
    m3u8_path: Path,
    hour_unix: int,
    camera_id: str,
    stream_type: str,
    dir_name: str,
) -> tuple[list[tuple[float, float, str, bool]], int]:
    text = m3u8_path.read_text()
    td_match = re.search(r"#EXT-X-TARGETDURATION:(\d+)", text)
    target_duration = int(td_match.group(1)) if td_match else 4

    seg_names: list[str] = []
    seg_extinf: list[float] = []
    pending: Optional[float] = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#EXTINF:"):
            m = re.match(r"#EXTINF:([\d.]+),", line)
            if m:
                pending = float(m.group(1))
            continue
        if line.startswith("#"):
            continue
        if pending is None:
            continue
        seg_names.append(line)
        seg_extinf.append(pending)
        pending = None

    sidecar = _load_sidecar(m3u8_path.parent)
    segments: list[tuple[float, float, str, bool]] = []

    if seg_names and all(s in sidecar for s in seg_names):
        nominal = float(target_duration)
        for i, name in enumerate(seg_names):
            start = sidecar[name]
            url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{name}"
            disc = False
            if i > 0:
                prev_gap = start - sidecar[seg_names[i - 1]]
                if prev_gap <= 0 or prev_gap > _DISC:
                    disc = True
            if i + 1 < len(seg_names):
                nxt_gap = sidecar[seg_names[i + 1]] - start
                dur = nxt_gap if (0 < nxt_gap <= _DISC) else nominal
            else:
                dur = nominal
            segments.append((start, dur, url, disc))
        return segments, target_duration

    # 缺 sidecar（舊錄影 / thermal）→ 回退舊 hour_unix+ΣEXTINF（byte-compatible）
    accumulated = 0.0
    for name, e in zip(seg_names, seg_extinf):
        seg_start = float(hour_unix) + accumulated
        url = f"/stream/hls/{camera_id}/{stream_type}/{dir_name}/{name}"
        segments.append((seg_start, e, url, False))
        accumulated += e
    return segments, target_duration
