import struct
import threading
import time
from typing import Optional

import cv2
import numpy as np
import zmq
from loguru import logger

import hls_manager as hls_mod
import inference.pipeline as pipeline_mod
from config import ZmqSource, settings

# zmq_common.py
_HDR = struct.Struct("dQII")   # ts, frame_id, rgb_len, th_len

def unpack_msg(data: bytes) -> tuple[bytes, float, int, bytes, bytes]:
    sep = data.index(b"\x00")
    topic = data[:sep]
    rest  = data[sep + 1:]
    ts, frame_id, rgb_len, th_len = _HDR.unpack_from(rest)
    body  = rest[_HDR.size:]
    rgb   = body[:rgb_len]
    th    = body[rgb_len: rgb_len + th_len]
    return topic, ts, frame_id, rgb, th


# ================================================================
# 單一 source 的 worker thread
# ================================================================
def _source_worker(
    cfg:      ZmqSource,
    running:  threading.Event,
    on_frame,                  # callback(label, ts, frame_id, rgb_bytes, thermal_bytes)
) -> None:
    tag = f"[{cfg.name}]"
    logger.info(
        f"{tag} SUB → tcp://{cfg.src_host}:{cfg.src_port}  "
        f"topic='{cfg.src_topic}'  label='{cfg.label}'"
    )

    ctx  = zmq.Context()
    sock = ctx.socket(zmq.SUB)
    sock.setsockopt(zmq.LINGER,   0)
    sock.setsockopt(zmq.CONFLATE, 1)   # 只保留最新幀，不積壓
    sock.setsockopt(zmq.RCVHWM,   2)
    sock.connect(f"tcp://{cfg.src_host}:{cfg.src_port}")
    sock.setsockopt_string(zmq.SUBSCRIBE, cfg.src_topic)

    # Slow joiner fix：等訂閱 handshake 完成
    time.sleep(settings.zmq_warmup_secs)
    logger.info(f"{tag} warm-up done, entering poll loop")

    recv_count = 0
    drop_count = 0

    try:
        while running.is_set():
            if sock.poll(200) == 0:
                continue

            try:
                data = sock.recv()
            except zmq.ZMQError as e:
                logger.error(f"{tag} recv error: {e}")
                break

            try:
                _topic, ts, frame_id, rgb_bytes, thermal_bytes = unpack_msg(data)
            except Exception as e:
                logger.warning(f"{tag} unpack error: {e}")
                continue

            age_ms = (time.time() - ts) * 1000
            if age_ms > settings.zmq_stale_ms:
                drop_count += 1
                logger.debug(
                    f"{tag} drop stale frame {frame_id}, age={age_ms:.0f}ms"
                )
                continue

            on_frame(cfg.label, ts, frame_id, rgb_bytes, thermal_bytes)
            recv_count += 1

            if recv_count % 100 == 0:
                logger.info(f"{tag} recv={recv_count}, stale_drop={drop_count}")

    finally:
        sock.close()
        ctx.term()
        logger.info(f"{tag} stopped. recv={recv_count}, stale_drop={drop_count}")


# ================================================================
# ZMQReceiver：管理所有 source threads
# ================================================================
class ZMQReceiver:
    def __init__(self) -> None:
        if not settings.zmq_sources:
            raise ValueError(
                "ZMQ_SOURCES 未設定。\n"
                "請在 .env 加入，格式：\n"
                "ZMQ_SOURCES=name:host:port:src_topic:label;..."
            )
        self._sources  = settings.zmq_sources
        self._running  = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self._running.set()
        for cfg in self._sources:
            t = threading.Thread(
                target = _source_worker,
                args   = (cfg, self._running, self._on_frame),
                name   = cfg.name,
                daemon = True,
            )
            t.start()
            self._threads.append(t)
        logger.info(f"ZMQReceiver started ({len(self._threads)} source(s))")

    def stop(self) -> None:
        self._running.clear()
        for t in self._threads:
            t.join(timeout=3)
            if t.is_alive():
                logger.warning(f"Thread '{t.name}' did not stop in time")
        self._threads.clear()
        logger.info("ZMQReceiver stopped")

    # ── 所有 source thread 共用這個 callback ──────────────────────
    def _on_frame(
        self,
        label:         str,
        ts:            float,
        frame_id:      int,
        rgb_bytes:     bytes,
        thermal_bytes: bytes,
    ) -> None:
        logger.debug(
            f"[{label}] frame={frame_id} ts={ts:.3f} "
            f"rgb={len(rgb_bytes)}B thermal={len(thermal_bytes)}B"
        )

        rgb_np:     Optional[np.ndarray] = None
        thermal_np: Optional[np.ndarray] = None

        if rgb_bytes:
            arr    = np.frombuffer(rgb_bytes, dtype=np.uint8)
            rgb_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            hls_mod.hls_manager.feed(label, "rgb", rgb_bytes, capture_ts=ts, frame_id=frame_id)

        if thermal_bytes:
            arr        = np.frombuffer(thermal_bytes, dtype=np.uint8)
            thermal_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            hls_mod.hls_manager.feed(label, "thermal", thermal_bytes)

        if rgb_np is not None:
            pipeline_mod.inference_pipeline.update_frame(
                label, rgb_np, thermal_np, ts, frame_id
            )


zmq_receiver = ZMQReceiver()