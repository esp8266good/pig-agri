import sys
from pathlib import Path

_REF_DIR = Path(__file__).parent.parent / "ref" / "HybridSORT"
if str(_REF_DIR) not in sys.path:
    sys.path.insert(0, str(_REF_DIR))
