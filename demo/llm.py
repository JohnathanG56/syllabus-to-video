"""FakeLLM for demo mode: answers every pipeline task with the hand-written
content in demo/content.py, so the full pipeline runs without an API key."""
from __future__ import annotations

import json
import re

from core.llm import ScriptedLLM
from demo.content import BROKEN_FIRST_TRY, CODE, SCRIPTS, TOPICS


def _topic_from_codegen_prompt(text: str) -> str:
    m = re.search(r"^Lesson: (.*)$", text, re.MULTILINE)
    if not m:
        raise AssertionError("demo LLM: could not find the lesson title in the codegen prompt")
    return m.group(1).strip()


def demo_llm(show_repair: bool = True) -> ScriptedLLM:
    first_codegen_done: set[str] = set()

    def respond(system: str, messages: list[dict], task: str) -> str:
        user = messages[0]["content"]
        if task == "topics":
            return json.dumps(TOPICS)
        if task == "script":
            title = re.search(r"^Topic: (.*)$", user, re.MULTILINE).group(1).strip()
            return json.dumps(SCRIPTS[title])
        if task == "codegen":
            title = _topic_from_codegen_prompt(user)
            if show_repair and title in BROKEN_FIRST_TRY and title not in first_codegen_done:
                first_codegen_done.add(title)
                return json.dumps({"code": BROKEN_FIRST_TRY[title]})
            return json.dumps({"code": CODE[title]})
        if task == "repair":
            # The repair prompt carries the failing code; each demo scene shows its
            # own title as a Text(...) object, which identifies the lesson.
            for title, code in CODE.items():
                if f'Text("{title}"' in user:
                    return json.dumps({"code": code})
            raise AssertionError("demo LLM: unknown repair request")
        raise AssertionError(f"demo LLM: unexpected task {task!r}")

    return ScriptedLLM(responder=respond, model="demo-fake-llm (hand-written outputs)")
