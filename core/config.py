"""Central configuration, read from environment variables (and a .env file).

Settings are re-read on every call to ``get_settings()`` so tests can
monkeypatch the environment without import-order tricks.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:  # python-dotenv is a hard dependency, but keep import failures readable
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

REPO_ROOT = Path(__file__).resolve().parent.parent

if load_dotenv is not None:
    load_dotenv(REPO_ROOT / ".env", override=False)


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _path(name: str, default: Path) -> Path:
    """Path from env; relative paths are resolved against the repo root."""
    raw = _env(name)
    p = Path(raw) if raw is not None else default
    if not p.is_absolute():
        p = REPO_ROOT / p
    return p.resolve()


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- storage ---
    data_dir: Path
    db_path: Path
    out_dir: Path
    work_dir: Path
    # --- LLM ---
    anthropic_api_key: str | None
    llm_model: str | None
    llm_max_tokens: int
    llm_temperature: float | None
    # --- rendering ---
    render_backend: str  # "docker" | "local"
    sandbox_image: str
    sandbox_mount_entry: bool  # mount sandbox/render_entry.py into the container (off inside compose)
    validate_quality: str  # manim quality flag letter for validation renders
    final_quality: str  # quality for the final re-render
    validate_timeout_s: int
    final_timeout_s: int
    max_repairs: int
    render_concurrency: int
    duration_tolerance_s: float
    # --- narration ---
    tts_backend: str  # "auto" | "piper-python" | "piper-cli" | "silent"
    piper_voice: Path
    piper_bin: str | None
    segment_pad_s: float
    # --- assembly ---
    burn_captions: bool
    # --- pipeline limits ---
    max_topics: int
    worker_threads: int
    worker_poll_s: float


def get_settings() -> Settings:
    data_dir = _path("DATA_DIR", REPO_ROOT / "data")
    temp = _env("LLM_TEMPERATURE")
    return Settings(
        data_dir=data_dir,
        db_path=_path("DB_PATH", data_dir / "app.sqlite"),
        out_dir=_path("OUT_DIR", REPO_ROOT / "out"),
        work_dir=_path("WORK_DIR", data_dir / "work"),
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        llm_max_tokens=_env_int("LLM_MAX_TOKENS", 8000),
        llm_temperature=float(temp) if temp is not None else None,
        render_backend=_env("RENDER_BACKEND", "docker").lower(),
        sandbox_image=_env("SANDBOX_IMAGE", "stv-manim-sandbox:latest"),
        sandbox_mount_entry=_env_bool("SANDBOX_MOUNT_ENTRY", True),
        validate_quality=_env("VALIDATE_QUALITY", "l"),
        final_quality=_env("FINAL_QUALITY", "m"),
        validate_timeout_s=_env_int("VALIDATE_TIMEOUT_S", 180),
        final_timeout_s=_env_int("FINAL_TIMEOUT_S", 600),
        max_repairs=_env_int("MAX_REPAIRS", 3),
        render_concurrency=_env_int("RENDER_CONCURRENCY", 2),
        duration_tolerance_s=_env_float("DURATION_TOLERANCE_S", 1.0),
        tts_backend=_env("TTS_BACKEND", "auto").lower(),
        piper_voice=_path("PIPER_VOICE", REPO_ROOT / "voices" / "en_US-lessac-medium.onnx"),
        piper_bin=_env("PIPER_BIN"),
        segment_pad_s=_env_float("SEGMENT_PAD_S", 0.35),
        burn_captions=_env_bool("BURN_CAPTIONS", False),
        max_topics=_env_int("MAX_TOPICS", 8),
        worker_threads=_env_int("WORKER_THREADS", 4),
        worker_poll_s=_env_float("WORKER_POLL_S", 1.0),
    )
