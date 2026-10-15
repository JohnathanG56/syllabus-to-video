import json
import shutil
import subprocess
from pathlib import Path

import pytest

from core.llm import ScriptedLLM
from core.schemas import LessonScript
from pipeline.codegen import (
    EXAMPLES_DIR, HEADER_START, check_timing_contract, codegen_prompt, generate_code, inject_timing,
    prepare_scene, segment_ends, strip_code_fences, timing_header,
)
from tests.fakes import script

EXAMPLES = sorted(EXAMPLES_DIR.glob("*.py"))

MODEL_CODE = '''from manim import *
import numpy as np

SEGMENT_DURATIONS = [1, 1, 1, 1]
SEGMENT_ENDS = [1,
                2, 3, 4]


class LessonScene(Scene):
    def sync_to(self, i):
        remaining = SEGMENT_ENDS[i] - self.renderer.time
        if remaining > 0.02:
            self.wait(remaining)

    def construct(self):
        for i in range(4):
            pass
        self.sync_to(0)
        self.sync_to(1)
        self.sync_to(2)
        self.sync_to(3)
'''


def test_segment_ends_and_header():
    assert segment_ends([1.5, 2.25, 3]) == [1.5, 3.75, 6.75]
    h = timing_header([1.5, 2.25])
    assert "SEGMENT_DURATIONS = [1.5, 2.25]" in h and "SEGMENT_ENDS = [1.5, 3.75]" in h


def test_strip_code_fences():
    assert strip_code_fences("```python\nx = 1\n```") == "x = 1\n"
    assert strip_code_fences("x = 1") == "x = 1\n"


def test_inject_timing_replaces_model_constants_after_imports():
    out = inject_timing(MODEL_CODE, [2.0, 3.0, 4.0, 5.0])
    assert out.count("SEGMENT_DURATIONS =") == 1 and out.count("SEGMENT_ENDS =") == 1
    assert "SEGMENT_ENDS = [2.0, 5.0, 9.0, 14.0]" in out
    lines = out.splitlines()
    assert lines.index(HEADER_START) > lines.index("import numpy as np")
    assert "2, 3, 4]" not in out  # multi-line model assignment fully removed


def test_inject_timing_is_idempotent_for_repair_rounds():
    once = inject_timing(MODEL_CODE, [2.0, 3.0, 4.0, 5.0])
    twice = inject_timing(once, [2.5, 3.0, 4.0, 5.0])
    assert twice.count(HEADER_START) == 1
    assert "SEGMENT_DURATIONS = [2.5, 3.0, 4.0, 5.0]" in twice


def test_inject_timing_survives_syntax_errors():
    broken = "from manim import *\nclass LessonScene(Scene)\n    pass\n"
    out = inject_timing(broken, [1.0])
    assert out.splitlines()[0] == "from manim import *"
    assert "SEGMENT_ENDS = [1.0]" in out


def test_timing_contract():
    assert check_timing_contract(MODEL_CODE, 4) == []
    problems = check_timing_contract(MODEL_CODE, 5)
    assert problems and "sync_to" in problems[0].message
    no_helper = MODEL_CODE.replace("def sync_to(self, i):", "def other(self, i):")
    assert any("define the sync_to" in p.message for p in check_timing_contract(no_helper, 4))


def test_prepare_scene_flags_contract_and_security_together():
    code, res = prepare_scene(MODEL_CODE.replace("self.sync_to(3)", "eval('1')"), [1, 1, 1, 1])
    assert not res.ok
    assert {v.rule for v in res.violations} == {"banned-name", "timing"}
    assert res.error_class == "validation"


@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_prompt_examples_pass_validation(path):
    src = path.read_text()
    n = src.count("self.sync_to(")
    code, res = prepare_scene(src, [5.0] * n)
    assert res.ok, res.format()


def test_codegen_prompt_embeds_examples_durations_and_script():
    s = LessonScript.model_validate(script(4, 45))
    system, user = codegen_prompt(s, [10.0, 12.5, 9.0, 11.0])
    for p in EXAMPLES:
        assert p.read_text().strip() in system
    assert "SEGMENT_DURATIONS (seconds) = [10.0, 12.5, 9.0, 11.0]" in user
    assert "SEGMENT_ENDS (seconds) = [10.0, 22.5, 31.5, 42.5]" in user
    assert "Segment 3 (11.00s)" in user and "{{" not in system + user


def test_generate_code_uses_schema():
    s = LessonScript.model_validate(script(4, 45))
    llm = ScriptedLLM(responses=[json.dumps({"code": MODEL_CODE})])
    assert generate_code(llm, s, [1, 2, 3, 4]) == MODEL_CODE.strip()
    assert llm.calls[0]["task"] == "codegen"
    with pytest.raises(ValueError):
        generate_code(llm, s, [1, 2])


@pytest.mark.manim
@pytest.mark.parametrize("path", EXAMPLES, ids=lambda p: p.name)
def test_prompt_examples_render_with_exact_duration(path, tmp_path):
    """The few-shot examples shown to the model must actually render."""
    if not (shutil.which("latex") and shutil.which("dvisvgm")):
        pytest.skip("MathTex needs latex + dvisvgm on PATH")
    from sandbox.run import LocalRunner
    scene = tmp_path / "scene.py"
    scene.write_text(path.read_text())
    res = LocalRunner().render(scene, tmp_path / "out", "l", timeout_s=300)
    assert res.success, res.stderr_tail
    dur = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
                                "csv=p=0", str(res.video_path)], capture_output=True, text=True).stdout)
    ns: dict = {}
    exec(path.read_text().split("class LessonScene")[0].replace("from manim import *", ""), ns)
    assert dur == pytest.approx(ns["SEGMENT_ENDS"][-1], abs=0.1)
