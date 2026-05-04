from pathlib import Path

import numpy as np
import torch
from loguru import logger

import inference  # triggers sys.path setup
from yolox.data.data_augment import preproc
from yolox.exp import get_exp
from yolox.utils import postprocess

from config import settings

_PROJECT_ROOT = Path(__file__).parent.parent
_RGB_MEANS = (0.485, 0.456, 0.406)
_STD = (0.229, 0.224, 0.225)


class BatchDetector:
    def __init__(self, ckpt_path: str, exp_file: str) -> None:
        abs_exp = str((_PROJECT_ROOT / exp_file).resolve())
        exp = get_exp(abs_exp, None)
        self._test_size: tuple[int, int] = exp.test_size
        self._num_classes: int = exp.num_classes
        self._confthre: float = exp.test_conf
        self._nmsthre: float = exp.nmsthre
        self._device = torch.device(settings.device if torch.cuda.is_available() else "cpu")

        model = exp.get_model().to(self._device)
        model.eval()
        abs_ckpt = str((_PROJECT_ROOT / ckpt_path).resolve())
        ckpt = torch.load(abs_ckpt, map_location="cpu")
        model.load_state_dict(ckpt["model"])
        self._model = model

        vram_gb = torch.cuda.memory_allocated() / 1e9 if torch.cuda.is_available() else 0.0
        logger.info(f"BatchDetector ready on {self._device}, VRAM used: {vram_gb:.2f} GB")

    @property
    def test_size(self) -> tuple[int, int]:
        return self._test_size

    def infer(self, images: list[np.ndarray]) -> list[np.ndarray | None]:
        if not images:
            return []
        batch_tensors = []
        for img in images:
            t, _ratio, _raw = preproc(img, self._test_size, _RGB_MEANS, _STD)
            batch_tensors.append(t)
        batch = torch.from_numpy(np.stack(batch_tensors)).float().to(self._device)
        with torch.no_grad():
            raw = self._model(batch)
        outputs = postprocess(raw, self._num_classes, self._confthre, self._nmsthre)
        results: list[np.ndarray | None] = []
        for out in outputs:
            results.append(None if out is None else out.cpu().numpy())
        return results
