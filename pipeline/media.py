"""ffprobe helpers."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


class MediaError(RuntimeError):
    pass


def probe_duration(path: Path) -> float:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {r.stderr.strip()[-300:]}")
    try:
        return float(json.loads(r.stdout)["format"]["duration"])
    except (KeyError, ValueError) as exc:
        raise MediaError(f"no duration for {path}") from exc


def probe_streams(path: Path) -> list[dict]:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise MediaError(f"ffprobe failed on {path}: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout).get("streams", [])
