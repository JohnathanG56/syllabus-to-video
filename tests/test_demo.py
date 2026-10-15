import json
import shutil

import pytest

from core.schemas import LessonScript, TopicList
from demo.content import BROKEN_FIRST_TRY, CODE, SCRIPTS, TOPICS
from demo.llm import demo_llm
from pipeline.codegen import prepare_scene


def test_demo_payloads_satisfy_the_real_schemas():
    TopicList.model_validate(TOPICS)
    for title, s in SCRIPTS.items():
        script = LessonScript.model_validate(s)
        assert 60 <= script.estimated_seconds() <= 120, title


@pytest.mark.parametrize("title", list(CODE))
def test_demo_code_passes_validator_and_timing_contract(title):
    n = len(SCRIPTS[title]["segments"])
    _, res = prepare_scene(CODE[title], [10.0] * n)
    assert res.ok, res.format()


def test_broken_first_try_is_a_real_hallucination():
    for title, code in BROKEN_FIRST_TRY.items():
        assert "ShowCreation" in code and "ShowCreation" not in CODE[title]
        _, res = prepare_scene(code, [10.0] * len(SCRIPTS[title]["segments"]))
        assert res.ok  # passes static checks; only the render reveals it


def test_demo_llm_routes_tasks():
    llm = demo_llm()
    t = json.loads(llm.complete("", [{"role": "user", "content": "x"}], 10, task="topics").text)
    assert t == TOPICS
    first = llm.complete("", [{"role": "user", "content": "Lesson: Vectors as arrows\n"}], 10, task="codegen")
    assert "ShowCreation" in first.text
    again = llm.complete("", [{"role": "user", "content": "Lesson: Vectors as arrows\n"}], 10, task="codegen")
    assert "ShowCreation" not in again.text
    fixed = llm.complete("", [{"role": "user", "content": json.loads(first.text)["code"]}], 10, task="repair")
    assert json.loads(fixed.text)["code"] == CODE["Vectors as arrows"]


@pytest.mark.manim
@pytest.mark.ffmpeg
def test_demo_runs_end_to_end(tmp_path, monkeypatch):
    """The real thing: Manim renders (LocalRunner), repair loop, ffmpeg assembly."""
    if not (shutil.which("latex") and shutil.which("dvisvgm")):
        pytest.skip("demo scenes use MathTex: needs latex + dvisvgm")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("OUT_DIR", str(tmp_path / "out"))
    monkeypatch.setenv("FINAL_QUALITY", "l")  # keep the test fast
    from demo.__main__ import main
    assert main(["--backend", "local", "--silent"]) == 0
    vids = sorted((tmp_path / "out").rglob("*.mp4"))
    assert [v.name for v in vids] == ["1-vectors-as-arrows.mp4", "2-the-pythagorean-theorem.mp4"]
