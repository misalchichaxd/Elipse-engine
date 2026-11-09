"""Pone la raíz del proyecto en sys.path para que los tests puedan hacer `import app`."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))