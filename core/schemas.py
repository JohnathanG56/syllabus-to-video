"""Strict JSON contracts for every LLM output (validated with pydantic).

``extra="forbid"`` means a model that invents fields fails validation and gets
one retry with the exact error message.
"""
from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Speaking rate used to sanity-check script length before we pay for TTS.
# Measured: Piper en_US-lessac-medium speaks ~3.1-3.3 words/s including
# sentence pauses (length_scale 1.0-1.15); a human narrator is ~2.5.
WORDS_PER_SECOND = 3.0
MIN_LESSON_S, MAX_LESSON_S = 60, 120


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# ------------------------------------------------------------- syllabus -> topics
class TopicSpec(Strict):
    title: str = Field(min_length=3, max_length=120)
    objectives: list[str] = Field(min_length=3, max_length=5)
    prerequisites: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("objectives")
    @classmethod
    def _non_empty_objectives(cls, v: list[str]) -> list[str]:
        v = [o.strip() for o in v]
        if any(len(o) < 5 for o in v):
            raise ValueError("each objective must be a full phrase (>= 5 chars)")
        return v


class TopicList(Strict):
    course_title: str = Field(min_length=2, max_length=200)
    topics: list[TopicSpec] = Field(min_length=1, max_length=40)


# ------------------------------------------------------------- topic -> script
class Segment(Strict):
    narration: str = Field(min_length=20, max_length=700)
    visual: str = Field(min_length=10, max_length=700)
    key_equation: str | None = Field(default=None, max_length=200)

    @field_validator("key_equation")
    @classmethod
    def _blank_is_none(cls, v: str | None) -> str | None:
        return v if v else None


class LessonScript(Strict):
    title: str = Field(min_length=3, max_length=120)
    segments: list[Segment] = Field(min_length=3, max_length=6)

    @model_validator(mode="after")
    def _length_fits_60_to_120s(self) -> "LessonScript":
        words = sum(len(re.findall(r"\S+", s.narration)) for s in self.segments)
        lo = int(MIN_LESSON_S * WORDS_PER_SECOND * 0.8)
        hi = int(MAX_LESSON_S * WORDS_PER_SECOND * 1.1)
        if not lo <= words <= hi:
            raise ValueError(
                f"total narration is {words} words; a {MIN_LESSON_S}-{MAX_LESSON_S}s lesson at "
                f"~{WORDS_PER_SECOND} words/s needs {lo}-{hi} words")
        return self

    def estimated_seconds(self) -> float:
        words = sum(len(s.narration.split()) for s in self.segments)
        return words / WORDS_PER_SECOND


# ------------------------------------------------------------- script -> manim code
class SceneCode(Strict):
    code: str = Field(min_length=40, max_length=60_000)
