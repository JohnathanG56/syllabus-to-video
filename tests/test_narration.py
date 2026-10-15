import array
import wave
from pathlib import Path

import pytest

from core.schemas import LessonScript
from pipeline.narration import (
    NarrationError, PiperCLITTS, SilentTTS, TTS, append_silence, concat_wavs, get_tts, narrate_script,
    segment_wav_path, wav_duration, write_silence,
)
from tests.conftest import VOICE, piper_engine
from tests.fakes import script


class CountingTTS(TTS):
    def __init__(self):
        self.calls = 0

    def synthesize(self, text, wav_path):
        self.calls += 1
        write_silence(wav_path, 2.0)


def test_write_and_measure_silence(tmp_path):
    p = tmp_path / "a.wav"
    write_silence(p, 1.5)
    assert wav_duration(p) == pytest.approx(1.5, abs=1e-3)


def test_append_silence(tmp_path):
    p = tmp_path / "a.wav"
    write_silence(p, 1.0)
    append_silence(p, 0.25)
    assert wav_duration(p) == pytest.approx(1.25, abs=1e-3)


def test_concat_wavs(tmp_path):
    paths = []
    for i, d in enumerate([1.0, 2.0, 0.5]):
        paths.append(tmp_path / f"{i}.wav")
        write_silence(paths[-1], d)
    assert concat_wavs(paths, tmp_path / "all.wav") == pytest.approx(3.5, abs=1e-3)


def test_concat_rejects_mixed_formats(tmp_path):
    write_silence(tmp_path / "a.wav", 1.0, rate=22050)
    write_silence(tmp_path / "b.wav", 1.0, rate=16000)
    with pytest.raises(NarrationError):
        concat_wavs([tmp_path / "a.wav", tmp_path / "b.wav"], tmp_path / "c.wav")


def test_narrate_script_durations_include_pad_and_resume(tmp_path):
    s = LessonScript.model_validate(script(4, 45))
    tts = CountingTTS()
    d = narrate_script(tts, s, tmp_path / "audio", pad_s=0.5)
    assert d == [2.5, 2.5, 2.5, 2.5]
    assert tts.calls == 4
    assert segment_wav_path(tmp_path / "audio", 3).exists()
    # resumable: existing segment files are reused
    segment_wav_path(tmp_path / "audio", 2).unlink()
    d2 = narrate_script(tts, s, tmp_path / "audio", pad_s=0.5)
    assert d2 == d and tts.calls == 5


def test_silent_tts_tracks_word_count(tmp_path):
    SilentTTS().synthesize(" ".join(["word"] * 25), tmp_path / "s.wav")
    assert wav_duration(tmp_path / "s.wav") == pytest.approx(10.0, abs=0.01)


def test_get_tts_selection(env, monkeypatch, tmp_path):
    from core.config import get_settings
    monkeypatch.setenv("TTS_BACKEND", "silent")
    assert isinstance(get_tts(get_settings()), SilentTTS)
    monkeypatch.setenv("TTS_BACKEND", "piper-cli")
    monkeypatch.setenv("PIPER_VOICE", str(tmp_path / "missing.onnx"))
    monkeypatch.setenv("PIPER_BIN", "/bin/true")
    with pytest.raises(NarrationError, match="download_voice"):
        get_tts(get_settings())


@pytest.mark.piper
def test_real_piper_produces_speech(tmp_path):
    backend, binary = piper_engine()
    if backend == "piper-cli":
        tts = PiperCLITTS(binary, VOICE)
    else:
        from pipeline.narration import PiperPythonTTS
        tts = PiperPythonTTS(VOICE)
    out = tmp_path / "hello.wav"
    tts.synthesize("A vector is an arrow that starts at the origin.", out)
    dur = wav_duration(out)
    assert 1.5 < dur < 8.0
    with wave.open(str(out)) as wf:
        samples = array.array("h", wf.readframes(wf.getnframes()))
    peak = max(abs(x) for x in samples)
    assert peak > 1000, "expected audible speech, got near-silence"
