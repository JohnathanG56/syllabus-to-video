"""Small LLM interface so the pipeline never talks to a vendor SDK directly.

* ``LLM.complete`` is the only thing a backend implements.
* ``complete_json`` adds the contract: JSON-only output, pydantic validation,
  and exactly one retry that feeds the validation error back to the model.
* ``AnthropicLLM`` is the production backend (official ``anthropic`` SDK).
* ``ScriptedLLM`` is a deterministic fake for tests and demo mode.
"""
from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

T = TypeVar("T", bound=BaseModel)


class LLMConfigError(RuntimeError):
    """Missing key/model etc."""


class LLMOutputError(RuntimeError):
    """The model failed the JSON contract twice."""

    def __init__(self, message: str, raw_outputs: list[str]):
        super().__init__(message)
        self.raw_outputs = raw_outputs


@dataclass
class LLMResponse:
    text: str
    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    model: str | None = None
    truncated: bool = False


class LLM(ABC):
    model: str | None = None

    @abstractmethod
    def complete(self, system: str, messages: list[dict], max_tokens: int,
                 task: str = "") -> LLMResponse:
        """messages: [{"role": "user"|"assistant", "content": str}, ...]"""


# Called after every LLM call: (task, response, ok)
UsageHook = Callable[[str, LLMResponse, bool], None]


# ---------------------------------------------------------------- JSON contract
_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def extract_json(text: str) -> dict:
    """Parse the first JSON object in ``text``, tolerating ```json fences and
    leading/trailing chatter."""
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    for cand in candidates:
        cand = cand.strip()
        start = cand.find("{")
        if start == -1:
            continue
        try:
            obj, _ = json.JSONDecoder().raw_decode(cand[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    raise ValueError("no JSON object found in model output")


def json_instructions(schema: type[BaseModel]) -> str:
    return (
        "\n\n# Output format\nRespond with ONE JSON object and nothing else (no prose, no markdown "
        "fences). It must validate against this JSON Schema; extra keys are rejected:\n"
        + json.dumps(schema.model_json_schema(), indent=1)
    )


def _format_error(err: Exception) -> str:
    if isinstance(err, ValidationError):
        lines = []
        for e in err.errors()[:12]:
            loc = ".".join(str(p) for p in e["loc"]) or "<root>"
            lines.append(f"- {loc}: {e['msg']}")
        return "\n".join(lines)
    return f"- {err}"


def complete_json(llm: LLM, *, system: str, user: str, schema: type[T], task: str,
                  max_tokens: int = 8000, on_usage: UsageHook | None = None) -> T:
    """Call the model, validate against ``schema``; on failure retry ONCE with the
    validation error appended to the conversation. Raises LLMOutputError."""
    sys_prompt = system + json_instructions(schema)
    messages: list[dict] = [{"role": "user", "content": user}]
    raw_outputs: list[str] = []
    last_err: Exception | None = None
    for attempt in (1, 2):
        resp = llm.complete(sys_prompt, messages, max_tokens, task=task)
        raw_outputs.append(resp.text)
        try:
            parsed = schema.model_validate(extract_json(resp.text))
        except (ValueError, ValidationError) as err:
            last_err = err
            if on_usage:
                on_usage(task, resp, False)
            if attempt == 2:
                break
            hint = _format_error(err)
            if resp.truncated:
                hint += "\n- your output was cut off at the token limit; be more concise"
            messages = messages + [
                {"role": "assistant", "content": resp.text},
                {"role": "user", "content": "Your previous response failed validation:\n" + hint +
                 "\nReturn ONLY the corrected JSON object."},
            ]
            continue
        if on_usage:
            on_usage(task, resp, True)
        return parsed
    raise LLMOutputError(f"{task}: model output failed validation twice:\n{_format_error(last_err)}",
                         raw_outputs)


# ---------------------------------------------------------------- backends
class AnthropicLLM(LLM):
    """Production backend using the official ``anthropic`` Python SDK."""

    def __init__(self, api_key: str, model: str, temperature: float | None = None,
                 client=None, timeout_s: float = 300.0):
        if not model:
            raise LLMConfigError("LLM_MODEL is not set. Put a current Claude model id in .env "
                                 "(see https://docs.anthropic.com/en/docs/about-claude/models).")
        self.model = model
        self.temperature = temperature
        if client is None:
            if not api_key:
                raise LLMConfigError("ANTHROPIC_API_KEY is not set (put it in .env).")
            import anthropic  # imported lazily so tests/demo never need the SDK
            client = anthropic.Anthropic(api_key=api_key, max_retries=3, timeout=timeout_s)
        self.client = client

    def complete(self, system: str, messages: list[dict], max_tokens: int,
                 task: str = "") -> LLMResponse:
        kwargs = dict(model=self.model, max_tokens=max_tokens, system=system, messages=messages)
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        t0 = time.monotonic()
        resp = self.client.messages.create(**kwargs)
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "text") == "text")
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            text=text,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            latency_s=round(time.monotonic() - t0, 3),
            model=getattr(resp, "model", self.model),
            truncated=getattr(resp, "stop_reason", None) == "max_tokens",
        )


@dataclass
class ScriptedLLM(LLM):
    """Deterministic fake. ``responder(system, messages, task) -> str`` or a list
    of canned strings returned in order. Every call is recorded in ``calls``."""

    responses: list[str] | None = None
    responder: Callable[[str, list[dict], str], str] | None = None
    model: str | None = "scripted-fake"
    calls: list[dict] = field(default_factory=list)

    def complete(self, system: str, messages: list[dict], max_tokens: int,
                 task: str = "") -> LLMResponse:
        self.calls.append({"system": system, "messages": [dict(m) for m in messages], "task": task})
        if self.responder is not None:
            text = self.responder(system, messages, task)
        else:
            if not self.responses:
                raise AssertionError(f"ScriptedLLM ran out of canned responses (task={task})")
            text = self.responses.pop(0)
        return LLMResponse(text=text, input_tokens=len(system + str(messages)) // 4,
                           output_tokens=len(text) // 4, model=self.model)


def get_llm(settings=None) -> LLM:
    """Build the configured production LLM (raises LLMConfigError if not configured)."""
    if settings is None:
        from core.config import get_settings
        settings = get_settings()
    return AnthropicLLM(settings.anthropic_api_key or "", settings.llm_model or "",
                        temperature=settings.llm_temperature)
