import sys
import numpy as np
from unittest.mock import MagicMock, patch

for _mod in [
    "fast_reid", "fast_reid.fast_reid_interfece",
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_extractor(mock_encoder=None):
    from inference.reid_extractor import ReIDExtractor
    e = ReIDExtractor.__new__(ReIDExtractor)
    e._encoder = mock_encoder or MagicMock()
    return e


def test_extract_empty_dets_returns_zeros():
    e = _make_extractor()
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = e.extract(img, np.zeros((0, 4), dtype=np.float32))
    assert result.shape == (0, 2048)
    e._encoder.inference.assert_not_called()


def test_extract_calls_encoder_with_dets():
    mock_enc = MagicMock()
    mock_enc.inference.return_value = np.ones((2, 2048), dtype=np.float32)
    e = _make_extractor(mock_enc)
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    dets = np.array([[10, 20, 50, 80], [100, 150, 200, 250]], dtype=np.float32)
    result = e.extract(img, dets)
    mock_enc.inference.assert_called_once()
    assert result.shape == (2, 2048)


def test_extract_none_dets_returns_zeros():
    e = _make_extractor()
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    result = e.extract(img, None)
    assert result.shape == (0, 2048)
