import json

import pytest

from core.llm import LLMOutputError, ScriptedLLM
from core.prompts import build, load_prompt, render
from pipeline.script import write_script
from tests.fakes import script


def test_write_script_renders_topic_into_prompt():
    llm = ScriptedLLM(responses=[json.dumps(script())])
    out = write_script(llm, title="Vectors as arrows", objectives=["Draw a vector", "Add vectors", "Scale"],
                       prerequisites=[], course_title="MATH 33A")
    assert len(out.segments) == 4
    assert out.segments[1].key_equation == "\\vec{v} = (2, 1)"
    assert out.segments[0].key_equation is None
    user = llm.calls[0]["messages"][0]["content"]
    assert "Topic: Vectors as arrows" in user and "- Add vectors" in user and "none" in user
    system = llm.calls[0]["system"]
    assert "No external assets" in system and "One idea per segment" in system


def test_write_script_retries_on_too_short_script():
    short = script(3, 10)
    llm = ScriptedLLM(responses=[json.dumps(short), json.dumps(script())])
    out = write_script(llm, title="T", objectives=["a b c d e", "f g h i j", "k l m n o"])
    assert len(out.segments) == 4
    assert "words" in llm.calls[1]["messages"][-1]["content"]


def test_write_script_fails_after_two_bad_outputs():
    llm = ScriptedLLM(responses=["{}", "{}"])
    with pytest.raises(LLMOutputError):
        write_script(llm, title="T", objectives=["aaaaa", "bbbbb", "ccccc"])


def test_estimated_seconds():
    from core.schemas import LessonScript
    s = LessonScript.model_validate(script(4, 45))
    from core.schemas import WORDS_PER_SECOND
    assert s.estimated_seconds() == pytest.approx(180 / WORDS_PER_SECOND)


def test_all_prompts_have_system_and_user_parts():
    for name in ("topics", "lesson_script"):
        system, user = load_prompt(name)
        assert system and user


def test_render_requires_all_variables():
    assert render("a {{x}} b {{ y }}", x=1, y=2) == "a 1 b 2"
    with pytest.raises(KeyError):
        render("{{missing}}")
    # braces that are not placeholders (python code) pass through untouched
    assert render("d = {1: 2}  {{x}}", x="ok") == "d = {1: 2}  ok"


def test_build_topics_prompt():
    system, user = build("topics", syllabus="SYL", max_topics=5)
    assert "at most 5 topics" in system and "SYL" in user
