"""Stage 3: narration FIRST. Synthesize one WAV per segment with Piper and
measure exact durations; the animation code is then generated to fit them.

Backends (TTS_BACKEND):
  piper-python  `pip install piper-tts` (supports the 1.2 and 1.3+ APIs)
  piper-cli     the standalone Piper binary (PIPER_BIN=/path/to/piper)
  silent        silent WAVs sized at ~2.5 words/s - for tests/CI/dry runs
  auto          piper-python if importable, else piper-cli if PIPER_BIN is set
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import threading
import wave
from abc import ABC, abstractmethod
from pathlib import Path

from core.schemas import WORDS_PER_SECOND, LessonScript

SAMPLE_RATE = 22050


class NarrationError(RuntimeError):
    pass


class TTS(ABC):
    name = "abstract"

    @abstractmethod
    def synthesize(self, text: str, wav_path: Path) -> None:
        ...


class PiperPythonTTS(TTS):
    name = "piper-python"

    def __init__(self, voice_path: Path):
        _require_voice(voice_path)
        from piper import PiperVoice  # lazy: optional dependency
        self.voice = PiperVoice.load(str(voice_path))
        self._lock = threading.Lock()  # one synthesis at a time per loaded voice

    def synthesize(self, text: str, wav_path: Path) -> None:
        with self._lock, wave.open(str(wav_path), "wb") as wf:
            if hasattr(self.voice, "synthesize_wav"):  # piper-tts >= 1.3
                self.voice.synthesize_wav(text, wf)
            else:  # piper-tts 1.2.x
                self.voice.synthesize(text, wf)


class PiperCLITTS(TTS):
    name = "piper-cli"

    def __init__(self, binary: str, voice_path: Path, timeout_s: float = 120):
        _require_voice(voice_path)
        if shutil.which(binary) is None and not Path(binary).exists():
            raise NarrationError(f"piper binary not found: {binary}")
        self.binary, self.voice_path, self.timeout_s = binary, voice_path, timeout_s

    def synthesize(self, text: str, wav_path: Path) -> None:
        r = subprocess.run([self.binary, "--model", str(self.voice_path), "--output_file", str(wav_path)],
                           input=text, capture_output=True, text=True, timeout=self.timeout_s)
        if r.returncode != 0 or not wav_path.exists():
            raise NarrationError(f"piper failed ({r.returncode}): {r.stderr[-500:]}")


class SilentTTS(TTS):
    """Deterministic stand-in: silence whose length matches a typical speaking rate."""

    name = "silent"

    def synthesize(self, text: str, wav_path: Path) -> None:
        seconds = max(1.0, len(text.split()) / WORDS_PER_SECOND)
        write_silence(wav_path, seconds)


def _require_voice(voice_path: Path) -> None:
    if not Path(voice_path).exists():
        raise NarrationError(f"Piper voice not found at {voice_path}. "
                             "Run: python scripts/download_voice.py")


def get_tts(settings=None) -> TTS:
    if settings is None:
        from core.config import get_settings
        settings = get_settings()
    backend = settings.tts_backend
    if backend == "silent":
        return SilentTTS()
    if backend == "piper-python" or (backend == "auto" and importlib.util.find_spec("piper")
                                     and not settings.piper_bin):
        return PiperPythonTTS(settings.piper_voice)
    if backend in ("piper-cli", "auto") and settings.piper_bin:
        return PiperCLITTS(settings.piper_bin, settings.piper_voice)
    raise NarrationError("No TTS engine available: `pip install piper-tts`, or set PIPER_BIN, "
                         "or TTS_BACKEND=silent for a dry run.")


# ------------------------------------------------------------------ wav utils
def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as wf:
        return wf.getnframes() / float(wf.getframerate())


def write_silence(path: Path, seconds: float, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(b"\x00\x00" * int(round(seconds * rate)))


def append_silence(path: Path, seconds: float) -> None:
    """Pad a WAV in place so the next sentence doesn't start mid-breath."""
    if seconds <= 0:
        return
    with wave.open(str(path), "rb") as wf:
        params = wf.getparams()
        frames = wf.readframes(wf.getnframes())
    pad = b"\x00" * (int(round(seconds * params.framerate)) * params.sampwidth * params.nchannels)
    tmp = path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as wf:
        wf.setparams(params)
        wf.writeframes(frames + pad)
    os.replace(tmp, path)


def concat_wavs(paths: list[Path], out_path: Path) -> float:
    """Concatenate WAVs that share a format. Returns total duration."""
    if not paths:
        raise NarrationError("nothing to concatenate")
    params, chunks = None, []
    for p in paths:
        with wave.open(str(p), "rb") as wf:
            pp = wf.getparams()
            if params is None:
                params = pp
            elif (pp.nchannels, pp.sampwidth, pp.framerate) != (params.nchannels, params.sampwidth,
                                                                 params.framerate):
                raise NarrationError(f"wav format mismatch in {p}")
            chunks.append(wf.readframes(wf.getnframes()))
    tmp = out_path.with_suffix(".tmp.wav")
    with wave.open(str(tmp), "wb") as wf:
        wf.setparams(params)
        for c in chunks:
            wf.writeframes(c)
    os.replace(tmp, out_path)
    return wav_duration(out_path)


# ------------------------------------------------------------------ stage
def segment_wav_path(audio_dir: Path, i: int) -> Path:
    return audio_dir / f"seg_{i:02d}.wav"


def narrate_script(tts: TTS, script: LessonScript, audio_dir: Path, pad_s: float = 0.35) -> list[float]:
    """Write seg_XX.wav per segment (+ trailing pause) and return their durations.
    Idempotent: segments whose WAV already exists are not re-synthesized."""
    audio_dir.mkdir(parents=True, exist_ok=True)
    durations = []
    for i, seg in enumerate(script.segments):
        path = segment_wav_path(audio_dir, i)
        if not (path.exists() and path.stat().st_size > 44):
            tmp = path.with_suffix(".part.wav")
            tts.synthesize(seg.narration, tmp)
            append_silence(tmp, pad_s)
            os.replace(tmp, path)
        durations.append(round(wav_duration(path), 3))
    return durations
