"""Self-repair loop. A fake LLM returns broken-then-fixed code; a fake runner
behaves like the sandbox. One manim-marked test does the same with real renders."""
import json
import shutil
from pathlib import Path

import pytest

from core.db import Database
from core.llm import ScriptedLLM
from core.schemas import LessonScript
from pipeline import errors
from pipeline.errors import classify_render_failure, trim_traceback
from pipeline.repair import number_lines, render_with_repair
from sandbox.run import RenderResult
from tests.fakes import script

DURS = [3.0, 3.0, 3.0]


def lesson() -> LessonScript:
    return LessonScript.model_validate(script(3, 50))


def scene(body_per_segment: str = "self.play(Create(Circle()), run_time=1)") -> str:
    segs = "\n".join(f"        {body_per_segment}\n        self.sync_to({i})" for i in range(3))
    return f'''from manim import *


class LessonScene(Scene):
    def sync_to(self, i):
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
{segs}
'''


FIXED = scene()
HALLUCINATED = scene("self.play(ShowCreation(Circle()), run_time=1)")
LATEX_BAD = scene(r'self.play(Write(MathTex(r"\frac{1}{")), run_time=1)')
SLOW = scene("self.play(Create(Circle()), run_time=1)\n        while True: pass")
EVIL = scene("eval('1')")
SYNTAX = FIXED.replace("def construct(self):", "def construct(self)")

NAME_ERROR_TB = '''Traceback (most recent call last):
  File "/opt/stv/render_entry.py", line 70, in main
    scene.render()
  File "/usr/local/lib/python3.12/site-packages/manim/scene/scene.py", line 237, in render
    self.construct()
  File "/work/scene.py", line 18, in construct
    self.play(ShowCreation(Circle()), run_time=1)
              ^^^^^^^^^^^^
NameError: name 'ShowCreation' is not defined'''


class FakeRunner:
    """Mimics the sandbox: decides the outcome from the code it is given."""

    def __init__(self):
        self.calls = []

    def render(self, scene_file, out_dir, quality="l", timeout_s=180, scene_name="LessonScene"):
        code = Path(scene_file).read_text()
        self.calls.append(code)
        out_dir.mkdir(parents=True, exist_ok=True)
        if "ShowCreation" in code:
            return RenderResult(False, None, NAME_ERROR_TB, 1.2, "error", "NameError",
                                "name 'ShowCreation' is not defined", 1)
        if r"\frac{1}{" in code:
            return RenderResult(False, None, "ERROR LaTeX compilation error: ! Missing } inserted.\n"
                                "ValueError: latex error converting to dvi.", 2.0, "error", "ValueError",
                                "latex error converting to dvi. See log output above", 1)
        if "while True" in code:
            return RenderResult(False, None, "", 10.0, "timeout", "Timeout", None, -9)
        video = out_dir / "render.mp4"
        video.write_bytes(b"fake")
        return RenderResult(True, video, "", 2.5, "ok", None, None, 0)


def run(llm, runner=None, *, probe=lambda p: 9.0, max_repairs=3, db_log=None, statuses=None, tmp_path):
    return render_with_repair(
        llm, runner or FakeRunner(), script=lesson(), durations=DURS, work_dir=tmp_path / "work",
        max_repairs=max_repairs, probe=probe, on_attempt=db_log,
        on_status=(statuses.append if statuses is not None else None))


def code_json(code):
    return json.dumps({"code": code})


