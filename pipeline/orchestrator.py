"""Runs one topic through every stage, persisting state after each one.

  queued -> scripted -> narrated -> coded -> rendering -> (repairing) -> assembled -> done | failed

Each stage is idempotent and checks for its own output first (script in the DB,
WAVs on disk, validated.json / final.json markers, the final MP4), so a crashed
or restarted worker resumes where it stopped instead of paying for LLM calls
or renders twice.
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from pathlib import Path

from core.config import Settings
from core.db import Database
from core.llm import LLM, LLMOutputError
from core.schemas import LessonScript
from pipeline.assemble import assemble_lesson, output_path
from pipeline.narration import TTS, narrate_script, segment_wav_path
from pipeline.repair import Attempt, render_with_repair
from pipeline.script import write_script
from sandbox.run import RenderResult, Runner


class LimitedRunner(Runner):
    """Caps concurrent renders across worker threads (renders are the CPU/RAM hog;
    LLM calls and TTS can overlap freely)."""

    def __init__(self, inner: Runner, max_concurrent: int):
        self.inner = inner
        self.name = f"limited-{inner.name}"
        self._sem = threading.BoundedSemaphore(max_concurrent)
        self.max_concurrent = max_concurrent
        self._active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def render(self, *args, **kwargs) -> RenderResult:
        with self._sem:
            with self._lock:
                self._active += 1
                self.peak = max(self.peak, self._active)
            try:
                return self.inner.render(*args, **kwargs)
            finally:
                with self._lock:
                    self._active -= 1


@dataclass
class Pipeline:
    db: Database
    llm: LLM
    runner: Runner
    tts: TTS
    settings: Settings

    # ------------------------------------------------------------------ helpers
    def topic_dir(self, topic: dict) -> Path:
        return self.settings.work_dir / str(topic["syllabus_id"]) / f"{topic['position']}-{topic['slug']}"

    def _usage_hook(self, topic: dict):
        def hook(task, resp, ok):
            self.db.log_llm_call(task, resp.model, resp.input_tokens, resp.output_tokens, resp.latency_s, ok,
                                 syllabus_id=topic["syllabus_id"], topic_id=topic["id"])
        return hook

    def _fail(self, topic_id: int, message: str) -> str:
        self.db.update_topic(topic_id, status="failed", error=message[:2000])
        return "failed"

    # ------------------------------------------------------------------ stages
    def process_topic(self, topic_id: int) -> str:
        db, s = self.db, self.settings
        topic = db.get_topic(topic_id)
        if topic is None:
            raise LookupError(f"topic {topic_id} not found")
        final_out = output_path(s.out_dir, topic["syllabus_id"], topic["position"], topic["slug"])
        if topic["status"] == "done" and final_out.exists():
            return "done"
        db.update_topic(topic_id, error=None)
        tdir = self.topic_dir(topic)
        tdir.mkdir(parents=True, exist_ok=True)
        usage = self._usage_hook(topic)

        # 1) script
        if topic["script"] is None:
            syl = db.get_syllabus(topic["syllabus_id"]) or {}
            try:
                script = write_script(self.llm, title=topic["title"], objectives=topic["objectives"],
                                      prerequisites=topic["prerequisites"], course_title=syl.get("title"),
                                      on_usage=usage)
            except LLMOutputError as exc:
                return self._fail(topic_id, f"script generation failed: {exc}")
            db.update_topic(topic_id, script_json=script.model_dump_json(), status="scripted")
        else:
            script = LessonScript.model_validate(topic["script"])

        # 2) narration first: durations drive the animation timing
        audio_dir = tdir / "audio"
        durations = narrate_script(self.tts, script, audio_dir, pad_s=s.segment_pad_s)
        if topic["durations"] != durations:
            db.update_topic(topic_id, durations_json=json.dumps(durations), status="narrated")

        # 3) codegen + validate + render (low quality) + self-repair
        validated_marker = tdir / "validated.json"
        scene_path = tdir / "scene.py"
        if validated_marker.exists() and scene_path.exists():
            validated = json.loads(validated_marker.read_text())
        else:
            def on_attempt(a: Attempt) -> None:
                db.log_render_attempt(topic_id, a.attempt_no, a.success, a.render_seconds,
                                      error_class=a.error_class, exit_reason=a.exit_reason,
                                      error_message=a.error_message, phase="validate",
                                      video_duration_s=a.video_duration_s)
                db.update_topic(topic_id, attempts=a.attempt_no)

            loop = render_with_repair(
                self.llm, self.runner, script=script, durations=durations, work_dir=tdir / "attempts",
                max_repairs=s.max_repairs, quality=s.validate_quality, timeout_s=s.validate_timeout_s,
                duration_tolerance_s=s.duration_tolerance_s, on_attempt=on_attempt,
                on_status=lambda st: db.update_topic(topic_id, status=st), on_usage=usage,
                llm_max_tokens=s.llm_max_tokens)
            if loop.code:
                db.update_topic(topic_id, code=loop.code)
            if not loop.success:
                n = len(loop.attempts)
                return self._fail(topic_id, f"no renderable code after {n} attempt(s) "
                                            f"(last error: {loop.failure_class}). {loop.warning or ''}".strip())
            scene_path.write_text(loop.code)
            validated = {"video": str(loop.video_path), "video_duration_s": loop.video_duration_s,
                         "duration_ok": loop.duration_ok, "attempts": len(loop.attempts), "warning": loop.warning}
            validated_marker.write_text(json.dumps(validated))
            if loop.warning:
                db.update_topic(topic_id, warning=loop.warning)

        # 4) final re-render at presentation quality (same code, same sandbox)
        final_marker = tdir / "final.json"
        if final_marker.exists():
            final_video = Path(json.loads(final_marker.read_text())["video"])
        elif s.final_quality == s.validate_quality:
            final_video = Path(validated["video"])
        else:
            db.update_topic(topic_id, status="rendering")
            res = self.runner.render(scene_path, tdir / "final_media", s.final_quality, s.final_timeout_s)
            db.log_render_attempt(topic_id, 0, res.success, res.duration_s, phase="final",
                                  exit_reason=res.exit_reason, error_message=res.error_message,
                                  error_class=None if res.success else "final_render")
            if res.success:
                final_video = res.video_path
            else:
                final_video = Path(validated["video"])
                db.update_topic(topic_id, warning=f"final -q{s.final_quality} render failed "
                                                  f"({res.exit_reason}); using the -q{s.validate_quality} render")
            final_marker.write_text(json.dumps({"video": str(final_video)}))

        # 5) assemble: narration + video (+ captions) -> out/{syllabus}/{n}-{slug}.mp4
        wavs = [segment_wav_path(audio_dir, i) for i in range(len(script.segments))]
        try:
            assemble_lesson(video=final_video, segment_wavs=wavs, script=script, durations=durations,
                            out_path=final_out, work_dir=tdir, pad_s=s.segment_pad_s,
                            burn_captions=s.burn_captions)
        except Exception as exc:  # noqa: BLE001 - surface ffmpeg problems on the topic
            return self._fail(topic_id, f"assembly failed: {exc}")
        db.update_topic(topic_id, status="assembled")
        rel = final_out.relative_to(s.out_dir).as_posix()
        db.update_topic(topic_id, status="done", video_path=rel)
        return "done"
