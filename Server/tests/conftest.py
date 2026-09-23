"""Test setup for Server: puts Server/ on sys.path so `import app` resolves
the same way it does when uvicorn runs `app:app` from that directory, and
makes the two agent packages importable (installed editable, same as the
CI job / local venv)."""

from __future__ import annotations

import sys
from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))