# ------------------------------------------------------------------ loop behaviour
def test_broken_then_fixed_recovers_and_logs_attempts(tmp_path):
    db = Database(tmp_path / "db.sqlite").init()
    sid = db.create_syllabus("x" * 50)
    [tid] = db.insert_topics(sid, [{"title": "T", "slug": "t", "objectives": ["a", "b", "c"]}])

    def log(a):
        db.log_render_attempt(tid, a.attempt_no, a.success, a.render_seconds, error_class=a.error_class,
                              exit_reason=a.exit_reason, error_message=a.error_message,
                              video_duration_s=a.video_duration_s)

    llm = ScriptedLLM(responses=[code_json(HALLUCINATED), code_json(FIXED)])
    statuses = []
    res = run(llm, db_log=log, statuses=statuses, tmp_path=tmp_path)

    assert res.success and res.video_path.exists()
    assert res.repairs_used == 1 and res.duration_ok
    assert [c["task"] for c in llm.calls] == ["codegen", "repair"]
    assert statuses == ["coded", "rendering", "repairing", "repairing"]
    rows = db.list_render_attempts(tid)
    assert [(r["attempt_no"], r["success"], r["error_class"]) for r in rows] == [
        (1, 0, "hallucinated_api"), (2, 1, None)]
    assert rows[1]["video_duration_s"] == 9.0

    # the repair prompt carries the error class, its hint, the trimmed traceback and numbered code
    repair_user = llm.calls[1]["messages"][0]["content"]
    assert "Error class: hallucinated_api" in repair_user
    assert "Manim Community v0.19" in repair_user  # hint text
    assert "NameError: name 'ShowCreation' is not defined" in repair_user
    assert "ShowCreation(Circle())" in repair_user and " | from manim import *" in repair_user
    assert "Repair mode" in llm.calls[1]["system"]


def test_gives_up_after_k_repairs(tmp_path):
    k = 3
    llm = ScriptedLLM(responses=[code_json(HALLUCINATED)] * (k + 1))
    runner = FakeRunner()
    res = run(llm, runner, max_repairs=k, tmp_path=tmp_path)
    assert not res.success and res.video_path is None
    assert len(res.attempts) == k + 1 and len(runner.calls) == k + 1
    assert len(llm.calls) == 1 + k  # one generation + K repairs, never more
    assert res.failure_class == "hallucinated_api"
    assert all(not a.success for a in res.attempts)


def test_validation_failure_skips_render(tmp_path):
    llm = ScriptedLLM(responses=[code_json(EVIL), code_json(FIXED)])
    runner = FakeRunner()
    res = run(llm, runner, tmp_path=tmp_path)
    assert res.success
    assert res.attempts[0].error_class == "validation" and res.attempts[0].render_seconds == 0
    assert len(runner.calls) == 1  # only the fixed code reached the sandbox
    assert "banned-name" in llm.calls[1]["messages"][0]["content"]


def test_syntax_error_caught_before_render(tmp_path):
    llm = ScriptedLLM(responses=[code_json(SYNTAX), code_json(FIXED)])
    runner = FakeRunner()
    res = run(llm, runner, tmp_path=tmp_path)
    assert res.attempts[0].error_class == "syntax" and len(runner.calls) == 1 and res.success


def test_latex_and_timeout_classes(tmp_path):
    llm = ScriptedLLM(responses=[code_json(LATEX_BAD), code_json(SLOW), code_json(FIXED)])
    res = run(llm, tmp_path=tmp_path)
    assert [a.error_class for a in res.attempts] == ["latex", "timeout", None]
    assert "LaTeX failed to compile" in llm.calls[1]["messages"][0]["content"]
    assert "time limit" in llm.calls[2]["messages"][0]["content"]


def test_duration_mismatch_triggers_repair_then_accepts_with_warning(tmp_path):
    llm = ScriptedLLM(responses=[code_json(FIXED), code_json(FIXED)])
    res = run(llm, probe=lambda p: 4.0, max_repairs=1, tmp_path=tmp_path)
    assert [a.error_class for a in res.attempts] == ["duration_mismatch", "duration_mismatch"]
    assert [a.success for a in res.attempts] == [False, True]
    assert res.success and res.duration_ok is False and "pad" in res.warning
    assert "narration totals 9.00s" in llm.calls[1]["messages"][0]["content"]


def test_duration_within_tolerance_passes(tmp_path):
    res = run(ScriptedLLM(responses=[code_json(FIXED)]), probe=lambda p: 9.6, tmp_path=tmp_path)
    assert res.success and res.duration_ok and len(res.attempts) == 1


