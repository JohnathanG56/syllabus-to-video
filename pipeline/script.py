"""Stage 2: topic -> lesson script (3-6 segments of narration + visual plan)."""
from __future__ import annotations

from core import prompts
from core.llm import LLM, UsageHook, complete_json
from core.schemas import LessonScript


def write_script(llm: LLM, *, title: str, objectives: list[str], prerequisites: list[str] | None = None,
                 course_title: str | None = None, on_usage: UsageHook | None = None) -> LessonScript:
    system, user = prompts.build(
        "lesson_script",
        course_title=course_title or "(unknown course)",
        title=title,
        objectives="\n".join(f"- {o}" for o in objectives),
        prerequisites=", ".join(prerequisites or []) or "none",
    )
    return complete_json(llm, system=system, user=user, schema=LessonScript, task="script",
                         max_tokens=4000, on_usage=on_usage)
