"""Download a Piper voice into voices/ (once).

Primary source: Hugging Face rhasspy/piper-voices. Fallback: the voice tarball
attached to the rhasspy/piper GitHub release v0.0.2 (same Lessac voice, older
file naming), for networks where huggingface.co is blocked.

Optionally (--binary) fetch the standalone Linux Piper binary (2023.11.14-2)
for machines where `pip install piper-tts` is not possible; then set
PIPER_BIN=voices/piper/piper.

Usage: python scripts/download_voice.py [--voice en_US-lessac-medium] [--binary]
"""
from __future__ import annotations

import argparse
import platform
import shutil
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VOICES = ROOT / "voices"
HF = "https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/{name}/{quality}/{voice}{ext}"
GH_VOICE = "https://github.com/rhasspy/piper/releases/download/v0.0.2/voice-en-us-lessac-medium.tar.gz"
GH_BIN = "https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_{arch}.tar.gz"


def fetch(url: str, dest: Path) -> None:
    print(f"  GET {url}")
    with urllib.request.urlopen(url, timeout=120) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)


def from_hf(voice: str) -> bool:
    lang, name, quality = voice.split("-")
    try:
        for ext in (".onnx", ".onnx.json"):
            fetch(HF.format(name=name, quality=quality, voice=voice, ext=ext), VOICES / f"{voice}{ext}")
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"  Hugging Face download failed: {exc}")
        return False


def from_github_lessac(voice: str) -> bool:
    if voice != "en_US-lessac-medium":
        return False
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "voice.tar.gz"
        try:
            fetch(GH_VOICE, tgz)
        except Exception as exc:  # noqa: BLE001
            print(f"  GitHub download failed: {exc}")
            return False
        with tarfile.open(tgz) as tf:
            tf.extractall(td, filter="data")
        shutil.copy(Path(td) / "en-us-lessac-medium.onnx", VOICES / f"{voice}.onnx")
        shutil.copy(Path(td) / "en-us-lessac-medium.onnx.json", VOICES / f"{voice}.onnx.json")
    return True


def fetch_binary() -> None:
    if platform.system() != "Linux":
        sys.exit("--binary is Linux-only; on macOS use `pip install piper-tts`.")
    arch = {"x86_64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}[platform.machine()]
    with tempfile.TemporaryDirectory() as td:
        tgz = Path(td) / "piper.tar.gz"
        fetch(GH_BIN.format(arch=arch), tgz)
        with tarfile.open(tgz) as tf:
            tf.extractall(VOICES, filter="data")
    print(f"Piper binary at {VOICES / 'piper' / 'piper'}  ->  set PIPER_BIN to this path")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--voice", default="en_US-lessac-medium")
    ap.add_argument("--binary", action="store_true", help="also fetch the standalone Linux piper binary")
    a = ap.parse_args()
    VOICES.mkdir(exist_ok=True)
    target = VOICES / f"{a.voice}.onnx"
    if target.exists() and (VOICES / f"{a.voice}.onnx.json").exists():
        print(f"voice already present: {target}")
    elif not (from_hf(a.voice) or from_github_lessac(a.voice)):
        sys.exit("could not download the voice")
    else:
        print(f"voice saved to {target}")
    if a.binary:
        fetch_binary()


if __name__ == "__main__":
    main()
