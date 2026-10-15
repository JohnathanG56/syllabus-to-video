"""Render-failure taxonomy used by the self-repair loop and the eval.

Classifying the failure lets the repair prompt carry a targeted hint, and lets
the eval report *why* first attempts fail (e.g. most failures are hallucinated
manimgl-era API names, not LaTeX).
"""
from __future__ import annotations

import re

SYNTAX = "syntax"
HALLUCINATED_API = "hallucinated_api"  # NameError / AttributeError / ImportError / bad kwargs
LATEX = "latex"
TIMEOUT = "timeout"
OOM = "oom"
VALIDATION = "validation"
DURATION_MISMATCH = "duration_mismatch"
OTHER = "other"

ALL_CLASSES = (SYNTAX, HALLUCINATED_API, LATEX, TIMEOUT, OOM, VALIDATION, DURATION_MISMATCH, OTHER)

HINTS = {
    SYNTAX: "Python syntax error. Fix the exact line: brackets, indentation, quotes; put LaTeX in raw strings r\"...\".",
    HALLUCINATED_API: ("A name, attribute or keyword argument does not exist in Manim Community v0.19 "
                       "(often a manimgl/3b1b-era API or an invented parameter). Replace it with the CE "
                       "equivalent from the API reminders, or rebuild that visual from basic primitives."),
    LATEX: ("LaTeX failed to compile inside MathTex/Tex. Use plain amsmath syntax, balance braces, avoid "
            "unicode and undefined macros, escape % # & _ outside math, and keep \\text{} ASCII. "
            "Simplify the expression if unsure."),
    TIMEOUT: ("Rendering exceeded the time limit. Remove while-loops, avoid thousands of mobjects or "
              "updaters doing heavy work, and keep run_times within the segment budget."),
    OOM: "Rendering ran out of memory. Use far fewer mobjects and avoid large arrays or many deep copies.",
    VALIDATION: ("The code violates the sandbox contract (see violations). Remove the forbidden constructs "
                 "entirely; do not try to work around the rules."),
    DURATION_MISMATCH: ("The rendered video length does not match the narration. End every segment i with "
                        "self.sync_to(i), keep run_times inside each segment's budget, and add nothing "
                        "after the final sync_to."),
    OTHER: "Read the traceback carefully and fix the root cause.",
}

_LATEX_MARKERS = re.compile(
    r"latex error|LaTeX Error|tex_file_writing|dvisvgm|Undefined control sequence|Missing \$ inserted|"
    r"compilation failed|\.tex\b.*error|Extra \}|Missing \} inserted|File `.*\.sty' not found",
    re.IGNORECASE)
_BAD_SIGNATURE = re.compile(r"unexpected keyword argument|got multiple values for|"
                            r"missing \d+ required positional argument|takes \d+ positional argument")


def classify_render_failure(exit_reason: str, error_type: str | None, error_message: str | None,
                            stderr_tail: str) -> str:
    if exit_reason == "timeout":
        return TIMEOUT
    if exit_reason == "oom":
        return OOM
    et = error_type or ""
    text = f"{error_message or ''}\n{stderr_tail or ''}"
    if et in ("SyntaxError", "IndentationError", "TabError"):
        return SYNTAX
    if _LATEX_MARKERS.search(text):
        return LATEX
    if et in ("NameError", "AttributeError", "ImportError", "ModuleNotFoundError", "UnboundLocalError"):
        return HALLUCINATED_API
    if et == "TypeError" and _BAD_SIGNATURE.search(text):
        return HALLUCINATED_API
    if not et:
        # no structured info (e.g. crashed before result.json): fall back to text sniffing
        m = re.search(r"^(\w+Error):", stderr_tail or "", re.MULTILINE)
        if m:
            return classify_render_failure(exit_reason, m.group(1), error_message, stderr_tail)
    return OTHER


def trim_traceback(output: str, scene_marker: str = "scene.py", max_lines: int = 40) -> str:
    """Keep what a model needs to fix the bug: frames in the scene file (+ their
    source line), the innermost frame, LaTeX '!' error lines, and the exception."""
    lines = output.splitlines()
    tex_errors = [ln for ln in lines if ln.lstrip().startswith("! ") or "LaTeX Error" in ln][:6]
    starts = [i for i, ln in enumerate(lines) if ln.startswith("Traceback (most recent call last)")]
    if not starts:
        body = lines[-max_lines:]
        return "\n".join(tex_errors + body)[-6000:]
    tb = lines[starts[-1]:]
    frames: list[tuple[int, int]] = []  # (start, end) of each "File ..." frame
    exc_start = len(tb)
    for i, ln in enumerate(tb):
        if ln.startswith('  File "'):
            frames.append((i, i))
        elif ln.startswith("    ") and frames:
            frames[-1] = (frames[-1][0], i)
        elif i > 0 and not ln.startswith(" "):
            exc_start = i
            break
    keep: list[str] = [tb[0]]
    for idx, (s, e) in enumerate(frames):
        is_scene = scene_marker in tb[s]
        is_last = idx == len(frames) - 1
        if is_scene or is_last:
            keep.extend(tb[s:e + 1])
        elif keep[-1] != "  ...":
            keep.append("  ...")
    keep.extend(tb[exc_start:])
    out = tex_errors + keep
    if len(out) > max_lines:
        out = out[:max_lines // 2] + ["  ..."] + out[-max_lines // 2:]
    return "\n".join(out)
