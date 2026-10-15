"""Code-generation + render evaluation over a fixed topic list.

For each topic: LLM script -> durations (estimated from word count, or real
Piper narration with --with-narration) -> LLM Manim code -> sandboxed render
with up to K self-repairs. Reports:

  * first-try render success %   (attempt 1 rendered and matched the narration length)
  * success % after <= K repairs
  * average attempts per topic
  * error-class breakdown (all failed attempts, and first attempts only)
  * average wall time per video (script -> validated render) and render time
  * LLM tokens per video (multiply by your model's current prices for $/video)

Writes results.csv, summary.json and summary.md to eval/results/<timestamp>/.

This NEEDS a real API key. Without ANTHROPIC_API_KEY and LLM_MODEL it refuses to
run: there is no code path that invents numbers. ``--fake-llm`` exists only to
test the harness; its outputs are stamped SYNTHETIC everywhere.

  python -m eval.run_eval                 # all 25 topics, K=3, docker sandbox
  python -m eval.run_eval --limit 5 --k 3 --jobs 2
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import platform
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.config import Settings, get_settings  # noqa: E402
from core.llm import LLM, LLMOutputError, ScriptedLLM  # noqa: E402
from core.schemas import WORDS_PER_SECOND, LessonScript  # noqa: E402
from pipeline import errors  # noqa: E402
from pipeline.media import probe_duration  # noqa: E402
from pipeline.repair import render_with_repair  # noqa: E402
from pipeline.script import write_script  # noqa: E402
from sandbox.run import Runner  # noqa: E402

TOPICS_FILE = Path(__file__).resolve().parent / "topics.json"
SYNTHETIC_BANNER = "SYNTHETIC RUN (--fake-llm): NOT A REAL EVALUATION. Do not report these numbers."


@dataclass
class TopicResult:
    topic_id: str
    title: str
    success: bool
    first_try_success: bool
    attempts: int
    repairs_used: int
    error_classes: list[str] = field(default_factory=list)
    failure_class: str | None = None
    duration_ok: bool | None = None
    narration_s: float = 0.0
    video_s: float | None = None
    render_seconds: float = 0.0
    wall_seconds: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    llm_calls: int = 0
    note: str | None = None


def estimate_durations(script: LessonScript, pad_s: float) -> list[float]:
    return [round(len(s.narration.split()) / WORDS_PER_SECOND + pad_s, 3) for s in script.segments]


def eval_topic(topic: dict, course_title: str, *, llm: LLM, runner: Runner, settings: Settings, k: int,
               work_root: Path, with_narration: bool, quality: str,
               probe: Callable[[Path], float] = probe_duration) -> TopicResult:
    usage = Counter()

    def on_usage(task, resp, ok):
        usage["in"] += resp.input_tokens
        usage["out"] += resp.output_tokens
        usage["calls"] += 1

    t0 = time.monotonic()
    res = TopicResult(topic["id"], topic["title"], False, False, 0, 0)
    try:
        script = write_script(llm, title=topic["title"], objectives=topic["objectives"],
                              prerequisites=topic.get("prerequisites", []), course_title=course_title,
                              on_usage=on_usage)
    except LLMOutputError as exc:
        res.failure_class, res.note = "llm_output", f"script: {exc}"[:300]
        res.wall_seconds = round(time.monotonic() - t0, 2)
        return _with_usage(res, usage)

    tdir = work_root / topic["id"]
    if with_narration:
        from pipeline.narration import get_tts, narrate_script
        durations = narrate_script(get_tts(settings), script, tdir / "audio", pad_s=settings.segment_pad_s)
    else:
        durations = estimate_durations(script, settings.segment_pad_s)
    res.narration_s = round(sum(durations), 2)

    loop = render_with_repair(llm, runner, script=script, durations=durations, work_dir=tdir, max_repairs=k,
                              quality=quality, timeout_s=settings.validate_timeout_s,
                              duration_tolerance_s=settings.duration_tolerance_s, on_usage=on_usage,
                              probe=probe, llm_max_tokens=settings.llm_max_tokens)
    res.success = loop.success
    res.attempts = len(loop.attempts)
    res.repairs_used = loop.repairs_used
    res.first_try_success = bool(loop.attempts) and loop.attempts[0].success and loop.attempts[0].error_class is None
    res.error_classes = [a.error_class for a in loop.attempts if not a.success and a.error_class]
    res.failure_class = None if loop.success else loop.failure_class
    res.duration_ok = loop.duration_ok
    res.video_s = loop.video_duration_s
    res.render_seconds = round(sum(a.render_seconds for a in loop.attempts), 2)
    res.wall_seconds = round(time.monotonic() - t0, 2)
    res.note = loop.warning[:300] if loop.warning else None
    return _with_usage(res, usage)


def _with_usage(res: TopicResult, usage: Counter) -> TopicResult:
    res.input_tokens, res.output_tokens, res.llm_calls = usage["in"], usage["out"], usage["calls"]
    return res


def summarize(results: list[TopicResult], meta: dict) -> dict:
    n = len(results)
    ok = [r for r in results if r.success]
    all_fail_classes = Counter(c for r in results for c in r.error_classes)
    first_fail = Counter(r.error_classes[0] for r in results if r.error_classes and not r.first_try_success)
    pct = lambda x: round(100.0 * x / n, 1) if n else 0.0  # noqa: E731
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None  # noqa: E731
    return {
        **meta,
        "topics": n,
        "first_try_success_pct": pct(sum(r.first_try_success for r in results)),
        "success_after_repairs_pct": pct(len(ok)),
        "avg_attempts": avg([r.attempts for r in results]),
        "avg_attempts_successful": avg([r.attempts for r in ok]),
        "avg_wall_seconds_per_video": avg([r.wall_seconds for r in ok]),
        "avg_render_seconds_per_video": avg([r.render_seconds for r in ok]),
        "avg_input_tokens_per_video": avg([r.input_tokens for r in results]),
        "avg_output_tokens_per_video": avg([r.output_tokens for r in results]),
        "duration_within_tolerance_pct_of_successes": (round(100.0 * sum(bool(r.duration_ok) for r in ok) / len(ok), 1)
                                                        if ok else None),
        "error_classes_all_failed_attempts": dict(all_fail_classes.most_common()),
        "error_classes_first_attempt": dict(first_fail.most_common()),
        "failed_topics": {r.topic_id: r.failure_class for r in results if not r.success},
    }


def to_markdown(s: dict) -> str:
    lines = []
    if s.get("synthetic"):
        lines += [f"> **{SYNTHETIC_BANNER}**", ""]
    lines += [
        f"Eval run {s['timestamp']} | model `{s['model']}` | K={s['k']} | quality -q{s['quality']} | "
        f"backend {s['backend']} | narration: {'Piper' if s['with_narration'] else 'estimated'} | "
        f"machine {s['machine']}",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Topics | {s['topics']} |",
        f"| First-try render success | {s['first_try_success_pct']}% |",
        f"| Success after <= {s['k']} repairs | {s['success_after_repairs_pct']}% |",
        f"| Avg attempts per topic | {s['avg_attempts']} |",
        f"| Avg wall time per video (script -> validated render) | {s['avg_wall_seconds_per_video']} s |",
        f"| Avg render time per video (all attempts) | {s['avg_render_seconds_per_video']} s |",
        f"| Avg LLM tokens per video (in / out) | {s['avg_input_tokens_per_video']} / {s['avg_output_tokens_per_video']} |",
        f"| Successful videos within +-1 s of narration | {s['duration_within_tolerance_pct_of_successes']}% |",
        "",
        "| Error class | Failed attempts (all) | First-attempt failures |",
        "|---|---|---|",
    ]
    classes = set(s["error_classes_all_failed_attempts"]) | set(s["error_classes_first_attempt"])
    for c in sorted(classes, key=lambda c: -s["error_classes_all_failed_attempts"].get(c, 0)):
        lines.append(f"| {c} | {s['error_classes_all_failed_attempts'].get(c, 0)} | "
                     f"{s['error_classes_first_attempt'].get(c, 0)} |")
    if not classes:
        lines.append("| (none) | 0 | 0 |")
    return "\n".join(lines) + "\n"


def write_outputs(results: list[TopicResult], summary: dict, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = "SYNTHETIC-" if summary.get("synthetic") else ""
    with open(out_dir / f"{prefix}results.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        if summary.get("synthetic"):
            w.writerow([f"# {SYNTHETIC_BANNER}"])
        fields = list(asdict(results[0]).keys()) if results else []
        w.writerow(fields)
        for r in results:
            row = asdict(r)
            row["error_classes"] = "|".join(row["error_classes"])
            w.writerow([row[f] for f in fields])
    (out_dir / f"{prefix}summary.json").write_text(json.dumps(summary, indent=2))
    (out_dir / f"{prefix}summary.md").write_text(to_markdown(summary))


def run_eval(topics: list[dict], course_titles: dict, *, llm: LLM, runner: Runner, settings: Settings,
             k: int, out_root: Path, with_narration: bool = False, quality: str = "l", jobs: int = 1,
             synthetic: bool = False, probe: Callable[[Path], float] = probe_duration) -> dict:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_root / (("SYNTHETIC-" if synthetic else "") + stamp)
    work_root = out_dir / "work"

    def one(t):
        r = eval_topic(t, course_titles.get(t.get("course"), ""), llm=llm, runner=runner, settings=settings, k=k,
                       work_root=work_root, with_narration=with_narration, quality=quality, probe=probe)
        status = "OK " if r.success else "FAIL"
        print(f"[{status}] {t['id']} {t['title'][:48]:48s} attempts={r.attempts} "
              f"classes={','.join(r.error_classes) or '-'} {r.wall_seconds:.0f}s", flush=True)
        return r

    if jobs > 1:
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            results = list(pool.map(one, topics))
    else:
        results = [one(t) for t in topics]
    meta = {
        "timestamp": stamp, "model": getattr(llm, "model", None), "k": k, "quality": quality,
        "backend": getattr(runner, "name", type(runner).__name__), "with_narration": with_narration,
        "machine": f"{platform.system()} {platform.machine()}, {os.cpu_count()} CPUs", "synthetic": synthetic,
    }
    summary = summarize(results, meta)
    write_outputs(results, summary, out_dir)
    summary["out_dir"] = str(out_dir)
    return summary


# ---------------------------------------------------------------- harness self-test
def fake_llm() -> ScriptedLLM:
    """Canned, deterministic responses for testing the harness only."""
    counter = Counter()

    def respond(system, messages, task):
        user = messages[0]["content"]
        if task == "script":
            title = re.search(r"Topic: (.*)", user).group(1)
            narr = ("Here is a short spoken sentence about the idea on screen, explained slowly and clearly "
                    "so that every viewer can follow along without any trouble at all today. ")
            return json.dumps({"title": title, "segments": [
                {"narration": narr * 2, "visual": "A circle appears in the center.", "key_equation": None}
                for _ in range(3)]})
        counter[task] += 1
        bad = task == "codegen" and counter["codegen"] % 3 == 0  # every third topic needs a repair
        body = "\n".join(f"        self.play({'ShowCreation' if bad else 'Create'}(Circle()), run_time=0.5)\n"
                         f"        self.sync_to({i})" for i in range(3))
        code = ("from manim import *\n\n\nclass LessonScene(Scene):\n    def sync_to(self, i):\n"
                "        remaining = SEGMENT_ENDS[i] - self.renderer.time\n        if remaining > 0.02:\n"
                "            self.wait(remaining)\n\n    def construct(self):\n" + body + "\n")
        return json.dumps({"code": code})

    return ScriptedLLM(responder=respond, model="fake-llm (synthetic)")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--k", type=int, default=3, help="max repairs per topic (default 3)")
    ap.add_argument("--limit", type=int, default=None, help="only the first N topics")
    ap.add_argument("--with-narration", action="store_true", help="synthesize real Piper narration (slower)")
    ap.add_argument("--quality", default=None, help="manim quality letter (default: VALIDATE_QUALITY)")
    ap.add_argument("--jobs", type=int, default=1, help="topics in parallel (renders compete for CPU)")
    ap.add_argument("--topics", type=Path, default=TOPICS_FILE)
    ap.add_argument("--out", type=Path, default=ROOT / "eval" / "results")
    ap.add_argument("--fake-llm", action="store_true", help="harness self-test; output is stamped SYNTHETIC")
    a = ap.parse_args(argv)

    settings = get_settings()
    if a.fake_llm:
        llm: LLM = fake_llm()
        print(f"!! {SYNTHETIC_BANNER}")
    else:
        if not settings.anthropic_api_key or not settings.llm_model:
            print("Refusing to run: the eval measures a real model, so it needs ANTHROPIC_API_KEY and LLM_MODEL "
                  "(set them in .env). Nothing was written.", file=sys.stderr)
            return 2
        from core.llm import get_llm
        llm = get_llm(settings)

    from pipeline.orchestrator import LimitedRunner
    from sandbox.run import get_runner
    runner = LimitedRunner(get_runner(settings), max(1, min(a.jobs, settings.render_concurrency)))
    runner.name = settings.render_backend
    data = json.loads(a.topics.read_text())
    topics = data["topics"][: a.limit] if a.limit else data["topics"]
    summary = run_eval(topics, data.get("course_titles", {}), llm=llm, runner=runner, settings=settings, k=a.k,
                       out_root=a.out, with_narration=a.with_narration,
                       quality=a.quality or settings.validate_quality, jobs=a.jobs, synthetic=a.fake_llm)
    print()
    print(to_markdown(summary))
    print(f"results: {summary['out_dir']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
