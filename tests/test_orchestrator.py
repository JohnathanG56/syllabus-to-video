import json
import threading
import time

import pytest

from core.config import get_settings
from core.db import Database
from core.llm import ScriptedLLM
from pipeline.narration import SilentTTS
from pipeline.orchestrator import LimitedRunner, Pipeline
from pipeline.media import probe_duration, probe_streams
from sandbox.run import RenderResult
from tests.fakes import FfmpegFakeRunner, TOPICS, good_scene, script

pytestmark = pytest.mark.ffmpeg


def setup(env, responses, runner=None):
    db = Database(env.db_path).init()
    sid = db.create_syllabus("syllabus " * 20)
    ids = db.insert_topics(sid, [{"title": t["title"], "slug": t["title"].lower().replace(" ", "-"),
                                  "objectives": t["objectives"], "prerequisites": t["prerequisites"]}
                                 for t in TOPICS["topics"][:1]])
    llm = ScriptedLLM(responses=list(responses))
    p = Pipeline(db=db, llm=llm, runner=runner or FfmpegFakeRunner(), tts=SilentTTS(), settings=env)
    return p, ids[0], sid


def test_full_topic_pipeline_with_repair(env):
    responses = [json.dumps(script(3, 50)),
                 json.dumps({"code": good_scene(3).replace("Create(", "ShowCreation(")}),
                 json.dumps({"code": good_scene(3)})]
    p, tid, sid = setup(env, responses)
    assert p.process_topic(tid) == "done"
    t = p.db.get_topic(tid)
    assert t["status"] == "done" and t["attempts"] == 2 and t["error"] is None
    out = env.out_dir / t["video_path"]
    assert out.exists() and t["video_path"] == f"{sid}/1-vectors-as-arrows.mp4"
    assert sorted(s["codec_type"] for s in probe_streams(out)) == ["audio", "subtitle", "video"]
    assert probe_duration(out) == pytest.approx(sum(t["durations"]), abs=0.2)
    attempts = p.db.list_render_attempts(tid)
    assert [(a["phase"], a["attempt_no"], a["success"], a["error_class"]) for a in attempts] == [
        ("validate", 1, 0, "hallucinated_api"), ("validate", 2, 1, None), ("final", 0, 1, None)]
    usage = p.db.llm_usage(topic_id=tid)
    assert usage["calls"] == 3
    # final re-render used the medium-quality flag
    assert [q for q, _ in p.runner.calls] == ["l", "l", "m"]


def test_rerun_is_idempotent_and_free(env):
    responses = [json.dumps(script(3, 50)), json.dumps({"code": good_scene(3)})]
    p, tid, _ = setup(env, responses)
    assert p.process_topic(tid) == "done"
    n_render = len(p.runner.calls)
    assert p.process_topic(tid) == "done"
    assert len(p.runner.calls) == n_render and p.llm.calls.__len__() == 2


def test_resume_after_crash_skips_completed_stages(env):
    responses = [json.dumps(script(3, 50)), json.dumps({"code": good_scene(3)})]
    p, tid, _ = setup(env, responses)

    class Crash(Exception):
        pass

    real = p.runner.render

    def crash_on_final(scene, out, quality="l", *a, **k):
        if quality == "m":
            raise Crash()
        return real(scene, out, quality, *a, **k)

    p.runner.render = crash_on_final
    with pytest.raises(Crash):
        p.process_topic(tid)
    assert p.db.get_topic(tid)["status"] == "rendering"
    p.runner.render = real
    llm_calls_before = len(p.llm.calls)
    assert p.process_topic(tid) == "done"
    assert len(p.llm.calls) == llm_calls_before  # script + code reused, no new LLM spend
    assert [q for q, _ in p.runner.calls] == ["l", "m"]


def test_failed_final_render_falls_back_to_validation_video(env):
    responses = [json.dumps(script(3, 50)), json.dumps({"code": good_scene(3)})]
    p, tid, _ = setup(env, responses, runner=FfmpegFakeRunner(fail_final=True))
    assert p.process_topic(tid) == "done"
    assert "using the -ql render" in p.db.get_topic(tid)["warning"]


def test_topic_fails_cleanly_after_k_repairs(env, monkeypatch):
    monkeypatch.setenv("MAX_REPAIRS", "2")
    env = get_settings()
    bad = json.dumps({"code": good_scene(3).replace("Create(", "ShowCreation(")})
    p, tid, _ = setup(env, [json.dumps(script(3, 50)), bad, bad, bad])
    assert p.process_topic(tid) == "failed"
    t = p.db.get_topic(tid)
    assert t["status"] == "failed" and "3 attempt(s)" in t["error"] and "hallucinated_api" in t["error"]
    assert t["attempts"] == 3


def test_script_failure_marks_topic_failed(env):
    p, tid, _ = setup(env, ["nope", "still nope"])
    assert p.process_topic(tid) == "failed"
    assert "script generation failed" in p.db.get_topic(tid)["error"]


def test_limited_runner_caps_concurrency():
    class Slow:
        name = "slow"

        def render(self, *a, **k):
            time.sleep(0.05)
            return RenderResult(True, None, "", 0.05, "ok")

    r = LimitedRunner(Slow(), 2)
    threads = [threading.Thread(target=r.render) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert r.peak == 2
