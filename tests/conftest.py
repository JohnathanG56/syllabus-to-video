import importlib.util
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

MANIM_AVAILABLE = importlib.util.find_spec("manim") is not None
FFMPEG_AVAILABLE = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
SANDBOX_IMAGE = os.environ.get("SANDBOX_IMAGE", "stv-manim-sandbox:latest")


def _docker_ready() -> bool:
    from sandbox.run import docker_available, image_available
    return docker_available() and image_available(SANDBOX_IMAGE)


def pytest_collection_modifyitems(config, items):
    docker_ok = None
    for item in items:
        if "manim" in item.keywords and not MANIM_AVAILABLE:
            item.add_marker(pytest.mark.skip(reason="manim not importable on host"))
        if "docker" in item.keywords:
            if docker_ok is None:
                docker_ok = _docker_ready()
            if not docker_ok:
                item.add_marker(pytest.mark.skip(reason=f"docker daemon or image {SANDBOX_IMAGE} unavailable"))
        if "ffmpeg" in item.keywords and not FFMPEG_AVAILABLE:
            item.add_marker(pytest.mark.skip(reason="ffmpeg/ffprobe not installed"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated settings: every path under tmp_path, no API key, local render."""
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OUT_DIR", str(tmp_path / "out"))
    monkeypatch.delenv("DB_PATH", raising=False)
    monkeypatch.delenv("WORK_DIR", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("RENDER_BACKEND", "local")
    monkeypatch.setenv("TTS_BACKEND", "silent")
    from core.config import get_settings
    return get_settings()
