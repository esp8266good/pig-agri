import sys
import numpy as np
from unittest.mock import MagicMock, patch

# Mock HybridSORT modules（無法在無 GPU / 未編譯環境中 import）
for _mod in [
    "yolox", "yolox.exp", "yolox.utils", "yolox.data", "yolox.data.data_augment",
]:
    if _mod not in sys.modules:
        sys.modules[_mod] = MagicMock()


def _make_detector():
    """建立 BatchDetector instance，跳過 __init__（不需要真實模型）"""
    from inference.batch_detector import BatchDetector
    d = BatchDetector.__new__(BatchDetector)
    d._model = MagicMock()
    d._test_size = (736, 1280)
    d._num_classes = 1
    d._confthre = 0.1
    d._nmsthre = 0.7
    d._device = "cpu"
    return d


def test_infer_empty_list_returns_empty():
    d = _make_detector()
    assert d.infer([]) == []


def test_test_size_property():
    d = _make_detector()
    assert d.test_size == (736, 1280)


def test_infer_returns_none_when_no_detections():
    import inference.batch_detector as bd_mod
    d = _make_detector()
    fake_img = np.zeros((480, 640, 3), dtype=np.uint8)
    with patch.object(bd_mod, "preproc", return_value=(np.zeros((3, 736, 1280)), 0.5, None)), \
         patch.object(bd_mod, "postprocess", return_value=[None]), \
         patch("torch.no_grad"):
        d._model.return_value = MagicMock()
        result = d.infer([fake_img])
    assert result == [None]


def test_infer_returns_ndarray_when_detections_exist():
    import inference.batch_detector as bd_mod
    d = _make_detector()
    fake_img = np.zeros((480, 640, 3), dtype=np.uint8)
    fake_dets = MagicMock()
    fake_dets.cpu.return_value.numpy.return_value = np.ones((3, 7), dtype=np.float32)
    with patch.object(bd_mod, "preproc", return_value=(np.zeros((3, 736, 1280)), 0.5, None)), \
         patch.object(bd_mod, "postprocess", return_value=[fake_dets]), \
         patch("torch.no_grad"):
        d._model.return_value = MagicMock()
        result = d.infer([fake_img])
    assert result[0].shape == (3, 7)
