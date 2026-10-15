from types import SimpleNamespace

import pytest

from core.llm import (
    AnthropicLLM, LLMConfigError, LLMOutputError, ScriptedLLM, complete_json, extract_json, get_llm,
)
from core.schemas import LessonScript, SceneCode, TopicList
from tests.fakes import TOPICS, dumps, script


# ------------------------------------------------------------- extract_json
@pytest.mark.parametrize("text", [
    '{"a": 1}',
    '```json\n{"a": 1}\n```',
    'Sure! Here you go:\n{"a": 1}\nHope that helps.',
    '```\n{"a": 1}\n```',
])
def test_extract_json_tolerates_wrappers(text):
    assert extract_json(text) == {"a": 1}


def test_extract_json_handles_braces_inside_strings():
    assert extract_json('{"code": "def f():\\n    return {1: 2}"}')["code"].endswith("{1: 2}")


@pytest.mark.parametrize("text", ["no json here", "[1, 2, 3]", "{broken"])
def test_extract_json_rejects_non_objects(text):
    with pytest.raises(ValueError):
        extract_json(text)


# ------------------------------------------------------------- complete_json
def test_complete_json_valid_first_try():
    llm = ScriptedLLM(responses=[dumps(TOPICS)])
    out = complete_json(llm, system="sys", user="u", schema=TopicList, task="topics")
    assert len(out.topics) == 3
    assert len(llm.calls) == 1
    # the JSON schema is appended to the system prompt
    assert '"TopicSpec"' in llm.calls[0]["system"] or "objectives" in llm.calls[0]["system"]


def test_complete_json_retries_once_with_error_feedback():
    bad = dict(TOPICS, topics=[dict(TOPICS["topics"][0], objectives=["only one objective"])])
    llm = ScriptedLLM(responses=[dumps(bad), dumps(TOPICS)])
    usage = []
    out = complete_json(llm, system="s", user="u", schema=TopicList, task="topics",
                        on_usage=lambda t, r, ok: usage.append(ok))
    assert len(out.topics) == 3
    assert len(llm.calls) == 2
    retry_msgs = llm.calls[1]["messages"]
    assert [m["role"] for m in retry_msgs] == ["user", "assistant", "user"]
    assert "failed validation" in retry_msgs[-1]["content"]
    assert "objectives" in retry_msgs[-1]["content"]
    assert usage == [False, True]


def test_complete_json_gives_up_after_one_retry():
    llm = ScriptedLLM(responses=["not json", '{"also": "wrong"}', dumps(TOPICS)])
    with pytest.raises(LLMOutputError) as ei:
        complete_json(llm, system="s", user="u", schema=TopicList, task="topics")
    assert len(llm.calls) == 2
    assert len(ei.value.raw_outputs) == 2


def test_extra_fields_are_rejected():
    payload = dict(TOPICS, surprise="field")
    llm = ScriptedLLM(responses=[dumps(payload), dumps(payload)])
    with pytest.raises(LLMOutputError):
        complete_json(llm, system="s", user="u", schema=TopicList, task="topics")


def test_lesson_script_length_bounds():
    LessonScript.model_validate(script(4, 45))  # 180 words: fine
    with pytest.raises(Exception):
        LessonScript.model_validate(script(3, 20))  # 60 words: too short
    with pytest.raises(Exception):
        LessonScript.model_validate(script(6, 70))  # 420 words: too long
    with pytest.raises(Exception):
        LessonScript.model_validate(script(2, 90))  # too few segments


def test_scene_code_schema():
    SceneCode.model_validate({"code": "from manim import *\nclass LessonScene(Scene):\n    pass\n"})
    with pytest.raises(Exception):
        SceneCode.model_validate({"code": "x"})


# ------------------------------------------------------------- Anthropic adapter
class FakeMessages:
    def __init__(self, text, stop_reason="end_turn"):
        self.text, self.stop_reason, self.kwargs = text, stop_reason, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=self.text)],
            usage=SimpleNamespace(input_tokens=123, output_tokens=45),
            model=kwargs["model"], stop_reason=self.stop_reason)


def test_anthropic_adapter_maps_request_and_response():
    msgs = FakeMessages('{"a": 1}')
    llm = AnthropicLLM(api_key="unused", model="claude-test", temperature=0.2,
                       client=SimpleNamespace(messages=msgs))
    resp = llm.complete("SYS", [{"role": "user", "content": "hi"}], 1000)
    assert msgs.kwargs == {"model": "claude-test", "max_tokens": 1000, "system": "SYS",
                           "messages": [{"role": "user", "content": "hi"}], "temperature": 0.2}
    assert resp.text == '{"a": 1}' and resp.input_tokens == 123 and resp.output_tokens == 45
    assert not resp.truncated


def test_anthropic_adapter_flags_truncation_and_omits_temperature():
    msgs = FakeMessages("{", stop_reason="max_tokens")
    llm = AnthropicLLM(api_key="unused", model="m", client=SimpleNamespace(messages=msgs))
    assert llm.complete("s", [], 10).truncated
    assert "temperature" not in msgs.kwargs


def test_get_llm_requires_model_and_key(env, monkeypatch):
    from core.config import get_settings
    monkeypatch.delenv("LLM_MODEL", raising=False)
    with pytest.raises(LLMConfigError, match="LLM_MODEL"):
        get_llm(get_settings())
    monkeypatch.setenv("LLM_MODEL", "some-model")
    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        get_llm(get_settings())
