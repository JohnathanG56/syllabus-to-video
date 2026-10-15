"""Stage 5: render + self-repair loop (the core of the project).

generate -> inject timing -> validate (AST) -> render (sandbox, low quality)
  -> on failure: classify -> repair prompt (code + error class hint + trimmed
     traceback) -> LLM returns a full corrected file -> repeat, at most K repairs
  -> on success: verify rendered duration matches the narration (+-tolerance)

Every attempt (including ones rejected by the validator before rendering) is
reported through ``on_attempt`` so the orchestrator can persist it to the
render_attempts table and the eval can aggregate it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from core import prompts
from core.llm import LLM, LLMOutputError, UsageHook, complete_json
from core.schemas import LessonScript, SceneCode
from pipeline import errors
from pipeline.codegen import codegen_prompt, generate_code, prepare_scene
from pipeline.media import probe_duration
from sandbox.run import RenderResult, Runner


@dataclass
class Attempt:
    attempt_no: int
    success: bool
    error_class: str | None
    exit_reason: str | None
    render_seconds: float
    error_message: str | None = None
    video_duration_s: float | None = None
    code_path: Path | None = None


@dataclass
class LoopResult:
    success: bool
    code: str | None
    video_path: Path | None
    attempts: list[Attempt] = field(default_factory=list)
    video_duration_s: float | None = None
    duration_ok: bool | None = None
    failure_class: str | None = None
    warning: str | None = None

    @property
    def repairs_used(self) -> int:
        return max(0, len(self.attempts) - 1)


def number_lines(code: str) -> str:
    return "\n".join(f"{i:4d} | {ln}" for i, ln in enumerate(code.splitlines(), start=1))


def request_repair(llm: LLM, *, code: str, error_class: str, error_text: str, script: LessonScript,
                   durations: list[float], attempt: int, max_repairs: int,
                   on_usage: UsageHook | None = None, max_tokens: int = 8000) -> str:
    base_system, _ = codegen_prompt(script, durations)
    repair_system, user = prompts.build(
        "repair", attempt=attempt, max_repairs=max_repairs, error_class=error_class,
        hint=errors.HINTS.get(error_class, errors.HINTS[errors.OTHER]), error=error_text.strip() or "(no output)",
        numbered_code=number_lines(code), n_segments=len(durations),
        durations=[round(d, 2) for d in durations], total=round(sum(durations), 2))
    out = complete_json(llm, system=base_system + "\n\n# Repair mode\n" + repair_system, user=user,
                        schema=SceneCode, task="repair", max_tokens=max_tokens, on_usage=on_usage)
    return out.code


def render_with_repair(
    llm: LLM,
    runner: Runner,
    *,
    script: LessonScript,
    durations: list[float],
    work_dir: Path,
    max_repairs: int = 3,
    quality: str = "l",
    timeout_s: float = 180,
    duration_tolerance_s: float = 1.0,
    initial_code: str | None = None,
    on_attempt: Callable[[Attempt], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    on_usage: UsageHook | None = None,
    probe: Callable[[Path], float] = probe_duration,
    llm_max_tokens: int = 8000,
) -> LoopResult:
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    status = on_status or (lambda s: None)
    expected = round(sum(durations), 3)
    result = LoopResult(success=False, code=None, video_path=None)

    try:
        raw = initial_code if initial_code is not None else generate_code(
            llm, script, durations, on_usage=on_usage, max_tokens=llm_max_tokens)
    except LLMOutputError as exc:
        result.failure_class = "llm_output"
        result.warning = str(exc)
        return result
    status("coded")

    for attempt_no in range(1, max_repairs + 2):  # 1 initial try + up to K repairs
        code, validation = prepare_scene(raw, durations)
        attempt_dir = work_dir / f"attempt_{attempt_no}"
        attempt_dir.mkdir(parents=True, exist_ok=True)
        scene_path = attempt_dir / "scene.py"
        scene_path.write_text(code)
        result.code = code

        error_class: str | None
        error_text = ""
        if not validation.ok:
            error_class = validation.error_class
            error_text = validation.format()
            att = Attempt(attempt_no, False, error_class, None, 0.0, error_text[:2000], code_path=scene_path)
        else:
            status("rendering" if attempt_no == 1 else "repairing")
            res: RenderResult = runner.render(scene_path, attempt_dir / "media", quality, timeout_s)
            if res.success:
                vdur = probe(res.video_path)
                ok = abs(vdur - expected) <= duration_tolerance_s
                last = attempt_no == max_repairs + 1
                if ok or last:
                    att = Attempt(attempt_no, True, None if ok else errors.DURATION_MISMATCH, res.exit_reason,
                                  res.duration_s, None, vdur, scene_path)
                    result.attempts.append(att)
                    if on_attempt:
                        on_attempt(att)
                    result.success, result.video_path = True, res.video_path
                    result.video_duration_s, result.duration_ok = vdur, ok
                    if not ok:
                        result.warning = (f"rendered {vdur:.2f}s vs narration {expected:.2f}s; "
                                          "assembly will pad to match")
                    return result
                error_class = errors.DURATION_MISMATCH
                error_text = (f"Rendered video is {vdur:.2f}s but the narration totals {expected:.2f}s "
                              f"(tolerance {duration_tolerance_s}s).")
                att = Attempt(attempt_no, False, error_class, res.exit_reason, res.duration_s, error_text, vdur,
                              scene_path)
            else:
                error_class = errors.classify_render_failure(res.exit_reason, res.error_type,
                                                             res.error_message, res.stderr_tail)
                error_text = errors.trim_traceback(res.stderr_tail)
                if res.exit_reason == "timeout":
                    error_text = f"Render killed after {timeout_s:.0f}s (time limit).\n" + error_text
                att = Attempt(attempt_no, False, error_class, res.exit_reason, res.duration_s,
                              (res.error_message or error_text)[:2000], code_path=scene_path)

        result.attempts.append(att)
        if on_attempt:
            on_attempt(att)
        result.failure_class = error_class
        if attempt_no == max_repairs + 1:
            break
        status("repairing")
        try:
            raw = request_repair(llm, code=code, error_class=error_class or errors.OTHER, error_text=error_text,
                                 script=script, durations=durations, attempt=attempt_no,
                                 max_repairs=max_repairs, on_usage=on_usage, max_tokens=llm_max_tokens)
        except LLMOutputError as exc:
            result.failure_class = "llm_output"
            result.warning = str(exc)
            return result
    return result
