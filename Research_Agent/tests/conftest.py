from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def disable_external_gemini_scheduler(monkeypatch, request):
    if Path(request.node.path).name == "test_gemini_queue.py":
        return
    from csg import gemini_queue
    monkeypatch.setattr(gemini_queue, "_reserve", lambda: {"wait_seconds": 0, "current_rpm": 0.5})
    monkeypatch.setattr(gemini_queue, "_record", lambda *_args, **_kwargs: {"current_rpm": 0.5})