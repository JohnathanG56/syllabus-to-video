"""Worker + API end to end with a fake LLM, fake sandbox (ffmpeg color clips)
and silent TTS: upload -> topics -> scripts -> narration -> code -> render ->
repair -> assembly -> video served over HTTP."""
import json
import re
import threading

import pytest
from fastapi.testclient import TestClient

from api.main import create_app
from core.config import get_settings
from core.db import Database
from core.llm import ScriptedLLM
from pipeline.narration import SilentTTS
from tests.fakes import TOPICS, FfmpegFakeRunner, good_scene, script
from worker.main import Worker

pytestmark = pytest.mark.ffmpeg


def responder(break_first_code_for: str | None = None):
    seen = set()

    def respond(system, messages, task):
        user = messages[0]["content"]
        if task == "topics":
            return json.dumps(dict(TOPICS, topics=TOPICS["topics"][:2]))
        if task == "script":
            title = re.search(r"Topic: (.*)", user).group(1)
            return json.dumps(dict(script(3, 50), title=title))
        if task == "codegen":
            key = user  # first codegen per topic
            code = good_scene(3)
            if break_first_code_for and break_first_code_for in user and key not in seen:
                seen.add(key)
                code = code.replace("Create(", "ShowCreation(")
            return json.dumps({"code": code})
        if task == "repair":
            return json.dumps({"code": good_scene(3)})
        raise AssertionError(task)
    return respond


def make_worker(settings, llm=None, runner=None):
    return Worker(settings, llm=llm or ScriptedLLM(responder=responder()), runner=runner or FfmpegFakeRunner(),
                  tts=SilentTTS(), db=Database(settings.db_path).init(), worker_id="test")


def test_upload_to_video_end_to_end(env):
    app = create_app(env)
    client = TestClient(app)
    sid = client.post("/syllabi", data={"text": "Week 1: vectors. Week 2: transformations. " * 3}).json()["id"]
    llm = ScriptedLLM(responder=responder(break_first_code_for="Linear transformations"))
    w = make_worker(env, llm=llm)
    jobs_run = w.drain()
    assert jobs_run == 3  # extract_topics + 2 process_topic

    body = client.get(f"/syllabi/{sid}").json()
    assert body["status"] == "ready" and body["title"] == TOPICS["course_title"]
    assert body["progress"] == {"total": 2, "done": 2, "failed": 0, "finished": True}
    t1, t2 = body["topics"]
    assert t1["attempts"] == 1 and t2["attempts"] == 2  # second topic needed one repair
    for t in body["topics"]:
        r = client.get(t["video_url"])
        assert r.status_code == 200 and r.headers["content-type"] == "video/mp4" and len(r.content) > 1000
        vtt = client.get(t["captions_url"])
        assert vtt.status_code == 200 and vtt.text.startswith("WEBVTT") and "-->" in vtt.text

    detail = client.get(f"/topics/{t2['id']}").json()
    assert [a["error_class"] for a in detail["render_attempts"] if a["phase"] == "validate"] == [
        "hallucinated_api", None]
    assert detail["llm_usage"]["calls"] == 3 and "sync_to" in detail["code"]


def test_video_endpoint_blocks_path_traversal(env):
    client = TestClient(create_app(env))
    (env.out_dir).mkdir(parents=True, exist_ok=True)
    (env.data_dir).mkdir(parents=True, exist_ok=True)
    (env.data_dir / "secret.mp4").write_bytes(b"x")
    for path in ["../data/secret.mp4", "..%2Fdata%2Fsecret.mp4", "/etc/passwd", "1/../../data/secret.mp4"]:
        assert client.get(f"/videos/{path}").status_code == 404


def test_web_ui_is_served(env):
    client = TestClient(create_app(env))
    r = client.get("/")
    assert r.status_code == 200 and "Syllabus" in r.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/syllabi").json() == []


def test_failed_topic_extraction_marks_syllabus_failed(env):
    db = Database(env.db_path).init()
    sid = db.create_syllabus("text " * 20)
    db.enqueue_job("extract_topics", sid)
    w = make_worker(env, llm=ScriptedLLM(responses=["nope", "nope"]))
    w.drain()
    syl = db.get_syllabus(sid)
    assert syl["status"] == "failed" and "topic extraction failed" in syl["error"]


def test_infrastructure_errors_are_retried(env):
    db = Database(env.db_path).init()
    sid = db.create_syllabus("text " * 20)
    db.enqueue_job("extract_topics", sid)
    calls = {"n": 0}
    good = responder()

    def flaky(system, messages, task):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ConnectionError("API overloaded")
        return good(system, messages, task)

    w = make_worker(env, llm=ScriptedLLM(responder=flaky))
    w.drain()
    assert db.get_syllabus(sid)["status"] == "ready"
    jobs = db.list_jobs()
    assert jobs[0]["attempts"] == 2 and jobs[0]["status"] == "done"


def test_threaded_worker_respects_render_concurrency(env, monkeypatch):
    monkeypatch.setenv("WORKER_THREADS", "3")
    monkeypatch.setenv("RENDER_CONCURRENCY", "1")
    monkeypatch.setenv("WORKER_POLL_S", "0.05")
    settings = get_settings()
    db = Database(settings.db_path).init()
    sid = db.create_syllabus("text " * 20)
    db.enqueue_job("extract_topics", sid)
    w = make_worker(settings, llm=ScriptedLLM(responder=responder()))
    stop = threading.Event()
    t = threading.Thread(target=w.run_forever, args=(stop,), kwargs={"exit_when_idle": True})
    t.start()
    t.join(timeout=120)
    stop.set()
    assert not t.is_alive()
    assert all(tp["status"] == "done" for tp in db.list_topics(sid))
    assert w.runner.peak == 1