def test_llm_contract_failure_during_repair_stops_cleanly(tmp_path):
    llm = ScriptedLLM(responses=[code_json(HALLUCINATED), "garbage", "more garbage"])
    res = run(llm, tmp_path=tmp_path)
    assert not res.success and res.failure_class == "llm_output"


def test_every_attempt_keeps_its_code_on_disk(tmp_path):
    llm = ScriptedLLM(responses=[code_json(HALLUCINATED), code_json(FIXED)])
    res = run(llm, tmp_path=tmp_path)
    assert "ShowCreation" in res.attempts[0].code_path.read_text()
    assert "SEGMENT_ENDS = [3.0, 6.0, 9.0]" in res.attempts[1].code_path.read_text()


# ------------------------------------------------------------------ classification
@pytest.mark.parametrize("exit_reason,etype,msg,tail,expected", [
    ("timeout", "Timeout", None, "", "timeout"),
    ("oom", None, None, "", "oom"),
    ("error", "SyntaxError", "invalid syntax", "", "syntax"),
    ("error", "IndentationError", "unexpected indent", "", "syntax"),
    ("error", "NameError", "name 'ShowCreation' is not defined", "", "hallucinated_api"),
    ("error", "AttributeError", "'Axes' object has no attribute 'get_graph'", "", "hallucinated_api"),
    ("error", "TypeError", "Mobject.__init__() got an unexpected keyword argument 'colour'", "", "hallucinated_api"),
    ("error", "TypeError", "unsupported operand type(s) for +: 'int' and 'str'", "", "other"),
    ("error", "ValueError", "latex error converting to dvi. See log output above", "", "latex"),
    ("error", "ValueError", "x", "! Undefined control sequence.", "latex"),
    ("error", "ZeroDivisionError", "division by zero", "", "other"),
    ("error", None, None, "Traceback...\nNameError: name 'x' is not defined", "hallucinated_api"),
])
def test_classify_render_failure(exit_reason, etype, msg, tail, expected):
    assert classify_render_failure(exit_reason, etype, msg, tail) == expected


def test_every_class_has_a_hint():
    assert set(errors.HINTS) == set(errors.ALL_CLASSES)


def test_trim_traceback_keeps_scene_frames_and_exception():
    noise = "\n".join(f"INFO line {i}" for i in range(200))
    out = trim_traceback(noise + "\n" + NAME_ERROR_TB)
    assert out.startswith("Traceback")
    assert 'File "/work/scene.py", line 18' in out and "ShowCreation(Circle())" in out
    assert out.rstrip().endswith("NameError: name 'ShowCreation' is not defined")
    assert "INFO line" not in out
    assert "render_entry.py" not in out  # framework frame elided
    assert len(out.splitlines()) <= 40


def test_trim_traceback_without_traceback_keeps_tail_and_tex_errors():
    out = trim_traceback("a\n! Missing $ inserted.\n" + "\n".join(f"l{i}" for i in range(100)))
    assert "! Missing $ inserted." in out and "l99" in out and "l10\n" not in out


def test_number_lines():
    assert number_lines("a\nb") == "   1 | a\n   2 | b"


# ------------------------------------------------------------------ real manim
@pytest.mark.manim
def test_real_render_recovers_from_hallucinated_api(tmp_path):
    from sandbox.run import LocalRunner
    from pipeline.media import probe_duration
    if shutil.which("ffprobe") is None:
        pytest.skip("ffprobe missing")
    llm = ScriptedLLM(responses=[code_json(HALLUCINATED), code_json(FIXED)])
    res = render_with_repair(llm, LocalRunner(), script=lesson(), durations=[1.5, 1.5, 1.5],
                             work_dir=tmp_path / "w", max_repairs=2, timeout_s=120, probe=probe_duration)
    assert res.success, [a.error_message for a in res.attempts]
    assert [a.error_class for a in res.attempts] == ["hallucinated_api", None]
    assert res.video_duration_s == pytest.approx(4.5, abs=0.2)
