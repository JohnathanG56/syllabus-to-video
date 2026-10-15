import threading
import time

import pytest

from core.db import Database


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "q.sqlite").init()


def test_enqueue_is_idempotent_while_live(db):
    j1 = db.enqueue_job("process_topic", 7)
    assert j1 is not None
    assert db.enqueue_job("process_topic", 7) is None  # duplicate while queued
    job = db.claim_job("w1")
    assert db.enqueue_job("process_topic", 7) is None  # duplicate while running
    db.finish_job(job["id"])
    assert db.enqueue_job("process_topic", 7) is not None  # allowed again once done


def test_claim_is_fifo_and_filters_kinds(db):
    a = db.enqueue_job("extract_topics", 1)
    b = db.enqueue_job("process_topic", 1)
    assert db.claim_job("w", kinds=("process_topic",))["id"] == b
    assert db.claim_job("w")["id"] == a
    assert db.claim_job("w") is None


def test_concurrent_claims_never_double_assign(db):
    for i in range(30):
        db.enqueue_job("process_topic", i)
    claimed, lock = [], threading.Lock()

    def worker(name):
        while True:
            job = db.claim_job(name)
            if job is None:
                return
            with lock:
                claimed.append(job["id"])

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(set(claimed)) and len(claimed) == 30


def test_fail_job_retries_then_fails(db):
    jid = db.enqueue_job("process_topic", 1, max_attempts=2)
    db.claim_job("w")
    assert db.fail_job(jid, "boom") == "queued"
    db.claim_job("w")
    assert db.fail_job(jid, "boom again") == "failed"
    assert db.list_jobs("failed")[0]["error"] == "boom again"


def test_fail_job_without_retry(db):
    jid = db.enqueue_job("process_topic", 1, max_attempts=5)
    db.claim_job("w")
    assert db.fail_job(jid, "fatal", retry=False) == "failed"


def test_requeue_stale_running_jobs(db):
    jid = db.enqueue_job("process_topic", 1)
    db.claim_job("dead-worker")
    assert db.requeue_stale(older_than_s=3600) == 0
    time.sleep(0.05)
    assert db.requeue_stale(older_than_s=0.01) == 1
    assert db.list_jobs("queued")[0]["id"] == jid


def test_topic_updates_validate_fields_and_states(db):
    sid = db.create_syllabus("t" * 50)
    [tid] = db.insert_topics(sid, [{"title": "T", "slug": "t", "objectives": ["a", "b", "c"]}])
    db.update_topic(tid, status="scripted", script_json='{"x": 1}')
    t = db.get_topic(tid)
    assert t["status"] == "scripted" and t["script"] == {"x": 1}
    with pytest.raises(ValueError):
        db.update_topic(tid, status="teleported")
    with pytest.raises(ValueError):
        db.update_topic(tid, slug="nope")


def test_render_attempt_log(db):
    sid = db.create_syllabus("t" * 50)
    [tid] = db.insert_topics(sid, [{"title": "T", "slug": "t", "objectives": ["a", "b", "c"]}])
    db.log_render_attempt(tid, 1, False, 3.2, error_class="hallucinated_api", exit_reason="error")
    db.log_render_attempt(tid, 2, True, 4.1, video_duration_s=61.0)
    rows = db.list_render_attempts(tid)
    assert [(r["attempt_no"], r["success"], r["error_class"]) for r in rows] == [
        (1, 0, "hallucinated_api"), (2, 1, None)]
