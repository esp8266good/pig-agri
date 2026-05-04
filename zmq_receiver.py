import struct
import threading
from typing import Optional

import cv2
import numpy as np
import zmq
from loguru import logger

import hls_manager as hls_mod
import inference.pipeline as pipeline_mod
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
        logger.debug(
            f"[{topic}] frame={frame_id} ts={ts:.3f} "
            f"rgb={len(rgb_bytes)}B thermal={len(thermal_bytes)}B"
        )

        rgb_np: np.ndarray | None = None
        thermal_np: np.ndarray | None = None

        if rgb_bytes:
            arr = np.frombuffer(rgb_bytes, dtype=np.uint8)
            rgb_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            hls_mod.hls_manager.feed(topic, "rgb", rgb_bytes)

        if thermal_bytes:
            arr = np.frombuffer(thermal_bytes, dtype=np.uint8)
            thermal_np = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            hls_mod.hls_manager.feed(topic, "thermal", thermal_bytes)

        if rgb_np is not None:
            pipeline_mod.inference_pipeline.update_frame(topic, rgb_np, thermal_np, ts)

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
