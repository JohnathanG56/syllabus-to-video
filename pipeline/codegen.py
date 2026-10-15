"""Stage 4: lesson script + narration durations -> Manim code (LLM).

Timing contract: the model never writes timing numbers itself. We inject
SEGMENT_DURATIONS / SEGMENT_ENDS (measured from the narration WAVs) after the
imports, and the model's code ends each segment with ``self.sync_to(i)``, which
waits until the absolute narration end time of segment i. Because targets are
absolute, an animation that overruns one segment is absorbed by the next sync
instead of drifting for the rest of the video.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from core import prompts
from core.llm import LLM, UsageHook, complete_json
from core.schemas import LessonScript, SceneCode
from pipeline.validator import ValidationResult, Violation, validate_code

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "prompts" / "examples"
TIMING_NAMES = {"SEGMENT_DURATIONS", "SEGMENT_ENDS"}
HEADER_START = "# --- injected by the pipeline (narration timing, seconds) ---"
HEADER_END = "# ------------------------------------------------------------"


def segment_ends(durations: list[float]) -> list[float]:
    ends, t = [], 0.0
    for d in durations:
        t += d
        ends.append(round(t, 3))
    return ends


def timing_header(durations: list[float]) -> str:
    durs = [round(float(d), 3) for d in durations]
    return "\n".join([HEADER_START, f"SEGMENT_DURATIONS = {durs}",
                      f"SEGMENT_ENDS = {segment_ends(durs)}", HEADER_END])


def strip_code_fences(code: str) -> str:
    s = code.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[1] if "\n" in s else ""
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip() + "\n"


def inject_timing(code: str, durations: list[float]) -> str:
    """Remove any model-written timing constants / old header and insert ours
    right after the top-level imports."""
    code = strip_code_fences(code)
    lines = code.splitlines()
    # drop a previously injected header (repair rounds send our own code back)
    if HEADER_START in lines:
        start = lines.index(HEADER_START)
        end = lines.index(HEADER_END, start) if HEADER_END in lines[start:] else start
        lines = lines[:start] + lines[end + 1:]
    insert_at = 0
    try:
        tree = ast.parse("\n".join(lines))
        drop: set[int] = set()
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                insert_at = max(insert_at, node.end_lineno or node.lineno)
            if isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id in TIMING_NAMES for t in node.targets):
                drop.update(range(node.lineno - 1, (node.end_lineno or node.lineno)))
        kept = [(i, ln) for i, ln in enumerate(lines) if i not in drop]
        insert_at = sum(1 for i, _ in kept if i < insert_at)
        lines = [ln for _, ln in kept]
    except SyntaxError:
        for i, ln in enumerate(lines):
            if ln.startswith("from manim import") or ln.startswith("import "):
                insert_at = i + 1
    header = ["", *timing_header(durations).splitlines(), ""]
    return "\n".join(lines[:insert_at] + header + lines[insert_at:]).rstrip() + "\n"


def check_timing_contract(code: str, n_segments: int) -> list[Violation]:
    """Cheap structural check that the code follows the sync_to contract."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return []  # reported by the validator
    scene = next((n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "LessonScene"), None)
    if scene is None:
        return []
    problems = []
    if not any(isinstance(n, ast.FunctionDef) and n.name == "sync_to" for n in scene.body):
        problems.append(Violation(scene.lineno, "timing", "LessonScene must define the sync_to(self, i) method"))
    calls: list[int] = []
    for node in ast.walk(scene):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "sync_to"
                and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, int)):
            calls.append(node.args[0].value)
    expected = list(range(n_segments))
    if sorted(calls) != expected:
        problems.append(Violation(scene.lineno, "timing",
                                  f"expected exactly one self.sync_to(i) for each i in {expected}; found {sorted(calls)}"))
    return problems


def prepare_scene(raw_code: str, durations: list[float]) -> tuple[str, ValidationResult]:
    """Inject timing, then run the security validator and the timing-contract check."""
    code = inject_timing(raw_code, durations)
    result = validate_code(code)
    if not result.syntax_error:
        contract = check_timing_contract(code, len(durations))
        if contract:
            result = ValidationResult(False, sorted(result.violations + contract, key=lambda v: v.line))
    return code, result


def format_segments(script: LessonScript, durations: list[float]) -> str:
    out = []
    for i, (seg, d) in enumerate(zip(script.segments, durations)):
        out.append(f"Segment {i} ({d:.2f}s):\n  narration: {seg.narration}\n  visual: {seg.visual}\n"
                   f"  key_equation: {json.dumps(seg.key_equation)}")
    return "\n".join(out)


def codegen_prompt(script: LessonScript, durations: list[float]) -> tuple[str, str]:
    return prompts.build(
        "manim_codegen",
        example_1=(EXAMPLES_DIR / "vector_addition.py").read_text().strip(),
        example_2=(EXAMPLES_DIR / "tangent_slope.py").read_text().strip(),
        title=script.title,
        n_segments=len(script.segments),
        durations=[round(d, 2) for d in durations],
        ends=[round(e, 2) for e in segment_ends(durations)],
        segments=format_segments(script, durations),
    )


def generate_code(llm: LLM, script: LessonScript, durations: list[float],
                  on_usage: UsageHook | None = None, max_tokens: int = 8000) -> str:
    if len(durations) != len(script.segments):
        raise ValueError("need exactly one duration per segment")
    system, user = codegen_prompt(script, durations)
    out = complete_json(llm, system=system, user=user, schema=SceneCode, task="codegen",
                        max_tokens=max_tokens, on_usage=on_usage)
    return out.code
