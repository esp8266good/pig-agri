from pathlib import Path

import numpy as np
import torch
from loguru import logger

import inference  # triggers sys.path setup
from fast_reid.fast_reid_interfece import FastReIDInterface

from config import settings

_PROJECT_ROOT = Path(__file__).parent.parent


class ReIDExtractor:
    def __init__(self, config_file: str, weights_path: str) -> None:
        abs_cfg = str((_PROJECT_ROOT / config_file).resolve())
        abs_wts = str((_PROJECT_ROOT / weights_path).resolve())
        _device = "cuda" if torch.cuda.is_available() else "cpu"
        self._encoder = FastReIDInterface(abs_cfg, abs_wts, _device)
        logger.info("ReIDExtractor ready")

    def extract(self, image: np.ndarray, dets: np.ndarray | None) -> np.ndarray:
        if dets is None or len(dets) == 0:
            return np.zeros((0, 2048), dtype=np.float32)
        bbox_xyxy = dets[:, :4].astype(np.float32)
        return self._encoder.inference(image, bbox_xyxy)
