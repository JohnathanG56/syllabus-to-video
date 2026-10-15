"""Stage 6: assembly. Concatenate narration, build captions, mux with ffmpeg.

If the narration is longer than the video, the last frame is held
(``tpad=stop_mode=clone``); if the video is longer, the audio is padded with
silence (``apad``). Captions are always written as a sidecar .srt and embedded
as a soft subtitle track; ``burn=True`` also burns them into the pixels
(needs ffmpeg built with libass).
"""
from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path

from core.schemas import LessonScript
from pipeline.media import probe_duration
from pipeline.narration import concat_wavs, wav_duration


class AssemblyError(RuntimeError):
    pass


# ------------------------------------------------------------------ captions
def srt_timestamp(t: float) -> str:
    ms = int(round(max(t, 0.0) * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def chunk_text(text: str, max_chars: int = 84) -> list[str]:
    """Split narration into caption-sized chunks, preferring sentence breaks."""
    sentences = re.split(r"(?<=[.!?;:])\s+", text.strip())
    chunks: list[str] = []
    for sent in sentences:
        words, cur = sent.split(), ""
        for w in words:
            if cur and len(cur) + 1 + len(w) > max_chars:
                chunks.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            chunks.append(cur)
    return chunks


def build_cues(script: LessonScript, durations: list[float], pad_s: float = 0.0,
               max_chars: int = 84) -> list[tuple[float, float, str]]:
    """Caption cues timed to the measured narration: each segment's chunks share
    its spoken time (duration minus trailing pause) in proportion to length."""
    cues, start = [], 0.0
    for seg, dur in zip(script.segments, durations):
        speech = max(dur - pad_s, 0.5)
        chunks = chunk_text(seg.narration, max_chars)
        total = sum(len(c) for c in chunks) or 1
        t = start
        for c in chunks:
            d = speech * len(c) / total
            cues.append((round(t, 3), round(t + d, 3), c))
            t += d
        start += dur
    return cues


def write_srt(cues: list[tuple[float, float, str]], path: Path) -> Path:
    blocks = []
    for i, (a, b, text) in enumerate(cues, start=1):
        if len(text) > 42 and " " in text:  # wrap to two lines near the middle
            mid = len(text) // 2
            cut = min((k for k in range(len(text)) if text[k] == " "), key=lambda k: abs(k - mid))
            text = text[:cut] + "\n" + text[cut + 1:]
        blocks.append(f"{i}\n{srt_timestamp(a)} --> {srt_timestamp(b)}\n{text}\n")
    path.write_text("\n".join(blocks))
    return path


# ------------------------------------------------------------------ ffmpeg
@lru_cache(maxsize=1)
def ffmpeg_has_filter(name: str) -> bool:
    try:
        r = subprocess.run(["ffmpeg", "-hide_banner", "-filters"], capture_output=True, text=True, timeout=30)
    except OSError:
        return False
    return any(line.split()[1:2] == [name] for line in r.stdout.splitlines() if len(line.split()) > 1)


def _escape_filter_path(p: Path) -> str:
    return str(p).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'").replace(",", "\\,")


def mux(video: Path, audio: Path, out_path: Path, srt: Path | None = None, burn: bool = False,
        timeout_s: float = 600) -> dict:
    vdur, adur = probe_duration(video), wav_duration(audio)
    target = max(vdur, adur)
    vf = []
    if adur > vdur + 0.02:
        vf.append(f"tpad=stop_mode=clone:stop_duration={adur - vdur + 0.1:.3f}")
    if burn and srt is not None:
        if not ffmpeg_has_filter("subtitles"):
            raise AssemblyError("BURN_CAPTIONS=1 but this ffmpeg has no libass 'subtitles' filter")
        vf.append(f"subtitles='{_escape_filter_path(srt)}':force_style='FontSize=20,MarginV=24'")
    cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(video), "-i", str(audio)]
    if srt is not None:
        cmd += ["-i", str(srt)]
    if vf:
        cmd += ["-vf", ",".join(vf)]
    cmd += ["-af", f"apad=whole_dur={target:.3f}"]
    cmd += ["-map", "0:v:0", "-map", "1:a:0"]
    if srt is not None:
        cmd += ["-map", "2:s:0", "-c:s", "mov_text", "-metadata:s:s:0", "language=eng"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", "-t", f"{target:.3f}", str(out_path)]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
    if r.returncode != 0:
        raise AssemblyError(f"ffmpeg failed: {r.stderr.strip()[-800:]}")
    return {"video_s": round(vdur, 3), "audio_s": round(adur, 3), "output_s": round(probe_duration(out_path), 3)}


def output_path(out_dir: Path, syllabus_id: int, position: int, slug: str) -> Path:
    return Path(out_dir) / str(syllabus_id) / f"{position}-{slug}.mp4"


def assemble_lesson(*, video: Path, segment_wavs: list[Path], script: LessonScript, durations: list[float],
                    out_path: Path, work_dir: Path, pad_s: float = 0.35, burn_captions: bool = False) -> dict:
    """Concatenate narration, write captions, mux. Returns timing info."""
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    narration = work_dir / "narration.wav"
    concat_wavs(segment_wavs, narration)
    srt = write_srt(build_cues(script, durations, pad_s=pad_s), out_path.with_suffix(".srt"))
    tmp = out_path.with_name(out_path.stem + ".part.mp4")
    info = mux(video, narration, tmp, srt=srt, burn=burn_captions)
    tmp.replace(out_path)
    info["path"] = str(out_path)
    info["srt"] = str(srt)
    return info
