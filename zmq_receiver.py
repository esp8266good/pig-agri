import struct
import threading
from typing import Optional

import zmq
from loguru import logger

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
        logger.info("ZMQ receiver stopped")

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
                if len(parts) < 4:
                    continue
                topic = parts[0].decode()
                ts, frame_id = struct.unpack("dQ", parts[1])
                logger.info(
                    f"[{topic}] frame={frame_id} ts={ts:.3f} "
                    f"rgb={len(parts[2])}B thermal={len(parts[3])}B"
                )
            except Exception as e:
                logger.error(f"ZMQ error: {e}")

        sock.close()
        ctx.term()


zmq_receiver = ZMQReceiver()
