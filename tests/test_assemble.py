import subprocess
from pathlib import Path

import pytest

from core.schemas import LessonScript
from pipeline.assemble import (
    assemble_lesson, build_cues, chunk_text, ffmpeg_has_filter, mux, output_path, srt_timestamp, write_srt,
)
from pipeline.media import probe_duration, probe_streams
from pipeline.narration import write_silence
from tests.fakes import script


def make_video(path: Path, seconds: float) -> Path:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc=size=320x180:rate=15:duration={seconds}", "-pix_fmt", "yuv420p", str(path)],
                   check=True)
    return path


# ------------------------------------------------------------------ captions (pure)
def test_srt_timestamp():
    assert srt_timestamp(0) == "00:00:00,000"
    assert srt_timestamp(3661.5) == "01:01:01,500"
    assert srt_timestamp(59.9996) == "00:01:00,000"


def test_chunk_text_respects_max_chars_and_keeps_words():
    text = "A vector is an arrow. " * 10
    chunks = chunk_text(text, max_chars=40)
    assert all(len(c) <= 40 for c in chunks)
    assert " ".join(chunks).split() == text.split()


def test_cues_are_contiguous_and_follow_segment_timing():
    s = LessonScript.model_validate(script(4, 45))
    durs = [10.0, 12.0, 8.0, 11.0]
    cues = build_cues(s, durs, pad_s=0.5)
    assert cues[0][0] == 0.0
    for (a1, b1, _), (a2, b2, _) in zip(cues, cues[1:]):
        assert b1 <= a2 + 1e-6 and a2 < b2
    seg2_start = sum(durs[:2])
    assert any(abs(a - seg2_start) < 1e-3 for a, _, _ in cues)
    assert cues[-1][1] <= sum(durs) - 0.5 + 1e-3


def test_write_srt_format(tmp_path):
    p = write_srt([(0, 1.5, "hello world"), (1.5, 3, "a much longer caption line that needs wrapping ok")],
                  tmp_path / "x.srt")
    text = p.read_text()
    assert text.startswith("1\n00:00:00,000 --> 00:00:01,500\nhello world\n")
    assert "2\n00:00:01,500 --> 00:00:03,000\n" in text
    assert text.count("\n") >= 8  # second cue wrapped onto two lines


def test_output_path_naming(tmp_path):
    assert output_path(tmp_path, 7, 2, "vectors-as-arrows") == tmp_path / "7" / "2-vectors-as-arrows.mp4"


# ------------------------------------------------------------------ ffmpeg
@pytest.mark.ffmpeg
def test_mux_pads_video_with_last_frame_when_audio_is_longer(tmp_path):
    video = make_video(tmp_path / "v.mp4", 2)
    audio = tmp_path / "a.wav"
    write_silence(audio, 3.5)
    info = mux(video, audio, tmp_path / "out.mp4")
    assert info["output_s"] == pytest.approx(3.5, abs=0.15)
    kinds = sorted(s["codec_type"] for s in probe_streams(tmp_path / "out.mp4"))
    assert kinds == ["audio", "video"]


@pytest.mark.ffmpeg
def test_mux_pads_audio_when_video_is_longer(tmp_path):
    video = make_video(tmp_path / "v.mp4", 3)
    audio = tmp_path / "a.wav"
    write_silence(audio, 1.0)
    assert mux(video, audio, tmp_path / "out.mp4")["output_s"] == pytest.approx(3.0, abs=0.15)


@pytest.mark.ffmpeg
def test_assemble_lesson_end_to_end(tmp_path):
    s = LessonScript.model_validate(script(3, 50))
    wavs = []
    for i in range(3):
        wavs.append(tmp_path / f"seg_{i}.wav")
        write_silence(wavs[-1], 1.2)
    video = make_video(tmp_path / "render.mp4", 3)
    out = output_path(tmp_path / "out", 1, 1, "vectors")
    info = assemble_lesson(video=video, segment_wavs=wavs, script=s, durations=[1.2] * 3, out_path=out,
                           work_dir=tmp_path / "work", pad_s=0.2)
    assert out.exists() and out.with_suffix(".srt").exists()
    assert info["output_s"] == pytest.approx(3.6, abs=0.15)
    kinds = sorted(st["codec_type"] for st in probe_streams(out))
    assert kinds == ["audio", "subtitle", "video"]
    assert not list(out.parent.glob("*.part.mp4"))


@pytest.mark.ffmpeg
def test_burned_captions(tmp_path):
    if not ffmpeg_has_filter("subtitles"):
        pytest.skip("ffmpeg built without libass")
    video = make_video(tmp_path / "v.mp4", 2)
    audio = tmp_path / "a.wav"
    write_silence(audio, 2)
    srt = write_srt([(0, 2, "burned caption")], tmp_path / "c.srt")
    info = mux(video, audio, tmp_path / "out.mp4", srt=srt, burn=True)
    assert info["output_s"] == pytest.approx(2.0, abs=0.15)
    assert probe_duration(tmp_path / "out.mp4") > 1.8
